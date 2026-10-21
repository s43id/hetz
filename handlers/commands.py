"""Slash commands: /servers, /find, /alerts, /cost, /health, /running, /update
(and the menu Telegram shows).

Each command opens the same screen as its button. The screens are written
for a button press (they edit the pressed message), so a command passes them
a ``_MessageQuery``: its first edit sends a new message, later ones edit it,
and the buttons on that message then work as usual.
"""
import asyncio

from telegram import BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from hetzner_api import api_registry
from utils import escape_markdown, get_location_info, server_location

from .servers import _show_all_servers, show_server_detail, _server_ip
from .watches import show_watches
from .cost import show_overage_cost
from .update import show_update
from .health import show_health, show_running

COMMANDS = [
    ("start", "Main menu"),
    ("servers", "All servers"),
    ("find", "Find a server: /find name or IP"),
    ("alerts", "Alerts & schedules"),
    ("cost", "Cost report"),
    ("health", "Health & safety, pause automations"),
    ("running", "What is running now"),
    ("update", "Bot version & update"),
]
MAX_MATCHES = 20


class _MessageQuery:
    """Stands in for a CallbackQuery when a screen is opened by a command."""

    def __init__(self, message, user):
        self.message = message
        self.from_user = user
        self.data = ""
        self._sent = None

    async def answer(self, *args, **kwargs):
        pass

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None, **kwargs):
        if self._sent is None:
            self._sent = await self.message.reply_text(
                text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
            if self._sent is not None:
                self.message = self._sent
            return self._sent
        return await self._sent.edit_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)


def _admin_command(screen, takes_args=False):
    """Wrap a screen(query, context[, *words]) as an admin-only command handler."""
    async def handler(update, context):
        if update.effective_user.id != Config.ADMIN_ID:
            return
        args = (context.args or []) if takes_args else []
        await screen(_MessageQuery(update.message, update.effective_user), context, *args)
    return handler


def _matches(server, needle):
    needle = needle.lower()
    ipv6 = ((server.get("public_net") or {}).get("ipv6") or {}).get("ip") or ""
    return (needle in (server.get("name") or "").lower()
            or _server_ip(server).startswith(needle)
            or (":" in needle and ipv6.lower().startswith(needle.split("/")[0].rstrip(":"))))


async def find_server(query, context, *words):
    needle = " ".join(words).strip()
    if not needle:
        await query.edit_message_text(
            "🔎 Usage: `/find name` or `/find IP` — part of a name or the start of an IP works.",
            parse_mode="Markdown")
        return
    names = api_registry.names()
    lists = await asyncio.gather(*(api_registry.get(n).list_servers() for n in names))
    found = []
    account_map = context.user_data.setdefault('server_account_map', {})
    for name, servers in zip(names, lists):
        for s in servers:
            account_map[s['id']] = name
            if _matches(s, needle):
                found.append((name, s))
    if len(found) == 1:
        await show_server_detail(query, context, found[0][1]['id'])
        return
    back = [InlineKeyboardButton("📋 All Servers", callback_data="list_servers"),
            InlineKeyboardButton("⬅️ Menu", callback_data="start_menu")]
    if not found:
        await query.edit_message_text(
            f"🔎 No server matches {escape_markdown(needle)}.",
            reply_markup=InlineKeyboardMarkup([back]), parse_mode="Markdown")
        return
    keyboard = []
    for account, s in found[:MAX_MATCHES]:
        _, flag = get_location_info(server_location(s))
        label = f"{s.get('name')} | {flag} | {_server_ip(s)}"
        if len(names) > 1:
            label = f"[{account}] {label}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"server_{s['id']}")])
    keyboard.append(back)
    more = f" (first {MAX_MATCHES} shown)" if len(found) > MAX_MATCHES else ""
    await query.edit_message_text(
        f"🔎 *{len(found)} servers match*{more}", reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown")


COMMAND_HANDLERS = {
    "servers": _admin_command(_show_all_servers),
    "find": _admin_command(find_server, takes_args=True),
    "alerts": _admin_command(show_watches),
    "cost": _admin_command(show_overage_cost),
    "update": _admin_command(show_update),
    "health": _admin_command(show_health),
    "running": _admin_command(show_running),
}


async def set_bot_commands(application):
    """Show the commands in Telegram's menu, for the admin's chat only."""
    try:
        await application.bot.set_my_commands(
            [BotCommand(c, d) for c, d in COMMANDS],
            scope=BotCommandScopeChat(Config.ADMIN_ID))
    except Exception:
        # The admin may not have opened the bot yet; commands still work.
        pass
