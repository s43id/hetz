"""Shared UI pieces used across the main menu."""
import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from config import Config
from price_overrides import effective_price
from utils import overage_cost, server_location, traffic_fraction

from hetzner_api import api_registry
from plan_watch import list_watches
from create_watch import list_create_watches
from reset_schedule import list_schedules
from server_manager import activity
import automation
import health
import updater

from .pricing import _server_type_price


async def send_not_found(query):
    """Report a server that no account can see, with a way back.

    Used wherever a button outlives its server — deleted meanwhile, or the
    id simply belongs to no account we hold a token for.
    """
    await query.edit_message_text(
        "⚠️ Server not found in any account. It may have been deleted.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back to List", callback_data="list_servers")]]
        ),
    )

def account_header(context):
    """'🔑 Account: x' line for account-wide screens, '' with one account."""
    from .context import _current_account
    if len(api_registry.names()) < 2:
        return ""
    return f"🔑 Account: `{_current_account(context)}`\n\n"


def account_switch_rows(context, screen):
    """Buttons to show an account-wide screen for another account.

    These screens (cost report, snapshots, IP lists) cover one account; with
    several, say which and offer the others instead of silently using
    whichever account was last touched.
    """
    from .context import _current_account
    names = api_registry.names()
    if len(names) < 2:
        return []
    current = _current_account(context)
    buttons = [
        InlineKeyboardButton(f"🔑 {name}", callback_data=f"useacct_{screen}_{name}")
        for name in names if name != current
    ]
    return [buttons[i:i + 3] for i in range(0, len(buttons), 3)]


MENU_TITLE = "🚀 *Hetzner Server Manager*"


async def menu_text():
    """The main menu's dashboard: servers, traffic, monthly cost, alerts."""
    names = api_registry.names()
    lists = await asyncio.gather(*(api_registry.get(n).list_servers() for n in names),
                                 return_exceptions=True)
    servers, per_account, cost, overage = [], [], 0.0, 0.0
    for name, items in zip(names, lists):
        items = items if isinstance(items, list) else []
        per_account.append(f"{name} {len(items)}")
        for s in items:
            servers.append(s)
            cost += effective_price(name, s.get("id"), _server_type_price(
                s.get("server_type", {}), server_location(s)))
            overage += overage_cost(s)
    running = sum(1 for s in servers if s.get("status") == "running")
    fractions = [traffic_fraction(s) for s in servers]
    near = sum(1 for f in fractions if Config.WARNING_THRESHOLD <= f < 1)
    over = sum(1 for f in fractions if f >= 1)
    pending = len(list_watches()) + len(list_create_watches()) + len(list_schedules())

    text = f"{MENU_TITLE}\n\n🖥 Servers: *{len(servers)}*"
    if servers:
        text += (" (all running)" if running == len(servers)
                 else f" ({running} running, {len(servers) - running} other)")
    if len(names) > 1:
        text += f"\n      {' · '.join(per_account)}"
    text += (f"\n🔥 Near the traffic limit: *{near}*" + (f" · over it: *{over}*" if over else "")
             + f"\n💶 Servers `€{cost:.2f}`/month"
             + (f" + overage `€{overage:.2f}`" if overage else "")
             + f"\n🔔 Alerts & schedules: *{pending}*")
    busy = len(activity())
    if busy:
        text += f"\n⏳ Running now: *{busy}*"
    for name, st in health.problems():
        text += "\n" + health.account_line(name, st)
    pause = automation.status_text()
    if pause:
        text += f"\n\n{pause}"
    return text


def _main_menu_keyboard(current_account: str = ''):
    names = api_registry.names()
    keyboard = []
    if Config.webapp_url():
        keyboard.append([InlineKeyboardButton(
            "📱 Open App", web_app=WebAppInfo(Config.webapp_url()))])

    if len(names) > 1:
        keyboard.append([InlineKeyboardButton("📊 All Servers", callback_data="list_servers")])
        for name in names:
            mark = "✅" if name == current_account else "🔘"
            keyboard.append([InlineKeyboardButton(
                f"{mark} {name}",
                callback_data=f"setaccount_{name}",
            )])
    else:
        keyboard.append([InlineKeyboardButton("📊 Servers", callback_data="list_servers")])

    keyboard += [
        [
            InlineKeyboardButton("📸 Snapshots", callback_data="snapshots"),
            InlineKeyboardButton("💸 Cost Report", callback_data="overage_cost"),
        ],
        [
            InlineKeyboardButton("🌐 Floating IPs", callback_data="fips"),
            InlineKeyboardButton("📍 Primary IPs", callback_data="pips"),
        ],
    ]
    pending = len(list_watches()) + len(list_create_watches()) + len(list_schedules())
    keyboard.append([InlineKeyboardButton(
        f"🔔 Alerts & Schedules ({pending})" if pending else "🔔 Alerts & Schedules",
        callback_data="watches",
    )])
    keyboard.append([
        InlineKeyboardButton("🩺 Health" + (" ⚠️" if health.problems() else "")
                             + (" ⏸" if automation.is_paused() else ""), callback_data="hl"),
        InlineKeyboardButton("⏳ Running now", callback_data="running"),
    ])
    keyboard.append([InlineKeyboardButton(
        f"⬆️ Update · v{updater.current_version()}", callback_data="upd")])
    return keyboard
