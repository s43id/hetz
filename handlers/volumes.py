"""Create, attach, detach and delete block-storage volumes."""
import asyncio
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils import server_location

from .router import router
from .context import _get_api, _resolve_server
from .ui import send_not_found
from .pricing import _gross


async def show_volumes(query, context, server_id):
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    api = _get_api(context)
    volumes, pricing = await asyncio.gather(
        api.list_volumes(),
        api.get_pricing(),
    )
    loc = server_location(server)
    per_gb = _gross(pricing.get("volume", {}).get("price_per_gb_month", {}) or pricing.get("volume", {}))
    attached = [v for v in volumes if v.get("server") == server_id]
    free = [v for v in volumes if not v.get("server") and v.get("location", {}).get("name") == loc]

    text = f"💽 *Volumes* — `{server.get('name')}`\n\n"
    keyboard = [[InlineKeyboardButton("➕ New Volume", callback_data=f"volnew_{server_id}")]]

    if attached:
        text += "*Attached:*\n"
        for v in attached:
            text += (
                f"• `{v.get('name')}` | {v.get('size')} GB | "
                f"`{v.get('linux_device', '?')}` | €{v.get('size', 0) * per_gb:.2f}/mo\n"
            )
            keyboard.append([
                InlineKeyboardButton(f"🔌 Detach {v.get('name')}", callback_data=f"voldetach_{v['id']}_{server_id}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"voldel_{v['id']}_{server_id}"),
            ])
        text += "\n"
    if free:
        text += f"*Available in {loc} (not attached):*\n"
        for v in free:
            text += f"• `{v.get('name')}` | {v.get('size')} GB | €{v.get('size', 0) * per_gb:.2f}/mo\n"
            keyboard.append([
                InlineKeyboardButton(f"🔗 Attach {v.get('name')} ({v.get('size')}GB)",
                                     callback_data=f"volattach_{v['id']}_{server_id}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"voldel_{v['id']}_{server_id}"),
            ])
        text += "\n"
    if not attached and not free:
        text += "No volumes yet.\n\n"

    text += f"🕓 Updated: `{datetime.now().strftime('%H:%M:%S')}`"
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"volmenu_{server_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Server", callback_data=f"server_{server_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def volume_pick_size(query, context, server_id):
    keyboard = [
        [InlineKeyboardButton(f"{size} GB", callback_data=f"volnewc_{server_id}_{size}")
         for size in (10, 20, 50, 100)],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"volmenu_{server_id}")],
    ]
    await query.edit_message_text(
        "💽 *New Volume*\n\nChoose the size (ext4, auto-mounted):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def volume_create(query, context, server_id, size):
    await query.edit_message_text(f"💽 Creating {size} GB volume...")
    name = f"vol-{datetime.now().strftime('%y%m%d%H%M%S')}"
    result = await _get_api(context).create_volume(name, size, server_id)
    keyboard = [[InlineKeyboardButton("💽 Back to Volumes", callback_data=f"volmenu_{server_id}")]]
    if result:
        device = result.get("volume", {}).get("linux_device", "?")
        await query.edit_message_text(
            f"✅ *Volume created & attached*\n\n"
            f"🏷 Name: `{name}`\n💾 Size: `{size} GB`\n📁 Device: `{device}`\n\n"
            f"It is formatted as ext4 and auto-mounted.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            "❌ Volume creation failed. Check the logs and try again.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def volume_attach(query, context, volume_id, server_id):
    await query.edit_message_text("🔗 Attaching volume...")
    await _get_api(context).attach_volume(volume_id, server_id)
    await asyncio.sleep(3)
    await show_volumes(query, context, server_id)


async def volume_detach(query, context, volume_id, server_id):
    await query.edit_message_text("🔌 Detaching volume...")
    await _get_api(context).detach_volume(volume_id)
    await asyncio.sleep(3)
    await show_volumes(query, context, server_id)


async def volume_delete_confirm(query, context, volume_id, server_id):
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, delete it", callback_data=f"voldelgo_{volume_id}_{server_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"volmenu_{server_id}"),
        ]
    ]
    await query.edit_message_text(
        "🗑 *Delete Volume*\n\n"
        "⚠️ All data on the volume will be lost. If it is attached, it will "
        "be detached first.\n\nAre you sure?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def volume_delete(query, context, volume_id, server_id):
    api = _get_api(context)
    await query.edit_message_text("🗑 Deleting volume...")
    volumes = await api.list_volumes()
    vol = next((v for v in volumes if v.get("id") == volume_id), None)
    if vol and vol.get("server"):
        await api.detach_volume(volume_id)
        await asyncio.sleep(5)
    result = await api.delete_volume(volume_id)
    keyboard = [[InlineKeyboardButton("💽 Back to Volumes", callback_data=f"volmenu_{server_id}")]]
    if result is not None:
        await query.edit_message_text("✅ Volume deleted.", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await query.edit_message_text(
            "❌ Failed to delete the volume. Check the logs and try again.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


router.add(r"volmenu_(\d+)", show_volumes, int)
router.add(r"volnew_(\d+)", volume_pick_size, int)
router.add(r"volnewc_(\d+)_(\d+)", volume_create, int, int)
router.add(r"volattach_(\d+)_(\d+)", volume_attach, int, int)
router.add(r"voldetach_(\d+)_(\d+)", volume_detach, int, int)
router.add(r"voldel_(\d+)_(\d+)", volume_delete_confirm, int, int)
router.add(r"voldelgo_(\d+)_(\d+)", volume_delete, int, int)
