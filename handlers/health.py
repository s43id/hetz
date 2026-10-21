"""🩺 Health & Safety, ⏸ pause automations, and ⏳ Running now.

- ``hl``: every account's API access, the pause state with its buttons,
  servers locked now, failed actions of the last 24 h, the daily report's
  settings.
- ``apause`` → ``apause_<hours>`` (0 = until resumed), ``aresume``.
- Daily report: ``hltog`` (on / off), ``hlh`` → ``hlhh_<h>`` → ``hlm_<h>_<m>``
  (time, Iran), ``hlrep`` (send it now).
- ``running`` (also ``/running``): the bot's own work (resets, plan and IP
  changes, with their stage) and Hetzner's running actions with progress.
"""
import asyncio
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import automation
import health
from hetzner_api import api_registry
from reset_schedule import fmt_time, iran_now
from server_manager import activity
from utils import escape_markdown

from .router import router

MAX_RUNNING = 15


def _rows(buttons, width):
    return [buttons[i:i + width] for i in range(0, len(buttons), width)]


async def show_health(query, context, notice=""):
    text = "🩺 *Health & Safety*\n\n"
    if notice:
        text += notice + "\n\n"
    text += "*Hetzner accounts*\n"
    data = health._load()
    for name in api_registry.names():
        text += health.account_line(name, health.account_state(name, data),
                                    api_registry.get(name)) + "\n"
    pause = automation.status_text()
    text += ("\n" + pause + ".\n" if pause
             else "\n▶️ *Automations run*: " + automation.WHAT + ".\n")
    locked = health.locked_list()
    if locked:
        text += "\n🔒 *Locked now*\n" + "\n".join(
            f"• `{x.get('name')}` since {fmt_time(x.get('since') or 0)}" for x in locked[:8]) + "\n"
    failed = health.failed_since(86400)
    text += (f"\n❌ *Failed actions, 24 h*: {len(failed)}\n"
             + "\n".join(health.failed_line(f) for f in failed[-5:]) + ("\n" if failed else ""))
    r = health.report_settings()
    text += (f"\n📋 Daily check: " + (f"on, {health.report_time_text(r)} Iran time · next "
                                       f"`{fmt_time(r['next_run'])}`" if r['enabled'] else "off")
             + f"\n\nChecked every {health.CHECK_INTERVAL_MINUTES} minutes: account access, "
               f"servers locked {health.LOCKED_ALERT_MINUTES}+ min, "
               f"{health.FAILED_ALERT_COUNT}+ failed actions in an hour.")
    keyboard = [
        [InlineKeyboardButton("▶️ Resume automations", callback_data="aresume") if pause
         else InlineKeyboardButton("⏸ Pause automations", callback_data="apause")],
        [InlineKeyboardButton("📋 Send the daily check now", callback_data="hlrep")],
        [InlineKeyboardButton(f"🕘 Check time: {health.report_time_text(r)}", callback_data="hlh"),
         InlineKeyboardButton("📋 Daily check: on" if r['enabled'] else "📋 Daily check: off",
                              callback_data="hltog")],
        [InlineKeyboardButton("⏳ Running now", callback_data="running"),
         InlineKeyboardButton("🔄 Refresh", callback_data="hl")],
        [InlineKeyboardButton("🏠 Menu", callback_data="start_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def pick_pause(query, context):
    keyboard = [
        [InlineKeyboardButton(f"⏸ {h} hour{'s' if h > 1 else ''}", callback_data=f"apause_{h}")
         for h in automation.PAUSE_HOURS],
        [InlineKeyboardButton("⏸ Until I resume", callback_data="apause_0")],
        [InlineKeyboardButton("⬅️ Back", callback_data="hl")],
    ]
    await query.edit_message_text(
        f"⏸ *Pause automations*\n\nStops {automation.WHAT} until you resume them, or for "
        f"the time you pick. Alerts stay saved and act once resumed; skipped runs are "
        f"reported.\n\nYour own buttons keep working, and a reset already running "
        f"finishes. The checks and traffic alerts go on.\n\nFor how long?",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def do_pause(query, context, hours):
    automation.pause(hours or None)
    await show_health(query, context, automation.status_text() + ".")


async def do_resume(query, context):
    automation.resume()
    await show_health(query, context, "▶️ Automations resumed.")


async def send_report(query, context):
    await query.edit_message_text("📋 Checking every account...")
    text = await health.report_text()
    keyboard = [[InlineKeyboardButton("🩺 Health", callback_data="hl"),
                 InlineKeyboardButton("🏠 Menu", callback_data="start_menu")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def toggle_report(query, context):
    on = not health.report_settings()['enabled']
    health.set_report_enabled(on)
    await show_health(query, context, "📋 Daily check on." if on else "📋 Daily check off.")


async def pick_report_hour(query, context):
    buttons = [InlineKeyboardButton(f"{h:02d}", callback_data=f"hlhh_{h}") for h in range(24)]
    keyboard = _rows(buttons, 6)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="hl")])
    await query.edit_message_text(
        f"📋 *Daily check time*\n\nWhich hour, Iran time? Now: `{iran_now().strftime('%H:%M')}`",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def pick_report_minute(query, context, hour):
    buttons = [InlineKeyboardButton(f"{hour:02d}:{m:02d}", callback_data=f"hlm_{hour}_{m}")
               for m in health.REPORT_MINUTES]
    keyboard = _rows(buttons, 3)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="hlh")])
    await query.edit_message_text("📋 *Daily check time*\n\nWhich minute?",
                                  reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def save_report_time(query, context, hour, minute):
    health.set_report_time(hour, minute)
    await show_health(query, context, f"📋 Daily check at {hour:02d}:{minute:02d} (Iran time).")


# ---------------------------------------------------------------- running now

def _bar(fraction, width=10):
    n = max(0, min(width, int(round(fraction * width))))
    return "▰" * n + "▱" * (width - n)


async def running_items():
    """(bot work, Hetzner actions) with server names, for the chat and the Mini App."""
    names = api_registry.names()
    lists = await asyncio.gather(*(api_registry.get(n).list_servers() for n in names),
                                 return_exceptions=True)
    actions = await asyncio.gather(
        *(api_registry.get(n).list_server_actions(status='running') for n in names),
        return_exceptions=True)
    server_names = {s.get('id'): s.get('name') for items in lists if isinstance(items, list)
                    for s in items}
    now = time.time()
    own = [dict(a, name=server_names.get(a['server_id']) or f"#{a['server_id']}",
                minutes=int((now - a['started']) // 60)) for a in activity()]
    hetzner = []
    for account, items in zip(names, actions):
        for a in items if isinstance(items, list) else []:
            sid = next((r.get('id') for r in a.get('resources') or [] if r.get('type') == 'server'),
                       None)
            started = health._epoch(a.get('started'))
            hetzner.append({'account': account, 'id': a.get('id'), 'server_id': sid,
                            'name': server_names.get(sid) or (f"#{sid}" if sid else "?"),
                            'command': a.get('command'),
                            'label': health.command_label(a.get('command')),
                            'progress': a.get('progress') or 0,
                            'minutes': int((now - started) // 60) if started else None})
    return own, hetzner


async def show_running(query, context):
    own, hetzner = await running_items()
    text = "⏳ *Running now*\n\n"
    keyboard, opened = [], set()
    if own:
        text += "*The bot*\n"
        for a in own:
            step = (f"step {a['step']}/{a['steps']}: " if a.get('steps') and a.get('step') else "")
            text += (f"• `{a['name']}` — {escape_markdown(a['title'])}, {a['minutes']} min\n"
                     f"   {step}{escape_markdown(a['stage'])}\n")
    if hetzner:
        text += ("\n" if own else "") + "*Hetzner*\n"
        for a in hetzner[:MAX_RUNNING]:
            since = f", {a['minutes']} min" if a['minutes'] is not None else ""
            text += (f"• `{a['name']}` — {a['label']} {_bar(a['progress'] / 100)} "
                     f"{a['progress']}%{since}\n")
    if not own and not hetzner:
        text += "Nothing is running.\n"
    for a in own + hetzner:
        sid = a.get('server_id')
        if sid and sid not in opened and len(opened) < 8:
            opened.add(sid)
            keyboard.append([InlineKeyboardButton(f"🖥 {a['name']}", callback_data=f"server_{sid}")])
    text += f"\n🕓 {iran_now().strftime('%H:%M:%S')} (Iran time)"
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="running"),
                     InlineKeyboardButton("🏠 Menu", callback_data="start_menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


router.add(r"hl", show_health)
router.add(r"apause", pick_pause)
router.add(r"apause_(\d+)", do_pause, int)
router.add(r"aresume", do_resume)
router.add(r"hlrep", send_report)
router.add(r"hltog", toggle_report)
router.add(r"hlh", pick_report_hour)
router.add(r"hlhh_(\d+)", pick_report_minute, int)
router.add(r"hlm_(\d+)_(\d+)", save_report_time, int, int)
router.add(r"running", show_running)
