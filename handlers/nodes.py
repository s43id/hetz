"""🧩 Add to Panel: register an existing server as a PasarGuard node.

The server must already run PasarGuard Node (setup.sh). The admin picks a
panel and pastes the node's API key (setup.sh prints it at the end); the bot
reads the node's certificate from its port and creates the node in the panel.
New servers made with "Create as node" are registered automatically instead
(node_setup.check_node_setups).
"""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from config import Config
import node_setup
from utils import copy_button

from .router import router
from .context import _resolve_server
from .ui import send_not_found

WAIT_NODE_KEY = 32


def _ip(server):
    return server.get("public_net", {}).get("ipv4", {}).get("ip")


def _back(server_id, api_key=None):
    rows = [[InlineKeyboardButton("🖥 Back to Server", callback_data=f"server_{server_id}")]]
    if api_key:
        rows.insert(0, [copy_button("📋 Copy API key", api_key)])
    return InlineKeyboardMarkup(rows)


async def node_pick_panel(query, context, server_id):
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    if not node_setup.can_register():
        await query.edit_message_text(
            "⚠️ No PasarGuard panel is configured (PANELS in .env).",
            reply_markup=_back(server_id))
        return
    keyboard = [[InlineKeyboardButton(f"🧩 {panel}", callback_data=f"nodeaddp_{server_id}_{panel}")]
                for panel in node_setup.panel_names()]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=f"server_{server_id}")])
    await query.edit_message_text(
        f"🧩 *Add to Panel*\n\n"
        f"Server: `{server.get('name')}` — `{_ip(server)}`\n\n"
        f"PasarGuard Node must already run on it (setup.sh). Which panel?",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
    )


async def _start_node_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != Config.ADMIN_ID:
        await query.answer("⛔ Unauthorized", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    _, sid, panel = query.data.split("_", 2)
    server_id = int(sid)
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return ConversationHandler.END
    if panel not in node_setup.panel_names():
        await query.edit_message_text(f"⚠️ Panel `{panel}` is not configured.",
                                      reply_markup=_back(server_id), parse_mode="Markdown")
        return ConversationHandler.END
    context.user_data['node_key'] = {"server_id": server_id, "panel": panel}
    await query.edit_message_text(
        f"🧩 *Add to Panel* `{panel}`\n\n"
        f"Server: `{server.get('name')}` — `{_ip(server)}`\n\n"
        f"Send the node's *API key* (a UUID; setup.sh prints it at the end, and "
        f"it is in `/opt/pg-node/.env` on the server).",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=f"nodekey_cancel_{server_id}")]]),
        parse_mode="Markdown",
    )
    return WAIT_NODE_KEY


async def recv_node_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != Config.ADMIN_ID:
        return ConversationHandler.END
    pending = context.user_data.get('node_key')
    if not pending:
        return ConversationHandler.END
    api_key = update.message.text.strip()
    if not node_setup.is_api_key(api_key):
        await update.message.reply_text(
            "⚠️ That is not a UUID. Send the API key like "
            "`1b4e28ba-2fa1-11d2-883f-0016d3cca427`, or tap Cancel above.",
            parse_mode="Markdown")
        return WAIT_NODE_KEY
    server_id, panel = pending["server_id"], pending["panel"]
    context.user_data.pop('node_key', None)
    server = await _resolve_server(context, server_id)
    if not server:
        await update.message.reply_text("❌ Server not found.")
        return ConversationHandler.END
    name, ip = server.get("name"), _ip(server)

    cert = await node_setup.fetch_node_cert(ip)
    if not cert:
        await update.message.reply_text(
            f"❌ *PasarGuard Node does not answer* on `{ip}:{node_setup.NODE_PORT}`.\n\n"
            f"Is setup.sh finished and the node running?",
            reply_markup=_back(server_id), parse_mode="Markdown")
        return ConversationHandler.END
    ok, info, _retryable = await node_setup.register_node(panel, name, ip, cert, api_key)
    text = (node_setup.added_text(name, ip, panel, info) if ok
            else node_setup.failed_text(name, ip, panel, api_key, info))
    await update.message.reply_text(text, reply_markup=_back(server_id, None if ok else api_key),
                                    parse_mode="Markdown")
    return ConversationHandler.END


async def cancel_node_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pending = context.user_data.pop('node_key', None) or {}
    server_id = pending.get("server_id") or int(query.data.rsplit("_", 1)[1])
    from .servers import show_server_detail
    await show_server_detail(query, context, server_id, refresh=True)
    return ConversationHandler.END


async def _stale_node_key_cancel(query, context, server_id):
    """The prompt's Cancel pressed after the prompt ended: just go back."""
    context.user_data.pop('node_key', None)
    from .servers import show_server_detail
    await show_server_detail(query, context, server_id, refresh=True)


router.add(r"nodeadd_(\d+)", node_pick_panel, int)
router.add(r"nodekey_cancel_(\d+)", _stale_node_key_cancel, int)
