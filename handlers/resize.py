"""Change a server's plan: pick family/type, adjacent (prev/next) plan, apply."""
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from hetzner_api import api_registry
from utils import (
    server_location, server_datacenter, escape_markdown,
    type_available_at, type_has_location_data, type_family as _type_family,
    server_disk_gb as _server_disk_gb,
)
from plan_watch import (
    add_watch, remove_watch, remove_watches_for, is_watching, CHECK_INTERVAL_MINUTES,
)
from server_manager import change_plan

from .router import router
from .context import _get_api, _current_account, _resolve_server
from .ui import send_not_found
from .pricing import _server_type_price

logger = logging.getLogger(__name__)

# Retries for a plan change Hetzner refuses on capacity. Short on
# purpose: the server is powered off while they run.
CHANGE_ATTEMPTS = 3
CHANGE_RETRY_SECONDS = 15


_FAMILY_LABEL = {
    "cx": "CX — Intel shared",
    "cpx": "CPX — AMD shared",
    "cax": "CAX — ARM64 Ampere",
    "ccx": "CCX — Dedicated vCPU",
}

_PLAN_LADDERS = [
    ("cx11", "cx21", "cx31", "cx41", "cx51"),
    ("cx22", "cx32", "cx42", "cx52"),
    ("cx23", "cx33", "cx43", "cx53"),
    ("cpx11", "cpx21", "cpx31", "cpx41", "cpx51"),
    ("cpx12", "cpx22", "cpx32", "cpx42", "cpx52"),
    ("cax11", "cax21", "cax31", "cax41"),
    ("ccx13", "ccx23", "ccx33", "ccx43", "ccx53", "ccx63"),
]


_type_price = _server_type_price


async def _resize_options(server, api):
    """Every plan this server could be moved to, plus which are out of stock.

    Only two constraints are hard: the architecture cannot change (the disk
    image would not boot) and deprecated plans cannot be targeted. Stock is
    advisory — Hetzner's flag has proved incomplete, and filtering on it hid
    plans that do in fact work — so return all compatible plans and merely
    flag the ones not currently orderable here. Hetzner gets the final say,
    and a refused migration powers the server back on.

    Returns ``(plans, offlist_ids)``.
    """
    types = await api.get_server_types()
    cur = server.get("server_type", {})
    location = server_location(server)
    plans = [
        t for t in types
        if t.get("architecture") == cur.get("architecture")
        and not t.get("deprecation")
        and t.get("name") != cur.get("name")
    ]

    # Without a location, or without the locations array, flag nothing rather
    # than flag everything.
    if location and any(type_has_location_data(t) for t in plans):
        offlist = {t.get("id") for t in plans if not type_available_at(t, location)}
    else:
        offlist = set()
    return plans, offlist


def _plan_sort_key(stype, location):
    return (
        _type_price(stype, location),
        stype.get("cores", 0),
        stype.get("memory", 0),
        stype.get("disk", 0),
        stype.get("name", ""),
    )


def _adjacent_ladder_name(current_name, direction):
    for ladder in _PLAN_LADDERS:
        if current_name not in ladder:
            continue
        index = ladder.index(current_name)
        target_index = index + (1 if direction == "next" else -1)
        if 0 <= target_index < len(ladder):
            return ladder[target_index]
        return None
    return None


def _watch_scope(server):
    """What a stock watch is keyed on: the datacenter name, or the location
    if the server payload omits the datacenter (which some do). Stock is read
    per location, and plan_watch.watch_location reduces either to that.
    """
    return server_datacenter(server) or server_location(server)


async def _adjacent_resize_target(server, api, direction):
    loc = server_location(server)
    cur = server.get("server_type", {})
    cur_name = cur.get("name")
    cur_family = _type_family(cur_name or "")
    types = await api.get_server_types()
    type_by_name = {t.get("name"): t for t in types if t.get("name")}
    plans, _offlist = await _resize_options(server, api)

    # The hand-written ladder gives the natural neighbour. Offer it even when
    # it is flagged out of stock: the flag under-reports, and a refused
    # migration is reported and the server powered back on.
    ladder_target_name = _adjacent_ladder_name(cur_name, direction)
    if ladder_target_name:
        ladder_target = type_by_name.get(ladder_target_name)
        if (
            ladder_target
            and ladder_target.get("architecture") == cur.get("architecture")
            and not ladder_target.get("deprecation")
        ):
            return ladder_target, None

    candidates = [
        t for t in plans
        if _type_family(t.get("name", "")) == cur_family
    ]
    ladder = candidates + [type_by_name.get(cur_name, cur)]
    ladder = [t for t in ladder if t.get("name")]
    ladder.sort(key=lambda t: _plan_sort_key(t, loc))

    cur_index = next((i for i, t in enumerate(ladder) if t.get("name") == cur_name), None)
    if cur_index is None:
        return None, "Current plan was not found in the available migration list."

    offset = 1 if direction == "next" else -1
    target_index = cur_index + offset
    if 0 <= target_index < len(ladder):
        return ladder[target_index], None

    word = "largest" if direction == "next" else "smallest"
    return None, f"This server is already on the {word} plan in this family."


def _watch_button(context, server, server_id, plan):
    """Toggle button for a stock alert on ``plan`` where this server lives."""
    account = _current_account(context)
    dc_name = _watch_scope(server)
    if is_watching(account, dc_name, plan, server_id):
        return InlineKeyboardButton(
            f"🔕 Stop watching {plan}",
            callback_data=f"pwdel_{server_id}_{plan}",
        )
    return InlineKeyboardButton(
        f"🔔 Auto-switch when {plan} is back",
        callback_data=f"pwadd_{server_id}_{plan}",
    )


async def _resolve_watch_scope(context, server, server_id):
    """Where this server lives, re-reading it if the payload we have is thin.

    A cached or partial server object can arrive with no datacenter details
    at all, which would leave nothing to key a watch on. Re-read the server
    uncached, then fall back to finding it in the account's server list,
    before admitting defeat — and log what we actually got if we do.
    """
    scope = _watch_scope(server)
    if scope:
        return scope, server

    api = _get_api(context)
    candidates = []
    fresh = await api.get_server(server_id, fresh=True)
    if fresh:
        candidates.append(fresh)
    candidates += [s for s in await api.list_servers() if s.get("id") == server_id]

    for candidate in candidates:
        scope = _watch_scope(candidate)
        if scope:
            return scope, candidate

    probe = candidates[0] if candidates else server
    logger.warning(
        "Server %s has no datacenter/location: datacenter=%r keys=%s",
        server_id, probe.get("datacenter"), sorted(probe.keys()),
    )
    return "", server


async def _toggle_plan_watch(query, context, server_id, plan, enable):
    # Resolve through the owning account: this button often outlives the
    # screen it came from, by which time another account may be selected.
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    account = _current_account(context)
    dc_name, server = await _resolve_watch_scope(context, server, server_id)
    if not dc_name:
        # An alert here would be invisible: button_handler already answered
        # this callback, and Telegram only shows the first response.
        await query.edit_message_text(
            "⚠️ Could not determine this server's datacenter or location, "
            "so there is nothing to watch.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🖥 Back to Server",
                                       callback_data=f"server_{server_id}")]]
            ),
        )
        return

    if enable:
        added = add_watch(account, dc_name, plan, server_id, query.message.chat_id)
        logger.info("Plan watch %s: %s/%s/%s server=%s",
                    "added" if added else "already present",
                    account, dc_name, plan, server_id)
        body = (
            f"🔔 *Watching* `{plan}`\n\n"
            f"Datacenter: `{dc_name}`\n"
            f"Server: `{server.get('name')}`\n\n"
            f"As soon as `{plan}` is back in stock here, the bot switches this "
            f"server to it automatically — it is powered off briefly for the "
            f"change — and tells you. Checked every {CHECK_INTERVAL_MINUTES} "
            f"minutes.\n\nAll alerts: 🔔 Alerts & Schedules in the main menu."
        )
    else:
        remove_watch(account, dc_name, plan, server_id)
        body = f"🔕 Stopped watching `{plan}` in `{dc_name}`."

    keyboard = [
        [_watch_button(context, server, server_id, plan)],
        [InlineKeyboardButton("⬅️ Back to Server", callback_data=f"server_{server_id}")],
    ]
    await query.edit_message_text(
        body, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
    )


async def plan_watch_open(query, context, server_id, plan, account):
    """Jump straight to the resize confirmation from a stock alert."""
    if account in api_registry.names():
        context.user_data['account'] = account
    await resize_confirm(query, context, server_id, plan)


async def resize_adjacent_plan(query, context, server_id, direction):
    if direction not in {"prev", "next"}:
        await query.edit_message_text("⚠️ Unknown plan direction.")
        return

    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    api = _get_api(context)

    target, error = await _adjacent_resize_target(server, api, direction)
    if error:
        keyboard = [
            [InlineKeyboardButton("⬅️ Back to Server", callback_data=f"server_{server_id}")]
        ]
        await query.edit_message_text(
            f"⚖️ *Plan Migration*\n\n{error}",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    await resize_confirm(query, context, server_id, target["name"])


async def resize_pick_family(query, context, server_id):
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    api = _get_api(context)
    candidates, _offlist = await _resize_options(server, api)
    if not candidates:
        await query.edit_message_text(
            "⚠️ No other plan matches this server's architecture.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back to Server", callback_data=f"server_{server_id}")]]
            ),
            parse_mode="Markdown",
        )
        return
    families = sorted({_type_family(t["name"]) for t in candidates})
    keyboard = [
        [
            InlineKeyboardButton("⬇️ Previous Plan", callback_data=f"resizenav_{server_id}_prev"),
            InlineKeyboardButton("⬆️ Next Plan", callback_data=f"resizenav_{server_id}_next"),
        ],
    ] + [
        [InlineKeyboardButton(
            _FAMILY_LABEL.get(f, f.upper()),
            callback_data=f"resizef_{server_id}_{f}",
        )]
        for f in families
    ]
    keyboard.append([InlineKeyboardButton("⬅️ Back to Server", callback_data=f"server_{server_id}")])
    cur = server.get("server_type", {})
    await query.edit_message_text(
        f"⚖️ *Change Plan*\n\n"
        f"Server: `{server.get('name')}`\n"
        f"Current: `{cur.get('name')}` ({cur.get('cores')}C / {cur.get('memory'):.0f}GB / {cur.get('disk')}GB)\n\n"
        f"Choose a CPU family:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def resize_pick_type(query, context, server_id, family):
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    api = _get_api(context)
    loc = server_location(server)
    plans, offlist = await _resize_options(server, api)
    candidates = [t for t in plans if _type_family(t["name"]) == family]
    candidates.sort(key=lambda t: (t.get("cores", 0), t.get("memory", 0)))
    cur = server.get("server_type", {})
    cur_disk = _server_disk_gb(server)

    keyboard = []
    any_offlist = any_smaller = False
    for t in candidates:
        arrow = "🔼" if t.get("memory", 0) > cur.get("memory", 0) else "🔽"
        marks = ""
        if t.get("id") in offlist:
            marks += "🔓"
            any_offlist = True
        if t.get("disk", 0) < cur_disk:
            marks += "💾"
            any_smaller = True
        keyboard.append([InlineKeyboardButton(
            f"{arrow}{marks} {t['name']} | {t.get('cores')}C / {t.get('memory'):.0f}GB / {t.get('disk')}GB | €{_type_price(t, loc):.2f}",
            callback_data=f"resizet_{server_id}_{t['name']}",
        )])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"resize_{server_id}")])

    text = (
        f"⚖️ *Change Plan* — {_FAMILY_LABEL.get(family, family.upper())}\n\n"
        f"Current: `{cur.get('name')}`\n\n"
    )
    text += "Choose the new plan:" if candidates else "No plan in this family matches this server."
    if any_offlist:
        text += "\n\n🔓 = Hetzner reports it out of stock here; it may still work, but can be refused."
    if any_smaller:
        text += "\n💾 = smaller disk than the current plan."
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def resize_confirm(query, context, server_id, new_type_name):
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    api = _get_api(context)
    types = await api.get_server_types()
    new_type = next((t for t in types if t.get("name") == new_type_name), None)
    if not new_type:
        await query.edit_message_text(f"⚠️ Plan `{new_type_name}` not found.",
                                      parse_mode="Markdown")
        return
    cur = server.get("server_type", {})
    loc = server_location(server)

    # Hetzner never shrinks a disk, so this one is certain to be refused —
    # stop here rather than power the server off to find out.
    disk_gb = _server_disk_gb(server)
    if new_type.get("disk", 0) < disk_gb:
        await query.edit_message_text(
            f"💾 *Disk too small*\n\n"
            f"`{new_type['name']}` has a {new_type.get('disk')} GB disk, but "
            f"`{server.get('name')}` is using {disk_gb} GB.\n\n"
            f"Hetzner cannot shrink a disk, so this change would be refused. "
            f"Pick a plan with at least {disk_gb} GB.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚖️ Other Plans", callback_data=f"resize_{server_id}")],
                [InlineKeyboardButton("🖥 Back to Server", callback_data=f"server_{server_id}")],
            ]),
            parse_mode="Markdown",
        )
        return

    _plans, offlist = await _resize_options(server, api)
    text = (
        f"⚖️ *Confirm Plan Change*\n\n"
        f"Server: `{server.get('name')}`\n\n"
        f"Current: `{cur.get('name')}` — {cur.get('cores')}C / {cur.get('memory'):.0f}GB / "
        f"{cur.get('disk')}GB — €{_type_price(cur, loc):.2f}\n"
        f"New: `{new_type['name']}` — {new_type.get('cores')}C / {new_type.get('memory'):.0f}GB / "
        f"{new_type.get('disk')}GB — €{_type_price(new_type, loc):.2f}\n\n"
        f"⚠️ The server will be powered off during the change.\n"
        f"💡 If you were grandfathered into a cheaper rate, moving plans puts "
        f"you on the current list price.\n"
    )
    keyboard = [[InlineKeyboardButton(
        "✅ Change (CPU/RAM only — can downgrade later)",
        callback_data=f"resizego_{server_id}_{new_type_name}_0",
    )]]
    if new_type.get("disk", 0) > disk_gb:
        text += "\n💡 Upgrading the disk too is permanent — you can never downgrade afterwards."
        keyboard.append([InlineKeyboardButton(
            "📈 Change + upgrade disk (PERMANENT)",
            callback_data=f"resizego_{server_id}_{new_type_name}_1",
        )])
    if new_type.get("id") in offlist:
        # Worth trying anyway — the stock flag has proved incomplete — but say
        # so up front, and offer an alert for when it comes back in stock.
        text += (
            f"\n\n🔓 Hetzner reports `{new_type['name']}` out of stock in "
            f"`{_watch_scope(server)}`. The change will likely be refused; if it "
            f"is, the server is powered back on unchanged. Tap 🔔 to switch "
            f"automatically once it is back."
        )
        keyboard.append([_watch_button(context, server, server_id, new_type["name"])])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=f"server_{server_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def resize_go(query, context, server_id, new_type_name, upgrade_disk):
    steps = []

    async def log(line):
        steps.append(escape_markdown(line))
        try:
            await query.edit_message_text("⚖️ *Changing Plan*\n\n" + "\n".join(steps),
                                          parse_mode="Markdown")
        except Exception:
            pass

    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    api = _get_api(context)

    outcome = await change_plan(
        api, server_id, new_type_name, upgrade_disk=upgrade_disk, log=log,
        attempts=CHANGE_ATTEMPTS, retry_seconds=CHANGE_RETRY_SECONDS,
    )

    body = "⚖️ *Changing Plan*\n\n" + "\n".join(steps)
    keyboard = []
    if outcome["ok"]:
        # Any alert waiting for this plan is fulfilled now.
        remove_watches_for(server_id, new_type_name)
        body += "\n\n🎉 *Done!*"
    else:
        if outcome["capacity"]:
            body += (
                f"\n\n💡 `{new_type_name}` has no capacity here right now. "
                f"Nothing changed — the server is still on "
                f"`{server.get('server_type', {}).get('name')}`."
            )
            keyboard.append([_watch_button(context, server, server_id, new_type_name)])
        keyboard.append([InlineKeyboardButton("⚖️ Other Plans", callback_data=f"resize_{server_id}")])
    keyboard.append([InlineKeyboardButton("🖥 Back to Server", callback_data=f"server_{server_id}")])
    await query.edit_message_text(
        body, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
    )


router.add(r"resize_(\d+)", resize_pick_family, int)
router.add(r"resizef_(\d+)_([^_]+)", resize_pick_type, int)
router.add(r"resizet_(\d+)_([^_]+)", resize_confirm, int)
router.add(r"resizenav_(\d+)_(prev|next)", resize_adjacent_plan, int)
router.add(
    r"resizego_(\d+)_([^_]+)_([01])",
    lambda q, c, sid, stype, disk: resize_go(q, c, sid, stype, disk == "1"),
    int,
)
router.add(
    r"pwadd_(\d+)_([^_]+)",
    lambda q, c, sid, plan: _toggle_plan_watch(q, c, sid, plan, True),
    int,
)
router.add(
    r"pwdel_(\d+)_([^_]+)",
    lambda q, c, sid, plan: _toggle_plan_watch(q, c, sid, plan, False),
    int,
)
# Account comes last so a name containing underscores still parses.
router.add(r"pwgo_(\d+)_([^_]+)_(.+)", plan_watch_open, int)
