"""Alerts & Schedules: see and cancel every stock alert and scheduled reset."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from hetzner_api import api_registry
from plan_watch import (
    list_watches, remove_watches_for, watch_location, format_retry, is_return,
    CHECK_INTERVAL_MINUTES,
)
from reset_schedule import list_schedules, remove_schedule, describe, fmt_time
from create_watch import list_create_watches, remove_create_watch
from utils import escape_markdown
import auto_reset
import automation

from .router import router


async def show_watches(query, context):
    watches = list_watches()
    creates = list_create_watches()
    schedules = list_schedules()
    keyboard = []

    if not watches and not creates and not schedules:
        text = (
            "🔔 *Alerts & Schedules*\n\n"
            "No alerts or schedules set.\n\n"
            "• To move a server to a sold-out plan, tap 🔔 on it in ⚖️ Change Plan.\n"
            "• To get a new server on a sold-out plan, tap 🔔 on it while "
            "creating a server.\n"
            "• To reset a server's traffic at set times, open it → ⏰ Reset Schedule.\n"
            "• 🌙 Auto Reset (button above) resets every server near its limit each night.\n\n"
            "Stock alerts act on their own as soon as the plan is back in stock."
        )
    else:
        lines = []
        if creates:
            lines.append("➕ *Create when in stock*")
        for c in creates:
            name = c.get('name') or "auto name"
            lines.append(
                f"• `{c.get('plan')}` in {' or '.join(f'`{l}`' for l in c.get('locations', []))} "
                f"({escape_markdown(c.get('account', '?'))}) — `{c.get('image')}`, "
                f"IPv6 {'on' if c.get('ipv6') else 'off'}, {escape_markdown(name)}"
                + (f", {c['failures']} refused" if c.get('failures') else "")
            )
            keyboard.append([InlineKeyboardButton(
                f"❌ Cancel new {c.get('plan')} in {'/'.join(c.get('locations', []))}",
                callback_data=f"cwx_{c.get('id')}",
            )])
        if watches:
            lines.append(("\n" if creates else "") + "⚖️ *Switch when in stock*")
        for w in watches:
            server_id, plan = w.get('server_id'), w.get('plan')
            api = api_registry.get(w.get('account'))
            server = await api.get_server(server_id) if api else None
            name = server.get('name', str(server_id)) if server else f"#{server_id} (gone)"
            current = (server or {}).get('server_type', {}).get('name', '?')

            detail = ["auto-switch" if w.get('auto', True) else "notify only"]
            if is_return(w):
                detail[0] = "back to its plan after a reset"
            if w.get('failures'):
                detail.append(f"{w['failures']} refused")
            retry = format_retry(w)
            if retry:
                detail.append(retry)

            lines.append(
                f"• `{name}`: `{current}` → `{plan}` in `{watch_location(w)}` "
                f"({escape_markdown(w.get('account', '?'))}) — {', '.join(detail)}"
            )
            keyboard.append([InlineKeyboardButton(
                f"❌ Cancel {name} → {plan}",
                callback_data=f"pwx_{server_id}_{plan}",
            )])
        if schedules:
            lines.append(("\n" if lines else "") + "⏰ *Scheduled traffic resets* (Iran time)")
        for sc in schedules:
            lines.append(
                f"• `{sc.get('name')}`: {describe(sc)} — next `{fmt_time(sc['next_run'])}` "
                f"({escape_markdown(sc.get('account', '?'))})"
            )
            keyboard.append([InlineKeyboardButton(
                f"❌ Cancel reset of {sc.get('name')}, {describe(sc)}",
                callback_data=f"rschx_{sc.get('id')}",
            )])
        text = (
            "🔔 *Alerts & Schedules*\n\n" + "\n".join(lines) + "\n\n"
            f"Checked every {CHECK_INTERVAL_MINUTES} minutes; the bot acts on its "
            f"own as soon as a plan has room (a switch briefly powers the server "
            f"off). Tap ❌ to cancel one."
        )

    pause = automation.status_text()
    if pause:
        text = text.replace("🔔 *Alerts & Schedules*\n\n", "🔔 *Alerts & Schedules*\n\n"
                            f"{pause}: nothing here acts until they are resumed.\n\n", 1)
    keyboard.insert(0, [InlineKeyboardButton(
        "▶️ Resume automations" if pause else "⏸ Pause all automations",
        callback_data="aresume" if pause else "apause")])
    ar = auto_reset.settings()
    keyboard.insert(0, [InlineKeyboardButton(
        f"🌙 Auto Reset: {auto_reset.time_text(ar)} · {ar['threshold']}%" if ar['enabled']
        else "🌙 Auto Reset: off", callback_data="ar")])
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="watches")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="start_menu")])
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
    )


async def cancel_watch(query, context, server_id, plan):
    remove_watches_for(server_id, plan)
    await show_watches(query, context)


async def cancel_create_watch(query, context, watch_id):
    remove_create_watch(watch_id)
    await show_watches(query, context)


async def cancel_schedule(query, context, sched_id):
    remove_schedule(sched_id)
    await show_watches(query, context)


router.add(r"watches", show_watches)
router.add(r"rschx_([0-9a-f]+)", cancel_schedule)
router.add(r"cwx_([0-9a-f]+)", cancel_create_watch)
router.add(r"pwx_(\d+)_([^_]+)", cancel_watch, int)
