"""Snapshot listing, detail, creation and deletion."""
import asyncio
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils import get_location_info, server_location

from .router import router
from .context import _get_api, _resolve_server
from .ui import send_not_found, account_header, account_switch_rows
from .pricing import _image_price_per_gb


async def show_snapshots(query, context):
    api = _get_api(context)
    images, pricing = await asyncio.gather(
        api.list_images(),
        api.get_pricing(),
    )
    per_gb = _image_price_per_gb(pricing)

    text = f"📸 *SNAPSHOTS*\n\n{account_header(context)}"
    keyboard = [[InlineKeyboardButton("➕ Take Snapshot", callback_data="snap_new")]]

    if not images:
        text += "No snapshots yet.\n"
    else:
        total_size = sum(i.get("image_size") or 0 for i in images)
        text += (
            f"Total: {len(images)} | {total_size:.1f} GB | "
            f"€{total_size * per_gb:.2f}/month\n\n"
            f"Tap a snapshot to manage it:"
        )
        for img in images:
            s_emoji = "✅" if img.get("status") == "available" else "⏳"
            lock = "🔒 " if img.get("protection", {}).get("delete") else ""
            size = img.get("image_size") or 0
            label = img.get("description") or img.get("name") or str(img["id"])
            keyboard.append([InlineKeyboardButton(
                f"{s_emoji} {lock}{label} | {size:.1f} GB",
                callback_data=f"snap_{img['id']}",
            )])

    text += f"\n\n🕓 Updated: `{datetime.now().strftime('%H:%M:%S')}`"
    keyboard += account_switch_rows(context, "snapshots")
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="snapshots")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="start_menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def show_snapshot_detail(query, context, image_id):
    api = _get_api(context)
    img, pricing = await asyncio.gather(
        api.get_image(image_id),
        api.get_pricing(),
    )
    if not img:
        await query.edit_message_text(
            "⚠️ Snapshot not found.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="snapshots")]]),
        )
        return

    size = img.get("image_size") or 0
    cost = size * _image_price_per_gb(pricing)
    status = img.get("status", "unknown")
    s_emoji = "✅" if status == "available" else "⏳"
    created = img.get("created", "")[:16].replace("T", " ")
    source = img.get("created_from", {}) or {}

    protected = bool(img.get("protection", {}).get("delete"))
    text = (
        f"📸 *Snapshot Detail*\n\n"
        f"🏷 Name: `{img.get('description') or img.get('name') or image_id}`\n"
        f"🆔 ID: `{img['id']}`\n"
        f"🖥 Server: `{source.get('name', 'N/A')}`\n"
        f"💾 Size: `{size:.2f} GB`\n"
        f"💰 Cost: `€{cost:.2f}/month`\n"
        f"{s_emoji} Status: `{status.upper()}`\n"
        f"🔒 Protection: `{'ON' if protected else 'OFF'}`\n"
        f"📅 Created: `{created}`\n"
    )
    if img.get("disk_size"):
        text += f"💽 Disk of the source: `{img['disk_size']} GB` (a new server needs at least this)\n"
    keyboard = [
        [InlineKeyboardButton("🗑 Delete Snapshot", callback_data=f"snapdel_{image_id}")],
        [InlineKeyboardButton("⬅️ Back to Snapshots", callback_data="snapshots")],
    ]
    if status == "available":
        keyboard.insert(0, [InlineKeyboardButton("🧬 New server from this snapshot",
                                                 callback_data=f"snapclone_{image_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def delete_snapshot_confirm(query, context, image_id):
    img = await _get_api(context).get_image(image_id)
    label = (img.get("description") or img.get("name") or str(image_id)) if img else str(image_id)
    protected = bool(img and img.get("protection", {}).get("delete"))
    note = "\n🔒 This snapshot is *delete-protected* — protection will be removed first.\n" if protected else ""
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, delete it", callback_data=f"snapdel_confirm_{image_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"snap_{image_id}"),
        ]
    ]
    await query.edit_message_text(
        f"🗑 *Delete Snapshot*\n\n"
        f"Snapshot: `{label}`\n{note}\n"
        f"⚠️ This cannot be undone. Are you sure?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def delete_snapshot(query, context, image_id):
    api = _get_api(context)
    await query.edit_message_text("🗑 Deleting snapshot...")
    img = await api.get_image(image_id)
    if img and img.get("protection", {}).get("delete"):
        unlocked = await api.change_image_protection(image_id, False)
        if unlocked is None:
            await query.edit_message_text(
                "❌ Could not remove the delete protection. Check the logs.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Back to Snapshots", callback_data="snapshots")]]
                ),
            )
            return
    result = await api.delete_image(image_id)
    keyboard = [[InlineKeyboardButton("⬅️ Back to Snapshots", callback_data="snapshots")]]
    if result is not None:
        await query.edit_message_text(
            "✅ Snapshot deleted successfully.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        await query.edit_message_text(
            "❌ Failed to delete snapshot. Check the logs and try again.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def show_server_snapshots(query, context, server_id):
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    api = _get_api(context)
    images, pricing = await asyncio.gather(
        api.list_images(),
        api.get_pricing(),
    )
    per_gb = _image_price_per_gb(pricing)
    own = [i for i in images if (i.get("created_from") or {}).get("id") == server_id]

    text = f"📸 *Snapshots* — `{server.get('name')}`\n\n"
    keyboard = [[InlineKeyboardButton("➕ Take Snapshot", callback_data=f"snapcreate_{server_id}")]]

    if not own:
        text += "This server has no snapshots yet.\n"
    else:
        total_size = sum(i.get("image_size") or 0 for i in own)
        text += (
            f"Total: {len(own)} | {total_size:.1f} GB | €{total_size * per_gb:.2f}/month\n\n"
            f"Tap a snapshot to manage it:"
        )
        for img in own:
            s_emoji = "✅" if img.get("status") == "available" else "⏳"
            lock = "🔒 " if img.get("protection", {}).get("delete") else ""
            size = img.get("image_size") or 0
            label = img.get("description") or img.get("name") or str(img["id"])
            keyboard.append([InlineKeyboardButton(
                f"{s_emoji} {lock}{label} | {size:.1f} GB",
                callback_data=f"snap_{img['id']}",
            )])

    text += f"\n\n🕓 Updated: `{datetime.now().strftime('%H:%M:%S')}`"
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"srvsnap_{server_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Server", callback_data=f"server_{server_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def snapshot_pick_server(query, context):
    servers = await _get_api(context).list_servers()
    if not servers:
        await query.edit_message_text("⚠️ No servers found or API error occurred.")
        return
    keyboard = []
    for s in servers:
        loc_name, flag = get_location_info(server_location(s))
        keyboard.append([InlineKeyboardButton(
            f"🖥 {s.get('name', 'Unnamed')} | {flag} {loc_name}",
            callback_data=f"snapcreate_{s['id']}",
        )])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Snapshots", callback_data="snapshots")])
    await query.edit_message_text(
        "📸 *Take Snapshot*\n\nChoose a server:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def create_snapshot(query, context, server_id):
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    api = _get_api(context)
    name = server.get("name", "Server")
    description = f"{name} {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    await query.edit_message_text(f"📸 Creating snapshot of `{name}`...", parse_mode="Markdown")
    result = await api.create_snapshot(server_id, description)

    keyboard = [
        [InlineKeyboardButton("📸 View Server Snapshots", callback_data=f"srvsnap_{server_id}"),
         InlineKeyboardButton("⏳ Progress", callback_data="running")],
        [InlineKeyboardButton("🖥 Back to Server", callback_data=f"server_{server_id}")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="start_menu")],
    ]
    if result:
        await query.edit_message_text(
            f"⏳ *Snapshot creation started*\n\n"
            f"🖥 Server: `{name}`\n"
            f"🏷 Name: `{description}`\n\n"
            f"The server keeps running. It can take several minutes — "
            f"the snapshot shows ⏳ in the list until it becomes ✅ available "
            f"(⏳ Progress shows how far it is). Once ready, 🧬 on the snapshot makes a "
            f"new server from it.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            "❌ Failed to start snapshot creation. Check the logs and try again.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


router.add(r"snapshots", show_snapshots)
router.add(r"snap_new", snapshot_pick_server)
router.add(r"snapcreate_(\d+)", create_snapshot, int)
router.add(r"snapdel_confirm_(\d+)", delete_snapshot, int)
router.add(r"snapdel_(\d+)", delete_snapshot_confirm, int)
router.add(r"snap_(\d+)", show_snapshot_detail, int)
router.add(r"srvsnap_(\d+)", show_server_snapshots, int)
