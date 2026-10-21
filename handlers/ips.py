"""Floating IPs and Primary IPs: per-server assignment plus bulk create/delete.

The list/create/delete flows are shared between both kinds via a ``kind``
argument ("fip" or "pip"); the per-kind routes are registered in a loop at
the bottom of this module.
"""
import asyncio
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils import get_location_info, floating_ip_command, copy_button

from .router import router
from .context import _get_api, _resolve_server
from .ui import send_not_found, account_header, account_switch_rows
from .pricing import _floating_ip_price, _primary_ip_price


async def show_server_fips(query, context, server_id):
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    api = _get_api(context)
    fips, servers = await asyncio.gather(
        api.list_floating_ips(),
        api.list_servers(),
    )
    server_names = {s["id"]: s.get("name", "?") for s in servers}

    text = f"🌐 *Floating IPs* — `{server.get('name')}`\n\n"
    keyboard = []
    if not fips:
        text += "No floating IPs exist yet. Create one from the main menu → 🌐 Floating IPs.\n"
    else:
        for f in fips:
            owner = f.get("server")
            if owner == server_id:
                state = "🔗 attached to this server"
                btn = InlineKeyboardButton(f"🔌 Detach {f.get('ip')}", callback_data=f"fipun_{f['id']}_{server_id}")
            elif owner:
                state = f"🔗 on `{server_names.get(owner, owner)}`"
                btn = InlineKeyboardButton(f"🔗 Move {f.get('ip')} here", callback_data=f"fipas_{f['id']}_{server_id}")
            else:
                state = "🆓 unassigned"
                btn = InlineKeyboardButton(f"🔗 Attach {f.get('ip')}", callback_data=f"fipas_{f['id']}_{server_id}")
            text += f"• `{f.get('ip')}` ({f.get('type')}) — {state}\n"
            row = [btn]
            if owner == server_id:
                text += f"  `{floating_ip_command(f)}`\n"
                row.append(copy_button("📋 Copy command", floating_ip_command(f)))
            keyboard.append(row)
        if any(f.get("server") == server_id for f in fips):
            text += ("\n📋 Run the command under an attached IP on the server (tap it to "
                     "copy) so it answers on that IP. It lasts until the next reboot.\n")

    text += f"\n🕓 Updated: `{datetime.now().strftime('%H:%M:%S')}`"
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"srvfip_{server_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Server", callback_data=f"server_{server_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def server_fip_assign(query, context, fip_id, server_id):
    await query.edit_message_text("🔗 Attaching floating IP...")
    await _get_api(context).assign_floating_ip(fip_id, server_id)
    await asyncio.sleep(2)
    await show_server_fips(query, context, server_id)


async def server_fip_unassign(query, context, fip_id, server_id):
    await query.edit_message_text("🔌 Detaching floating IP...")
    await _get_api(context).unassign_floating_ip(fip_id)
    await asyncio.sleep(2)
    await show_server_fips(query, context, server_id)
_IP_LABEL = {"fip": ("🌐", "Floating IP"), "pip": ("📍", "Primary IP")}


async def _fetch_ips(kind, api):
    if kind == "fip":
        return await api.list_floating_ips()
    return await api.list_primary_ips()


def _ip_assignee_id(kind, ip):
    return ip.get("server") if kind == "fip" else ip.get("assignee_id")


def _ip_location_name(kind, ip):
    if kind == "fip":
        return ip.get("home_location", {}).get("name", "")
    # Primary IPs carry a top-level location; older payloads nested it
    # under datacenter.
    return ((ip.get("location") or {}).get("name")
            or (ip.get("datacenter") or {}).get("location", {}).get("name", ""))


async def show_ip_list(query, context, kind):
    api = _get_api(context)
    emoji, label = _IP_LABEL[kind]
    ips, pricing, servers = await asyncio.gather(
        _fetch_ips(kind, api),
        api.get_pricing(),
        api.list_servers(),
    )
    server_names = {s["id"]: s.get("name", "?") for s in servers}

    existing_ids = {ip["id"] for ip in ips}
    sel = {i for i in context.user_data.get(f"{kind}_sel", set()) if i in existing_ids}
    context.user_data[f"{kind}_sel"] = sel

    text = f"{emoji} *{label}s*\n\n{account_header(context)}"
    keyboard = [[InlineKeyboardButton(f"➕ Create {label}s", callback_data=f"{kind}_new")]]

    if not ips:
        text += f"No {label.lower()}s yet.\n"
    else:
        total_cost = 0
        for ip in ips:
            aid = _ip_assignee_id(kind, ip)
            loc = _ip_location_name(kind, ip)
            _, flag = get_location_info(loc)
            if kind == "fip":
                price = _floating_ip_price(pricing, ip)
            else:
                price = 0 if aid else _primary_ip_price(pricing, ip)
            total_cost += price
            attach = f"🔗 {server_names.get(aid, aid)}" if aid else "🆓 unassigned"
            cost_str = "free (on server)" if (kind == "pip" and aid) else f"€{price:.2f}/mo"
            text += f"• `{ip.get('ip')}` {flag} {ip.get('type')} | {attach} | {cost_str}\n"
            row = [InlineKeyboardButton(
                f"{'✅' if ip['id'] in sel else '⬜'} {ip.get('ip')}",
                callback_data=f"{kind}tog_{ip['id']}",
            )]
            if kind == "fip" and ip["id"] in sel:
                row.append(copy_button("📋 Copy command", floating_ip_command(ip)))
            keyboard.append(row)
        text += f"\nTotal: {len(ips)} | €{total_cost:.2f}/month\n"
        chosen = [ip for ip in ips if ip["id"] in sel]
        if kind == "fip" and chosen:
            text += ("\n📋 *Run on the server* (tap to copy; lasts until the next reboot):\n"
                     + "".join(f"`{floating_ip_command(ip)}`\n" for ip in chosen))
        text += ("\nTap an IP to select it (and see its command); delete the selected ones together."
                 if kind == "fip" else "\nTap IPs to select them, then delete together.")

    if sel:
        keyboard.append([
            InlineKeyboardButton(f"🗑 Delete Selected ({len(sel)})", callback_data=f"{kind}delsel"),
            InlineKeyboardButton("♻️ Clear", callback_data=f"{kind}clear"),
        ])
    text += f"\n\n🕓 Updated: `{datetime.now().strftime('%H:%M:%S')}`"
    keyboard += account_switch_rows(context, f"{kind}s")
    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"{kind}s")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="start_menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def ip_toggle(query, context, kind, ip_id):
    sel = context.user_data.setdefault(f"{kind}_sel", set())
    sel.symmetric_difference_update({ip_id})
    await show_ip_list(query, context, kind)


async def ip_new_type(query, context, kind):
    emoji, label = _IP_LABEL[kind]
    keyboard = [
        [
            InlineKeyboardButton("IPv4", callback_data=f"{kind}newt_ipv4"),
            InlineKeyboardButton("IPv6", callback_data=f"{kind}newt_ipv6"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"{kind}s")],
    ]
    await query.edit_message_text(
        f"{emoji} *Create {label}s*\n\nChoose the IP type:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def ip_new_place(query, context, kind, ip_type):
    api = _get_api(context)
    emoji, label = _IP_LABEL[kind]
    # Both kinds are created per location (Hetzner removed datacenters).
    places = [loc.get("name", "") for loc in await api.list_locations()]
    keyboard = []
    row = []
    for place in places:
        if not place:
            continue
        _, flag = get_location_info(place)
        row.append(InlineKeyboardButton(f"{flag} {place}", callback_data=f"{kind}newl_{ip_type}_{place}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"{kind}_new")])
    await query.edit_message_text(
        f"{emoji} *Create {label}s* ({ip_type})\n\nChoose the location:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def ip_new_count(query, context, kind, ip_type, place):
    emoji, label = _IP_LABEL[kind]
    keyboard = [
        [InlineKeyboardButton(str(n), callback_data=f"{kind}newc_{ip_type}_{place}_{n}")
         for n in (1, 2, 3, 5)],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"{kind}newt_{ip_type}")],
    ]
    await query.edit_message_text(
        f"{emoji} *Create {label}s* ({ip_type} @ {place})\n\nHow many?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def ip_create(query, context, kind, ip_type, place, count):
    api = _get_api(context)
    emoji, label = _IP_LABEL[kind]
    await query.edit_message_text(f"{emoji} Creating {count} {label.lower()}(s)...")
    stamp = datetime.now().strftime('%y%m%d%H%M%S')
    lines = []
    ok = 0
    for i in range(count):
        name = f"{kind}-{stamp}-{i + 1}"
        if kind == "fip":
            result = await api.create_floating_ip(ip_type, place, name)
            created = (result or {}).get("floating_ip", {})
        else:
            result = await api.create_primary_ip(ip_type, place, name)
            created = (result or {}).get("primary_ip", {})
        if result:
            ok += 1
            lines.append(f"✅ `{created.get('ip', name)}`")
            if kind == "fip" and created.get("ip"):
                lines.append(f"   `{floating_ip_command(created)}`")
        else:
            lines.append(f"❌ {name} — creation failed")
    text = (
        f"{emoji} *Create {label}s — done ({ok}/{count})*\n\n" + "\n".join(lines)
    )
    if kind == "fip" and ok:
        text += ("\n\n📋 Assign the IP to a server, then run its command there "
                 "(tap to copy).")
    keyboard = [
        [InlineKeyboardButton(f"{emoji} View {label}s", callback_data=f"{kind}s")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="start_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def ip_delete_confirm(query, context, kind):
    api = _get_api(context)
    emoji, label = _IP_LABEL[kind]
    sel = context.user_data.get(f"{kind}_sel", set())
    if not sel:
        await show_ip_list(query, context, kind)
        return
    ips = await _fetch_ips(kind, api)
    chosen = [ip for ip in ips if ip["id"] in sel]
    lines = "\n".join(f"• `{ip.get('ip')}`" for ip in chosen)
    note = ""
    if kind == "pip" and any(_ip_assignee_id(kind, ip) for ip in chosen):
        note = "\n⚠️ Primary IPs attached to a server cannot be deleted — those will fail."
    keyboard = [
        [
            InlineKeyboardButton(f"✅ Yes, delete {len(chosen)}", callback_data=f"{kind}delsel_yes"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"{kind}s"),
        ]
    ]
    await query.edit_message_text(
        f"🗑 *Delete {label}s*\n\n{lines}\n{note}\n"
        f"⚠️ This cannot be undone. Are you sure?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def ip_delete_selected(query, context, kind):
    api = _get_api(context)
    emoji, label = _IP_LABEL[kind]
    sel = context.user_data.get(f"{kind}_sel", set())
    if not sel:
        await show_ip_list(query, context, kind)
        return
    await query.edit_message_text(f"🗑 Deleting {len(sel)} {label.lower()}(s)...")
    ips = await _fetch_ips(kind, api)
    ip_by_id = {ip["id"]: ip for ip in ips}
    lines = []
    ok = 0
    for ip_id in sorted(sel):
        addr = ip_by_id.get(ip_id, {}).get("ip", ip_id)
        if kind == "fip":
            result = await api.delete_floating_ip(ip_id)
        else:
            result = await api.delete_primary_ip(ip_id)
        if result is not None:
            ok += 1
            lines.append(f"✅ `{addr}` deleted")
        else:
            lines.append(f"❌ `{addr}` failed (still attached?)")
    context.user_data[f"{kind}_sel"] = set()
    text = f"🗑 *Delete {label}s — done ({ok}/{len(lines)})*\n\n" + "\n".join(lines)
    keyboard = [
        [InlineKeyboardButton(f"{emoji} View {label}s", callback_data=f"{kind}s")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="start_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def ip_clear(query, context, kind):
    context.user_data[f"{kind}_sel"] = set()
    await show_ip_list(query, context, kind)


router.add(r"srvfip_(\d+)", show_server_fips, int)
router.add(r"fipas_(\d+)_(\d+)", server_fip_assign, int, int)
router.add(r"fipun_(\d+)_(\d+)", server_fip_unassign, int, int)

for _kind in ("fip", "pip"):
    router.add(fr"{_kind}s", lambda q, c, k=_kind: show_ip_list(q, c, k))
    router.add(fr"{_kind}_new", lambda q, c, k=_kind: ip_new_type(q, c, k))
    router.add(fr"{_kind}newt_([^_]+)", lambda q, c, t, k=_kind: ip_new_place(q, c, k, t))
    router.add(fr"{_kind}newl_([^_]+)_([^_]+)", lambda q, c, t, p, k=_kind: ip_new_count(q, c, k, t, p))
    router.add(fr"{_kind}newc_([^_]+)_([^_]+)_(\d+)", lambda q, c, t, p, n, k=_kind: ip_create(q, c, k, t, p, int(n)))
    router.add(fr"{_kind}tog_(\d+)", lambda q, c, i, k=_kind: ip_toggle(q, c, k, int(i)))
    router.add(fr"{_kind}delsel_yes", lambda q, c, k=_kind: ip_delete_selected(q, c, k))
    router.add(fr"{_kind}delsel", lambda q, c, k=_kind: ip_delete_confirm(q, c, k))
    router.add(fr"{_kind}clear", lambda q, c, k=_kind: ip_clear(q, c, k))
