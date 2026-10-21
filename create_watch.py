"""Alerts that create a server automatically once its plan is in stock.

The user picks a sold-out plan, the locations they would accept, an image,
IPv6 on/off and a name (or an automatic one). A scheduled job watches
Hetzner's per-location stock (server_types[].locations) and creates the
server in the first acceptable location that has room, then reports it.

Unlike a plan change, a refused create costs nothing — there is no server to
power off — so besides acting on the stock flag, which has proved to
under-report, it also just tries every PROBE_MINUTES regardless.
"""
import json
import logging
import secrets
import time
from datetime import datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import automation
from hetzner_api import api_registry
from utils import (
    escape_markdown, type_available_at, is_capacity_refusal, api_error_text, copy_button,
)

logger = logging.getLogger(__name__)

WATCH_FILE = Path('create_watches.json')

# Try creating even while the flag says sold out, this often.
PROBE_MINUTES = 15
# After a capacity refusal, wait this long before trying again.
REFUSAL_PAUSE_MINUTES = 5


def _load():
    if WATCH_FILE.exists():
        try:
            data = json.loads(WATCH_FILE.read_text())
            if isinstance(data, list):
                return data
            logger.warning("%s is not a list, ignoring it", WATCH_FILE)
        except Exception as e:
            logger.error(f"Could not read {WATCH_FILE}: {e}")
    return []


def _save(watches):
    try:
        WATCH_FILE.write_text(json.dumps(watches, indent=2))
    except Exception as e:
        logger.error(f"Failed to save create watches: {e}")


def list_create_watches():
    return _load()


def add_create_watch(account, plan, locations, image, ipv6, name, chat_id):
    """Register an auto-create alert; returns its id.

    ``name`` is the server name to use, or None for an automatic one chosen
    at creation time (so it can carry the location actually used).
    """
    watch_id = secrets.token_hex(4)
    watches = _load()
    watches.append({
        'id': watch_id,
        'account': account,
        'plan': plan,
        'locations': list(locations),
        'image': image,
        'ipv6': bool(ipv6),
        'name': name,
        'chat_id': chat_id,
        'created_at': time.time(),
        'failures': 0,
        'retry_after': 0,
        'last_probe': 0,
    })
    _save(watches)
    return watch_id


def remove_create_watch(watch_id):
    watches = _load()
    kept = [w for w in watches if w.get('id') != watch_id]
    if len(kept) == len(watches):
        return False
    _save(kept)
    return True


def _put(watch):
    remove_create_watch(watch.get('id'))
    watches = _load()
    watches.append(watch)
    _save(watches)


def auto_name(plan, location):
    return f"{plan}-{location}-{datetime.now().strftime('%m%d%H%M')}".lower()


def _unique(name, existing):
    if name not in existing:
        return name
    n = 2
    while f"{name}-{n}" in existing:
        n += 1
    return f"{name}-{n}"


async def check_create_watches(bot):
    """Try to create the server for every alert that has a chance now."""
    watches = _load()
    if not watches or automation.is_paused():
        return   # paused: the alerts wait, untouched

    now = time.time()
    by_account = {}
    for w in watches:
        if (w.get('retry_after') or 0) > now:
            continue
        by_account.setdefault(w.get('account'), []).append(w)

    for account, items in by_account.items():
        api = api_registry.get(account)
        if not api:
            logger.warning("Dropping %d create alert(s) for unknown account '%s'",
                           len(items), account)
            for w in items:
                remove_create_watch(w.get('id'))
            continue
        try:
            types = await api.get_server_types(fresh=True)
        except Exception as e:
            logger.error(f"Create-alert check failed for account '{account}': {e}")
            continue

        by_name = {t.get('name'): t for t in types if t.get('name')}
        for w in items:
            stype = by_name.get(w.get('plan'))
            if not stype or stype.get('deprecation'):
                remove_create_watch(w.get('id'))
                await _send(bot, w, f"🔔 `{w.get('plan')}` can no longer be ordered, "
                                    f"so its auto-create alert was removed.")
                continue

            in_stock = [loc for loc in w.get('locations', []) if type_available_at(stype, loc)]
            if in_stock:
                await _try_create(bot, api, w, in_stock, probing=False)
            elif now - (w.get('last_probe') or 0) >= PROBE_MINUTES * 60:
                await _try_create(bot, api, w, list(w.get('locations', [])), probing=True)


async def _find_server(api, name):
    for s in await api.list_servers():
        if s.get('name') == name:
            return s
    return None


async def _try_create(bot, api, watch, locations, probing):
    plan = watch.get('plan')
    # Claim the alert first, so an overlapping check cannot create a second
    # server for it; it is put back only if every location refuses.
    remove_create_watch(watch.get('id'))

    ssh_keys = [k['name'] for k in (await api.list_ssh_keys() or [])] or None
    existing = {s.get('name') for s in await api.list_servers()}

    for location in locations:
        name = _unique(watch.get('name') or auto_name(plan, location), existing)
        result = await api.create_server(
            name, plan, watch.get('image'), location, ssh_keys,
            enable_ipv6=watch.get('ipv6', False),
        )
        if result and result.get('server'):
            await _report_created(bot, watch, result['server'], result.get('root_password'),
                                  location, bool(ssh_keys))
            return
        if is_capacity_refusal(result):
            continue

        # A dropped connection can hide a create that did go through; check
        # before reporting a failure, so the user is not told "failed" while a
        # server they are paying for exists.
        created = await _find_server(api, name)
        if created:
            await _report_created(bot, watch, created, None, location, bool(ssh_keys),
                                  reply_lost=True)
            return
        await _send(bot, watch,
                    f"❌ *Auto-create failed*\n\n"
                    f"`{plan}` in `{location}` was refused: "
                    f"{escape_markdown(api_error_text(result))}\n\nAlert removed.")
        return

    # Every acceptable location refused for capacity: keep waiting.
    now = time.time()
    retry = dict(watch,
                 failures=(watch.get('failures') or 0) + 1,
                 retry_after=now + REFUSAL_PAUSE_MINUTES * 60)
    if probing:
        retry['last_probe'] = now
    _put(retry)
    logger.info("Auto-create of %s refused in %s (%s); waiting",
                plan, ", ".join(locations), "probe" if probing else "flag said in stock")


async def _report_created(bot, watch, server, root_password, location, had_keys,
                          reply_lost=False):
    ip = server.get('public_net', {}).get('ipv4', {}).get('ip', 'N/A')
    text = (
        f"✅ *Server created automatically*\n\n"
        f"🏷 `{server.get('name')}`\n"
        f"🔧 `{watch.get('plan')}` in `{location}` ({escape_markdown(watch.get('account', ''))})\n"
        f"💿 `{watch.get('image')}` · IPv6 {'on' if watch.get('ipv6') else 'off'}\n"
        f"🌐 `{ip}`"
    )
    if root_password:
        text += (f"\n\n🔑 Root password: `{root_password}`\n"
                 f"⚠️ Save it now — it won't be shown again.")
    elif had_keys:
        text += "\n\n🗝 SSH keys were installed."
    elif reply_lost:
        # Hetzner sends the root password only in the create reply, which was
        # lost; without SSH keys there is no other way in.
        text += ("\n\n⚠️ Hetzner's reply was lost, so the root password never "
                 "arrived. Open the server and use 🔑 Reset Password to get one.")
    await _send(bot, watch, text, server_id=server.get('id'), password=root_password)


async def _send(bot, watch, text, server_id=None, password=None):
    rows = []
    if password:
        rows.append([copy_button("📋 Copy password", password)])
    if server_id is not None:
        rows.append([InlineKeyboardButton("🖥 Open Server", callback_data=f"server_{server_id}")])
    keyboard = InlineKeyboardMarkup(rows) if rows else None
    try:
        await bot.send_message(chat_id=watch.get('chat_id'), text=text,
                               reply_markup=keyboard, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Failed to send create-alert message: {e}")
