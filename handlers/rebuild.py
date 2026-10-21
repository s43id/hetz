"""Rebuild a server's OS from a system image."""
import asyncio
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils import copy_button

from .router import router
from .context import _get_api, _resolve_server
from .ui import send_not_found
from .servers import is_protected, send_protected


async def rebuild_pick_image(query, context, server_id):
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    if is_protected(server):
        await send_protected(query, server_id, server.get("name", "Server"), "rebuild")
        return
    api = _get_api(context)
    images = await api.list_images("system")
    arch = server.get("server_type", {}).get("architecture", "x86")
    usable = [
        i for i in images
        if i.get("architecture") == arch and not i.get("deprecated")
    ]
    keyboard = []
    row = []
    for img in sorted(usable, key=lambda i: i.get("name", "")):
        row.append(InlineKeyboardButton(
            img.get("description") or img.get("name"),
            callback_data=f"rebuildimg_{server_id}_{img.get('name')}",
        ))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("⬅️ Back to Server", callback_data=f"server_{server_id}")])
    await query.edit_message_text(
        f"🔧 *Rebuild OS*\n\n"
        f"Server: `{server.get('name')}`\n\n"
        f"Choose the operating system image:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def rebuild_confirm(query, context, server_id, image):
    server = await _resolve_server(context, server_id)
    name = server.get("name", "Server") if server else "Server"
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, rebuild it", callback_data=f"rebuildgo_{server_id}_{image}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"server_{server_id}"),
        ]
    ]
    await query.edit_message_text(
        f"🔧 *Rebuild OS*\n\n"
        f"Server: `{name}`\n"
        f"New image: `{image}`\n\n"
        f"🚨 *ALL DATA ON THE SERVER WILL BE ERASED* and the OS will be "
        f"reinstalled from scratch.\n\n"
        f"Are you absolutely sure?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def rebuild_go(query, context, server_id, image):
    server = await _resolve_server(context, server_id)
    if server and is_protected(server):
        await send_protected(query, server_id, server.get("name", "Server"), "rebuild")
        return
    await query.edit_message_text("🔧 Rebuilding server... this takes a minute or two.")
    result = await _get_api(context).rebuild_server(server_id, image)
    keyboard = [[InlineKeyboardButton("🖥 Back to Server", callback_data=f"server_{server_id}")]]
    if result:
        pw = result.get("root_password")
        text = f"✅ *Rebuild started* with `{image}`.\n\n"
        if pw:
            text += f"🔑 New root password:\n`{pw}`\n\n⚠️ Save it now — it won't be shown again."
            keyboard.insert(0, [copy_button("📋 Copy password", pw)])
        else:
            text += "Your SSH keys were installed, no new password was generated."
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await query.edit_message_text(
            "❌ Rebuild failed. Check the logs and try again.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


router.add(r"rebuild_(\d+)", rebuild_pick_image, int)
router.add(r"rebuildimg_(\d+)_(.+)", rebuild_confirm, int)
router.add(r"rebuildgo_(\d+)_(.+)", rebuild_go, int)
