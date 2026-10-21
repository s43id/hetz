"""Stock alerts that switch a server to a plan once it is back in stock.

A watch pins (account, location, plan) plus the server to move. A scheduled
job re-reads Hetzner's per-location stock (server_types[].locations — the
replacement for the /datacenters endpoint Hetzner removed) and, the first time
the plan shows as available, switches the server to it automatically and
reports the outcome.

Hetzner's stock flag has proved unreliable, so an automatic switch can still
be refused. Each refusal backs the watch off, and after a few it stops
switching automatically and only notifies — otherwise a flag stuck on
"available" would power the server off every couple of minutes. The
exception is a return watch (``kind='return'``), left by a traffic reset that
could not move the server back to its own plan: it keeps switching
automatically, backed off, until the server is home.
"""
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from hetzner_api import api_registry
from server_manager import change_plan, is_busy
import automation
from utils import escape_markdown, type_available_at

logger = logging.getLogger(__name__)

WATCH_FILE = Path('plan_watches.json')

# How often check_plan_watches runs. Stock for a popular plan can vanish
# within minutes, so poll often; a check with no watches costs nothing, and
# one with watches is a single uncached /server_types call per account.
CHECK_INTERVAL_MINUTES = 2

# An automatic switch is retried once, briefly — the server is off meanwhile.
AUTO_CHANGE_ATTEMPTS = 2
AUTO_CHANGE_RETRY_SECONDS = 10

# After this many refused automatic switches, fall back to notify-only.
AUTO_REFUSALS_BEFORE_NOTIFY = 4
# Wait after each refusal: this many minutes times the refusal count, capped.
BACKOFF_MINUTES = 15
BACKOFF_CAP_MINUTES = 120


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
        logger.error(f"Failed to save plan watches: {e}")


def _same(w, account, datacenter, plan, server_id):
    return (
        w.get('account') == account
        and w.get('datacenter') == datacenter
        and w.get('plan') == plan
        and w.get('server_id') == server_id
    )


def is_watching(account, datacenter, plan, server_id):
    return any(_same(w, account, datacenter, plan, server_id) for w in _load())


def add_watch(account, datacenter, plan, server_id, chat_id, auto=True, kind=None):
    """Register interest; returns False if this watch already exists.

    ``kind='return'`` marks a watch that takes a server back to its own plan
    after a traffic reset could not: it keeps switching automatically
    however often Hetzner refuses (see _auto_switch). An existing watch for
    the same plan is turned into one.
    """
    watches = _load()
    for w in watches:
        if _same(w, account, datacenter, plan, server_id):
            if kind and w.get('kind') != kind:
                w.update(kind=kind, auto=True)
                _save(watches)
            return False
    watch = {
        'account': account,
        'datacenter': datacenter,
        'plan': plan,
        'server_id': server_id,
        'chat_id': chat_id,
        'auto': auto,
        'failures': 0,
        'retry_after': 0,
    }
    if kind:
        watch['kind'] = kind
    watches.append(watch)
    _save(watches)
    return True


def is_return(watch):
    return watch.get('kind') == 'return'


def home_plan_of(account, server_id):
    """The plan a server should go back to, from its watches (None if none).

    A return watch (left by a traffic reset) wins; any other switch watch is
    also a plan the admin wants the server on.
    """
    mine = [w for w in _load()
            if w.get('server_id') == server_id and w.get('account') == account]
    mine.sort(key=lambda w: not is_return(w))
    return mine[0].get('plan') if mine else None


def remove_watch(account, datacenter, plan, server_id):
    watches = _load()
    kept = [w for w in watches if not _same(w, account, datacenter, plan, server_id)]
    if len(kept) == len(watches):
        return False
    _save(kept)
    return True


def list_watches():
    return _load()


def remove_watches_for(server_id, plan):
    """Drop every watch for this server and plan; returns how many."""
    watches = _load()
    kept = [w for w in watches
            if not (w.get('server_id') == server_id and w.get('plan') == plan)]
    if len(kept) != len(watches):
        _save(kept)
    return len(watches) - len(kept)


def watch_location(watch):
    """The location a watch applies to.

    Watches made before Hetzner removed datacenters stored a datacenter name
    such as "fsn1-dc14"; its location is the part before the dash.
    """
    return (watch.get('datacenter') or '').split('-')[0]


def _drop(watch):
    remove_watch(watch.get('account'), watch.get('datacenter'),
                 watch.get('plan'), watch.get('server_id'))


def _put(watch):
    """Store a watch again (after claiming it), replacing any equal one."""
    _drop(watch)
    watches = _load()
    watches.append(watch)
    _save(watches)


async def check_plan_watches(bot):
    """Act on every watch whose plan is back in stock where its server is."""
    watches = _load()
    if not watches or automation.is_paused():
        return   # paused: the watches wait, untouched

    now = time.time()
    by_account = {}
    for w in watches:
        if (w.get('retry_after') or 0) > now:
            continue
        by_account.setdefault(w.get('account'), []).append(w)

    for account, items in by_account.items():
        api = api_registry.get(account)
        if not api:
            logger.warning("Dropping %d watch(es) for unknown account '%s'",
                           len(items), account)
            for w in items:
                _drop(w)
            continue
        try:
            types = await api.get_server_types(fresh=True)
        except Exception as e:
            # Keep the watches; a transient API failure must not lose them.
            logger.error(f"Plan watch check failed for account '{account}': {e}")
            continue

        by_name = {t.get('name'): t for t in types if t.get('name')}
        for w in items:
            if is_busy(w.get('server_id')):
                continue   # a reset or plan change is running; look again next time
            stype = by_name.get(w.get('plan'))
            if not stype or not type_available_at(stype, watch_location(w)):
                continue
            if w.get('auto', True):
                await _auto_switch(bot, api, w, stype)
            else:
                _drop(w)
                await _notify_available(bot, w)


async def _auto_switch(bot, api, watch, stype):
    plan = watch.get('plan')
    server_id = watch.get('server_id')
    location = watch_location(watch)

    # Claim the watch before the switch: it takes minutes, and the next check
    # must not start a second one for the same server.
    _drop(watch)

    server = await api.get_server(server_id, fresh=True)
    if not server:
        await _send(bot, watch,
                    f"🔔 `{plan}` is back in stock in `{location}`, but the server "
                    f"it was for no longer exists. Alert removed.")
        return
    name = server.get('name', str(server_id))
    current = server.get('server_type', {}).get('name')
    if current == plan:
        logger.info("Server %s is already on %s; dropping its watch", server_id, plan)
        return

    disk = server.get('primary_disk_size') or server.get('server_type', {}).get('disk') or 0
    if (stype.get('disk') or 0) < disk:
        await _send(bot, watch,
                    f"🔔 `{plan}` is back in stock, but its {stype.get('disk')} GB disk is "
                    f"smaller than `{name}`'s {disk} GB and Hetzner cannot shrink a disk. "
                    f"Alert removed.", server_id=server_id)
        return

    logger.info("Plan watch: %s back in %s, switching %s from %s", plan, location, name, current)
    outcome = await change_plan(api, server_id, plan,
                                attempts=AUTO_CHANGE_ATTEMPTS,
                                retry_seconds=AUTO_CHANGE_RETRY_SECONDS)

    if outcome['ok']:
        if is_return(watch):
            await _send(bot, watch,
                        f"↩️ *Back on the original plan*\n\n"
                        f"`{name}` is on `{plan}` again (it was left on `{current}` "
                        f"by a traffic reset), in `{location}`.\n"
                        f"It was briefly powered off for the change.",
                        server_id=server_id)
            return
        await _send(bot, watch,
                    f"✅ *Switched automatically*\n\n"
                    f"`{name}` is now on `{plan}` (was `{current}`), in `{location}`.\n"
                    f"It was briefly powered off for the change.",
                    server_id=server_id)
        return

    if outcome['capacity']:
        failures = (watch.get('failures') or 0) + 1
        delay = min(BACKOFF_MINUTES * failures, BACKOFF_CAP_MINUTES)
        retry = dict(watch, failures=failures, retry_after=time.time() + delay * 60)
        # A return watch never goes quiet: the server costs more on the
        # other plan every hour, so keep trying (at most every BACKOFF_CAP).
        if failures >= AUTO_REFUSALS_BEFORE_NOTIFY and not is_return(watch):
            retry['auto'] = False
            _put(retry)
            await _send(bot, watch,
                        f"⚠️ *Auto-switch paused*\n\n"
                        f"Hetzner listed `{plan}` as in stock {failures} times, but "
                        f"refused the switch each time — and every try powers "
                        f"`{name}` off briefly. I have stopped switching it "
                        f"automatically; next time it shows as in stock I will only "
                        f"tell you.", server_id=server_id)
        else:
            _put(retry)
            logger.info("Auto-switch of %s to %s refused (%d); retrying in %d min",
                        name, plan, failures, delay)
        return

    await _send(bot, watch,
                f"❌ *Auto-switch failed*\n\n"
                f"`{name}` could not be moved to `{plan}`: "
                f"{escape_markdown(outcome['error'])}\n\n"
                f"The server is unchanged on `{current}`. Alert removed.",
                server_id=server_id)


async def _send(bot, watch, text, server_id=None):
    keyboard = []
    if server_id is not None:
        keyboard.append([InlineKeyboardButton("🖥 Open Server", callback_data=f"server_{server_id}")])
    try:
        await bot.send_message(
            chat_id=watch.get('chat_id'),
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
            parse_mode='Markdown',
        )
    except Exception as e:
        logger.error(f"Failed to send plan-watch message: {e}")


async def _notify_available(bot, watch):
    """Notify-only path, for watches that stopped switching automatically."""
    plan = watch.get('plan')
    server_id = watch.get('server_id')
    account = watch.get('account')
    keyboard = [
        [InlineKeyboardButton(
            f"⚖️ Change to {plan} now",
            callback_data=f"pwgo_{server_id}_{plan}_{account}",
        )],
        [InlineKeyboardButton("🖥 Open Server", callback_data=f"server_{server_id}")],
    ]
    try:
        await bot.send_message(
            chat_id=watch.get('chat_id'),
            text=(
                f"🔔 *Plan Available*\n\n"
                f"`{plan}` is in stock again in `{watch_location(watch)}`.\n\n"
                f"Stock can go quickly — migrate now if you still want it."
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown',
        )
    except Exception as e:
        logger.error(f"Failed to send plan-available alert: {e}")


def format_retry(watch):
    """'next try 14:30' for a backed-off watch, else ''."""
    ts = watch.get('retry_after') or 0
    if ts <= time.time():
        return ''
    return f"next try {datetime.fromtimestamp(ts).strftime('%H:%M')}"
