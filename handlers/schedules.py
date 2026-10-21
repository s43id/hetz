"""⏰ Reset Schedule: scheduled traffic resets of one server, in Iran time.

Picking a schedule is all buttons: repeat → day (date, weekday or day of the
month; none for daily) → hour → minute, which saves it.
"""
from datetime import timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import auto_reset

from reset_schedule import (
    add_schedule, describe, fmt_time, iran_now, list_schedules, remove_schedule,
    REPEATS, WEEKDAYS,
)

from .router import router
from .context import _current_account, _resolve_server
from .ui import send_not_found

MINUTES = (0, 10, 20, 30, 40, 50)


def _rows(buttons, width):
    return [buttons[i:i + width] for i in range(0, len(buttons), width)]


async def show_schedules(query, context, server_id, notice=""):
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    items = list_schedules(server_id)
    text = (
        f"⏰ *Reset Schedule* — `{server.get('name')}`\n\n"
        f"Times are Iran time (Tehran). Now: `{iran_now().strftime('%a %d %b %H:%M')}`\n\n"
    )
    if notice:
        text += notice + "\n\n"
    keyboard = []
    if items:
        for s in items:
            text += f"• {describe(s)} — next `{fmt_time(s['next_run'])}`\n"
            keyboard.append([InlineKeyboardButton(
                f"❌ {describe(s)}", callback_data=f"rschdel_{s['id']}_{server_id}")])
    else:
        text += "No scheduled resets.\n"
    ar = auto_reset.settings()
    included = str(server_id) not in ar['excluded']
    if not ar['enabled']:
        text += "\n🌙 Auto reset is off for every server.\n"
    elif included:
        own = auto_reset.has_own_time(server_id, ar)
        text += (f"\n🌙 Auto reset: on — nightly at {auto_reset.slot_of(server_id, ar)} "
                 f"({'its own time' if own else 'the default time'}) when traffic "
                 f"is at {ar['threshold']}%+ or on pace to pass 100%.\n")
    else:
        text += "\n🌙 Auto reset: off for this server.\n"
    text += (
        "\nEach reset powers the server off for a few minutes, twice. If its plan "
        "has no room to move back to, the server stays up on the other plan and "
        "the bot moves it back automatically once the plan is in stock."
    )
    keyboard.append([InlineKeyboardButton("➕ Add Schedule", callback_data=f"rschn_{server_id}")])
    keyboard.append([
        InlineKeyboardButton(
            ("🌙 Auto reset: on" if included else "🌙 Auto reset: off")
            + (" (all off)" if not ar['enabled'] else ""),
            callback_data=f"arsrv_{server_id}"),
        InlineKeyboardButton("⚙️ Auto reset settings", callback_data="ar"),
    ])
    keyboard.append([InlineKeyboardButton(
        f"🕔 Auto reset time: {auto_reset.slot_of(server_id, ar)}"
        + ("" if auto_reset.has_own_time(server_id, ar) else " (default)"),
        callback_data=f"arst_{server_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Server", callback_data=f"server_{server_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def pick_repeat(query, context, server_id):
    keyboard = [
        [InlineKeyboardButton("1️⃣ Once", callback_data=f"rschr_{server_id}_o"),
         InlineKeyboardButton("📅 Every day", callback_data=f"rschr_{server_id}_d")],
        [InlineKeyboardButton("🗓 Every week", callback_data=f"rschr_{server_id}_w"),
         InlineKeyboardButton("📆 Every month", callback_data=f"rschr_{server_id}_m")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"rsch_{server_id}")],
    ]
    await query.edit_message_text(
        "⏰ *New Reset Schedule*\n\nHow often?",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def pick_day(query, context, server_id, repeat):
    if repeat == "d":
        await pick_hour(query, context, server_id, repeat, "0")
        return
    if repeat == "o":
        today = iran_now().date()
        days = [today + timedelta(days=i) for i in range(14)]
        labels = ["Today", "Tomorrow"] + [d.strftime("%a %d %b") for d in days[2:]]
        buttons = [InlineKeyboardButton(label, callback_data=f"rschday_{server_id}_o_{d:%Y%m%d}")
                   for label, d in zip(labels, days)]
        keyboard, question = _rows(buttons, 3), "Which day? (Iran calendar days)"
    elif repeat == "w":
        buttons = [InlineKeyboardButton(name, callback_data=f"rschday_{server_id}_w_{num}")
                   for num, name in WEEKDAYS]
        keyboard, question = _rows(buttons, 3), "Which day of the week?"
    else:
        buttons = [InlineKeyboardButton(str(d), callback_data=f"rschday_{server_id}_m_{d}")
                   for d in range(1, 32)]
        keyboard = _rows(buttons, 7)
        question = ("Which day of the month? In a shorter month a later day "
                    "runs on its last day.")
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"rschn_{server_id}")])
    await query.edit_message_text(
        f"⏰ *New Reset Schedule* ({REPEATS[repeat]})\n\n{question}",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def pick_hour(query, context, server_id, repeat, day):
    buttons = [InlineKeyboardButton(f"{h:02d}", callback_data=f"rschh_{server_id}_{repeat}_{day}_{h}")
               for h in range(24)]
    keyboard = _rows(buttons, 6)
    keyboard.append([InlineKeyboardButton(
        "⬅️ Back", callback_data=f"rschr_{server_id}_{repeat}" if repeat != "d" else f"rschn_{server_id}")])
    await query.edit_message_text(
        f"⏰ *New Reset Schedule* ({REPEATS[repeat]})\n\n"
        f"Which hour, Iran time? Now: `{iran_now().strftime('%H:%M')}`",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def pick_minute(query, context, server_id, repeat, day, hour):
    buttons = [InlineKeyboardButton(f"{hour:02d}:{m:02d}",
                                    callback_data=f"rschm_{server_id}_{repeat}_{day}_{hour}_{m}")
               for m in MINUTES]
    keyboard = _rows(buttons, 3)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"rschday_{server_id}_{repeat}_{day}")])
    await query.edit_message_text(
        f"⏰ *New Reset Schedule* ({REPEATS[repeat]})\n\nWhich minute?",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def save_schedule(query, context, server_id, repeat, day, hour, minute):
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    sched, error = add_schedule(
        _current_account(context), server_id, server.get("name", str(server_id)),
        query.message.chat_id, repeat, day, hour, minute)
    if error:
        notice = f"⚠️ Not saved: {error}."
    else:
        notice = f"✅ Saved: {describe(sched)}. First run `{fmt_time(sched['next_run'])}`."
    await show_schedules(query, context, server_id, notice)


async def delete_schedule(query, context, sched_id, server_id):
    remove_schedule(sched_id)
    await show_schedules(query, context, server_id)


router.add(r"rsch_(\d+)", show_schedules, int)
router.add(r"rschn_(\d+)", pick_repeat, int)
router.add(r"rschr_(\d+)_([odwm])", pick_day, int)
router.add(r"rschday_(\d+)_([odwm])_(\d+)", pick_hour, int)
router.add(r"rschh_(\d+)_([odwm])_(\d+)_(\d+)", pick_minute, int, None, None, int)
router.add(r"rschm_(\d+)_([odwm])_(\d+)_(\d+)_(\d+)", save_schedule, int, None, int, int, int)
router.add(r"rschdel_([0-9a-f]+)_(\d+)", delete_schedule, None, int)
