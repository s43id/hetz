"""🔁 Change IPv4 (🌐 Network → 🔁 Change IPv4): give a server another public IPv4.

``ipchg_<sid>`` shows the current IP and the choices; ``ipchgo_<sid>_<how>_<keep>``
runs ip_change.change_ipv4 with one status line, then the result with a copy
button. ``how`` = ``n`` (a new IP) or the id of a kept primary IP in the
server's location; ``keep`` = 1 keeps the old IP (unassigned, billed), 0
deletes it.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import ip_change
from server_manager import is_busy
from utils import copy_button, escape_markdown, server_location

from .router import router
from .context import _get_api, _resolve_server
from .ui import send_not_found
from .pricing import _primary_ip_price
from .servers import _node_tags, _server_ip

MAX_KEPT = 5


async def ipchange_confirm(query, context, server_id):
    server = await _resolve_server(context, server_id, fresh=True)
    if not server:
        await send_not_found(query)
        return
    api = _get_api(context)
    back = [InlineKeyboardButton("⬅️ Back to Network", callback_data=f"srvnet_{server_id}")]
    old = ip_change.current_ipv4(server)
    if not old:
        await query.edit_message_text("⚠️ This server has no public IPv4 to change.",
                                      reply_markup=InlineKeyboardMarkup([back]))
        return
    if is_busy(server_id):
        await query.edit_message_text(
            "⏳ A reset, plan change or IP change is running on this server; try again "
            "when it is done.", reply_markup=InlineKeyboardMarkup([back]))
        return
    loc = server_location(server)
    kept = (await ip_change.kept_ipv4s(api, loc))[:MAX_KEPT]
    price = _primary_ip_price(await api.get_pricing(), {"type": "ipv4", "location": {"name": loc}})
    panel = (await _node_tags()).get(_server_ip(server))
    text = (
        f"🔁 *Change IPv4* — `{server.get('name')}`\n\n"
        f"Now: `{old['ip']}`\n\n"
        f"The server gets another public IPv4. It is *powered off for about a minute*. "
        f"Everything that points at the old address (DNS records, clients"
        f"{', the node address in the panel' if panel else ''}) has to be changed.\n"
    )
    if panel:
        text += f"\n🧩 This server is a node of panel `{panel}`: change its address there too.\n"
    keep_note = f" (€{price:.2f}/month)" if price else ""
    text += (f"\nKeep the old IP{keep_note} to use it again later (📍 Primary IPs, or for a "
             f"new server in this location); otherwise it is deleted.")
    keyboard = [
        [InlineKeyboardButton("🆕 New IP, delete the old one",
                              callback_data=f"ipchgo_{server_id}_n_0")],
        [InlineKeyboardButton("🆕 New IP, keep the old one",
                              callback_data=f"ipchgo_{server_id}_n_1")],
    ]
    for p in kept:
        keyboard.append([InlineKeyboardButton(
            f"📍 Use kept {p.get('ip')}, delete the old one",
            callback_data=f"ipchgo_{server_id}_{p['id']}_0")])
    keyboard.append(back)
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def ipchange_go(query, context, server_id, how, keep):
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    name = server.get("name", str(server_id))
    shown = []

    async def log(line):
        text = f"🔁 *Change IPv4* — `{name}`\n\n⏳ {escape_markdown(line)}"
        if shown and shown[-1] == text:
            return
        shown.append(text)
        try:
            await query.edit_message_text(text, parse_mode="Markdown")
        except Exception:
            pass

    await log("Starting…")
    result = await ip_change.change_ipv4(_get_api(context), server_id, keep_old=bool(keep),
                                         use_ip=None if how == "n" else int(how), log=log)
    keyboard = []
    if result['ok']:
        text = (f"✅ *IPv4 changed* — `{name}`\n\n"
                f"`{result['old_ip']}` → `{result['new_ip']}`\n")
        if result['kept']:
            text += f"\n📍 `{result['old_ip']}` was kept as a free primary IP (billed monthly).\n"
        if result['warning']:
            text += f"\n⚠️ {escape_markdown(result['warning'])}\n"
        text += "\nUpdate DNS records and anything else that used the old address."
        keyboard.append([copy_button("📋 Copy new IP", result['new_ip'])])
    else:
        text = (f"❌ *IPv4 not changed* — `{name}`\n\n{escape_markdown(result['error'] or '?')}"
                f"\n\nThe server keeps its old address and its power state.")
    keyboard.append([InlineKeyboardButton("🌐 Network", callback_data=f"srvnet_{server_id}"),
                     InlineKeyboardButton("🖥 Server", callback_data=f"server_{server_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


router.add(r"ipchg_(\d+)", ipchange_confirm, int)
router.add(r"ipchgo_(\d+)_(n|\d+)_([01])", ipchange_go, int, None, int)
