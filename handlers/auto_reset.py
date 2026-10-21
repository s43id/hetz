"""🌙 Auto Reset: settings of the nightly traffic reset (auto_reset.py).

All buttons: on / off, the default time (hour → minute, Iran time), the
threshold and which servers take part. A server's ⏰ Reset Schedule screen has
its own switch too (``arsrv_<sid>``) and its own time (``arst_<sid>`` → hour →
minute, or back to the default time).
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import auto_reset
from hetzner_api import api_registry
from reset_schedule import fmt_time, iran_now
from utils import traffic_fraction

from .router import router

MAX_SERVERS = 40


def _rows(buttons, width):
    return [buttons[i:i + width] for i in range(0, len(buttons), width)]


async def _all_servers():
    out = []
    for account in api_registry.names():
        out += await api_registry.get(account).list_servers() or []
    return sorted(out, key=lambda s: s.get('name', ''))


async def show_auto_reset(query, context, notice=""):
    s = auto_reset.settings()
    servers = await _all_servers()
    included = [srv for srv in servers if str(srv.get('id')) not in s['excluded']]
    state = "✅ On" if s['enabled'] else "⏸ Off"
    text = f"🌙 *Auto Reset* — {state}\n\n"
    if notice:
        text += notice + "\n\n"
    text += (
        f"Every night at `{auto_reset.time_text(s)}` (Iran time) the bot resets the "
        f"traffic of each running server that has used *{s['threshold']}%* or more, "
        f"or that would pass 100% before the next night at the pace of the last 24 h.\n\n"
        f"Servers: {len(included)} of {len(servers)} take part.\n"
    )
    own = sorted({auto_reset.slot_of(srv.get('id'), s) for srv in included
                  if auto_reset.has_own_time(srv.get('id'), s)})
    if own:
        n = sum(1 for srv in included if auto_reset.has_own_time(srv.get('id'), s))
        text += (f"{n} of them at their own time ({', '.join(own)}); set it on a server's "
                 f"⏰ Reset Schedule.\n")
    if s['enabled']:
        text += f"Next run: `{fmt_time(s['next_run'])}`\n"
    text += (
        "\nResets run one after another; each powers the server off for a few "
        "minutes, twice. You get one message when it starts and one with the results."
    )
    keyboard = [
        [InlineKeyboardButton("⏸ Turn off" if s['enabled'] else "▶️ Turn on",
                              callback_data="artog")],
        [InlineKeyboardButton(f"🕔 Default time: {auto_reset.time_text(s)}", callback_data="arh"),
         InlineKeyboardButton(f"🖥 Servers ({len(included)}/{len(servers)})",
                              callback_data="arsv")],
        [InlineKeyboardButton(f"{'✅ ' if t == s['threshold'] else ''}{t}%",
                              callback_data=f"art_{t}") for t in auto_reset.THRESHOLDS],
        [InlineKeyboardButton("⬅️ Alerts & Schedules", callback_data="watches"),
         InlineKeyboardButton("🏠 Menu", callback_data="start_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def toggle_enabled(query, context):
    on = not auto_reset.settings()['enabled']
    auto_reset.set_enabled(on)
    await show_auto_reset(query, context, "✅ Turned on." if on else "⏸ Turned off.")


async def set_threshold(query, context, percent):
    auto_reset.set_threshold(percent)
    await show_auto_reset(query, context, f"✅ Threshold: {percent}%.")


async def pick_hour(query, context):
    buttons = [InlineKeyboardButton(f"{h:02d}", callback_data=f"arhh_{h}") for h in range(24)]
    keyboard = _rows(buttons, 6)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="ar")])
    await query.edit_message_text(
        f"🌙 *Auto Reset time*\n\nWhich hour, Iran time? Now: "
        f"`{iran_now().strftime('%H:%M')}`",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def pick_minute(query, context, hour):
    buttons = [InlineKeyboardButton(f"{hour:02d}:{m:02d}", callback_data=f"arm_{hour}_{m}")
               for m in auto_reset.MINUTES]
    keyboard = _rows(buttons, 3)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="arh")])
    await query.edit_message_text(
        "🌙 *Auto Reset time*\n\nWhich minute?",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def save_time(query, context, hour, minute):
    auto_reset.set_time(hour, minute)
    await show_auto_reset(query, context, f"✅ Time: {hour:02d}:{minute:02d} (Iran time).")


async def show_servers(query, context):
    s = auto_reset.settings()
    servers = (await _all_servers())[:MAX_SERVERS]
    buttons = []
    for srv in servers:
        on = str(srv.get('id')) not in s['excluded']
        own = (f" · {auto_reset.slot_of(srv.get('id'), s)}"
               if auto_reset.has_own_time(srv.get('id'), s) else "")
        buttons.append(InlineKeyboardButton(
            f"{'✅' if on else '⛔'} {srv.get('name')} · {traffic_fraction(srv) * 100:.0f}%{own}",
            callback_data=f"arx_{srv.get('id')}"))
    keyboard = _rows(buttons, 2)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="ar")])
    text = ("🌙 *Auto Reset — servers*\n\n✅ takes part, ⛔ left out. Tap to switch. "
            "The % is the traffic used now; a time means the server has its own.")
    if not servers:
        text += "\n\nNo servers."
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def toggle_server(query, context, server_id):
    auto_reset.set_included(server_id, not auto_reset.is_included(server_id))
    await show_servers(query, context)


async def toggle_from_schedules(query, context, server_id):
    from .schedules import show_schedules
    on = not auto_reset.is_included(server_id)
    auto_reset.set_included(server_id, on)
    await show_schedules(query, context, server_id,
                         "🌙 Auto reset on for this server." if on
                         else "🌙 Auto reset off for this server.")


async def pick_server_hour(query, context, server_id):
    s = auto_reset.settings()
    buttons = [InlineKeyboardButton(f"{h:02d}", callback_data=f"arsth_{server_id}_{h}")
               for h in range(24)]
    keyboard = _rows(buttons, 6)
    if auto_reset.has_own_time(server_id, s):
        keyboard.append([InlineKeyboardButton(f"↩️ Use the default time ({auto_reset.time_text(s)})",
                                              callback_data=f"arstd_{server_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"rsch_{server_id}")])
    await query.edit_message_text(
        f"🌙 *Auto reset time for this server*\n\nNow: `{auto_reset.slot_of(server_id, s)}`"
        f"{'' if auto_reset.has_own_time(server_id, s) else ' (the default time)'}. "
        f"Which hour, Iran time?",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def pick_server_minute(query, context, server_id, hour):
    buttons = [InlineKeyboardButton(f"{hour:02d}:{m:02d}",
                                    callback_data=f"arstm_{server_id}_{hour}_{m}")
               for m in auto_reset.MINUTES]
    keyboard = _rows(buttons, 3)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"arst_{server_id}")])
    await query.edit_message_text("🌙 *Auto reset time for this server*\n\nWhich minute?",
                                  reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def save_server_time(query, context, server_id, hour, minute):
    from .schedules import show_schedules
    auto_reset.set_server_time(server_id, hour, minute)
    await show_schedules(query, context, server_id,
                         f"🌙 Auto reset time of this server: {hour:02d}:{minute:02d}.")


async def default_server_time(query, context, server_id):
    from .schedules import show_schedules
    auto_reset.set_server_time(server_id, None)
    await show_schedules(query, context, server_id,
                         f"🌙 This server uses the default time ({auto_reset.time_text()}).")


router.add(r"ar", show_auto_reset)
router.add(r"arst_(\d+)", pick_server_hour, int)
router.add(r"arsth_(\d+)_(\d+)", pick_server_minute, int, int)
router.add(r"arstm_(\d+)_(\d+)_(\d+)", save_server_time, int, int, int)
router.add(r"arstd_(\d+)", default_server_time, int)
router.add(r"artog", toggle_enabled)
router.add(r"art_(\d+)", set_threshold, int)
router.add(r"arh", pick_hour)
router.add(r"arhh_(\d+)", pick_minute, int)
router.add(r"arm_(\d+)_(\d+)", save_time, int, int)
router.add(r"arsv", show_servers)
router.add(r"arx_(\d+)", toggle_server, int)
router.add(r"arsrv_(\d+)", toggle_from_schedules, int)
