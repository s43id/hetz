"""Server list, server detail, power, traffic reset, password & backups."""
import asyncio
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from config import Config
from hetzner_api import api_registry
from utils import (
    format_traffic, get_traffic_emoji, get_location_info,
    included_traffic_tb, overage_tb, overage_cost, overage_price_per_tb,
    server_location, server_disk_gb, escape_markdown, traffic_bar, traffic_fraction,
    copy_button, ipv6_host,
)
import auto_reset
from reset_schedule import (
    reset_with_return, log_text, list_schedules, describe, fmt_time,
    remove_schedules_for, progress_text, summary_text, details_button, last_log,
)
from price_overrides import get_override, set_override, clear_override
from plan_watch import list_watches, is_return, remove_watches_for
from shell_handler import console_entry, _close_session
import node_setup
import ip_change
from server_manager import activity
from health import command_label

from .router import router
from .context import _get_api, _current_account, _resolve_server
from .ui import send_not_found
from .pricing import _server_type_price


async def _start_console(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != Config.ADMIN_ID:
        await query.answer("⛔ Unauthorized", show_alert=True)
        return ConversationHandler.END
    server_id = int(query.data.split("_")[1])
    server = await _resolve_server(context, server_id)
    if not server:
        await query.answer()
        await send_not_found(query)
        return ConversationHandler.END
    if server.get("status") != "running":
        # Answer with the alert itself: Telegram shows only the first response
        # to a callback, so acking beforehand would swallow this warning.
        await query.answer("⚠️ Server must be RUNNING to open a console.", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    ip = server.get("public_net", {}).get("ipv4", {}).get("ip", "")
    name = server.get("name", "Server")
    return await console_entry(query, context, server_id, ip, name)


LIST_PAGE_SIZE = 10


async def _node_tags():
    """{address: panel} for the 🧩 tags; empty without panels or on any error."""
    if not node_setup.can_register():
        return {}
    try:
        return await node_setup.node_panels()
    except Exception:
        return {}


def _server_ip(server):
    return (server or {}).get("public_net", {}).get("ipv4", {}).get("ip", "")


async def show_server_list(query, context, page=0):
    all_names = api_registry.names()
    account_filter = context.user_data.get('server_filter')

    # Determine which accounts to fetch
    if account_filter and account_filter in all_names:
        fetch_names = [account_filter]
    else:
        fetch_names = all_names

    results, tags = await asyncio.gather(
        asyncio.gather(*[api_registry.get(n).list_servers() for n in fetch_names]),
        _node_tags(),
    )

    all_servers = []
    server_account_map = context.user_data.get('server_account_map', {})
    for account_name, servers in zip(fetch_names, results):
        for s in servers:
            all_servers.append((account_name, s))
            server_account_map[s['id']] = account_name
    context.user_data['server_account_map'] = server_account_map

    if not all_servers:
        # Surface the real reason per account so a bad token isn't mistaken
        # for an empty project.
        reasons = await asyncio.gather(
            *[api_registry.get(n).diagnose_servers() for n in fetch_names]
        )
        detail = "\n".join(f"• `{n}`: {escape_markdown(r)}" for n, r in zip(fetch_names, reasons))
        # Offer to create a server right here. Creation targets the current
        # account, so name the button after it when a single one is in scope.
        target = account_filter if account_filter in all_names else _current_account(context)
        create_label = f"➕ Create Server in {target}" if target else "➕ Create Server"
        keyboard = [
            [InlineKeyboardButton(create_label, callback_data="srvc")],
            [InlineKeyboardButton("🔄 Retry", callback_data="list_servers")],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="start_menu")],
        ]
        await query.edit_message_text(
            f"⚠️ *No servers to show*\n\n{detail}\n\n"
            f"`unauthorized`/`403` → the account token is wrong or expired.\n"
            f"`ok — 0 server(s)` → the account is empty; tap *Create Server*.\n"
            f"`no response` → a temporary network/rate-limit hiccup; tap Retry.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    sort = context.user_data.get('list_sort', 'traffic')
    near_only = context.user_data.get('list_near', False)
    if sort == 'traffic':
        all_servers.sort(key=lambda x: -traffic_fraction(x[1]))
    else:
        all_servers.sort(key=lambda x: (x[1].get('name') or '').lower())
    near = [x for x in all_servers if traffic_fraction(x[1]) >= Config.WARNING_THRESHOLD]
    shown = near if near_only else all_servers

    total_pages = max(1, (len(shown) - 1) // LIST_PAGE_SIZE + 1)
    page = min(page, total_pages - 1)
    page_items = shown[page * LIST_PAGE_SIZE:(page + 1) * LIST_PAGE_SIZE]

    show_account_tag = len(fetch_names) > 1
    keyboard = [[InlineKeyboardButton("➕ Create Server", callback_data="srvc")]]
    keyboard.append([
        InlineKeyboardButton(
            "↕️ Sort: traffic" if sort == 'traffic' else "↕️ Sort: name",
            callback_data="lsort_name" if sort == 'traffic' else "lsort_traffic"),
        InlineKeyboardButton(
            f"{'✅' if near_only else '🔥'} Near limit ({len(near)})", callback_data="lnear"),
    ])
    for account_name, s in page_items:
        frac = traffic_fraction(s)
        emoji = get_traffic_emoji(frac * included_traffic_tb(s), included_traffic_tb(s))
        _, flag = get_location_info(server_location(s))
        panel = tags.get(_server_ip(s))
        label = (f"{emoji} {s.get('name', 'Unnamed')}{f' 🧩{panel}' if panel else ''} | {flag} | "
                 f"{traffic_bar(frac, 5)} {frac * 100:.0f}%")
        if show_account_tag:
            label = f"[{account_name}] {label}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"server_{s['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"page_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="start_menu")])

    title = f"{account_filter} — Servers" if account_filter else "All Servers"
    header = (f"📋 *{escape_markdown(title)}* ({len(all_servers)})\n"
              f"Sorted by {sort}"
              + (f" · 🔥 only the {len(near)} at {Config.WARNING_THRESHOLD:.0%}+ of their traffic"
                 if near_only else "")
              + (f" · page {page + 1}/{total_pages}" if total_pages > 1 else ""))
    if near_only and not near:
        header += f"\n\nNo server is at {Config.WARNING_THRESHOLD:.0%} of its traffic or more."

    await query.edit_message_text(
        header,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def set_list_sort(query, context, sort):
    context.user_data['list_sort'] = sort
    await show_server_list(query, context)


async def toggle_near_limit(query, context):
    context.user_data['list_near'] = not context.user_data.get('list_near', False)
    await show_server_list(query, context)


async def _show_all_servers(query, context, page=0):
    context.user_data.pop('server_filter', None)
    await show_server_list(query, context, page)


async def show_server_detail(query, context, server_id, refresh=False):
    server = await _resolve_server(context, server_id, fresh=refresh)
    if not server:
        await send_not_found(query)
        return

    name   = server.get("name", "Unnamed")
    status = server.get("status", "unknown")
    stype  = server.get("server_type", {}).get("name", "Unknown")
    loc_name, flag = get_location_info(server_location(server))
    traffic_bytes = server.get("outgoing_traffic", 0)
    traffic_tb    = traffic_bytes / (1024 ** 4)
    limit_tb      = included_traffic_tb(server)
    traffic_pct   = (traffic_tb / limit_tb) * 100 if limit_tb else 0
    emoji         = get_traffic_emoji(traffic_tb, limit_tb)
    overage_eur   = overage_cost(server)
    per_tb        = overage_price_per_tb(server)
    ip     = server.get("public_net", {}).get("ipv4", {}).get("ip", "N/A")
    cores  = server.get("server_type", {}).get("cores", "N/A")
    memory = server.get("server_type", {}).get("memory", "N/A")
    plan_disk = server.get("server_type", {}).get("disk")
    disk   = server_disk_gb(server) or "N/A"
    # A past disk upgrade leaves the server bigger than its plan says.
    disk_note = f" (plan {plan_disk} GB)" if plan_disk and disk != plan_disk else ""

    loc_code = server_location(server)
    list_price = _server_type_price(server.get("server_type", {}), loc_code)
    override = get_override(_current_account(context), server_id)
    price_eur = list_price if override is None else override
    monthly_price = f"€{price_eur:.2f}" if price_eur else "N/A"
    # Shown after the "/month" suffix so the figure itself stays readable.
    price_note = f" ✏️ your price (list `€{list_price:.2f}`)" if override is not None else ""

    status_emoji = "🟢" if status == "running" else "🔴" if status == "off" else "🟡"
    backups_on = bool(server.get("backup_window"))
    protected = is_protected(server)

    text = (
        f"🖥️ *{name}*\n\n"
        f"📍 Location: `{flag} {loc_name}`\n"
        f"🔧 Type: `{stype}`\n"
        f"💻 CPU: `{cores} cores` | RAM: `{memory} GB` | Disk: `{disk} GB`{disk_note}\n"
        f"🌐 IP: `{ip}`\n"
        f"{status_emoji} Status: `{status.upper()}`\n"
        f"💾 Backups: `{'ON' if backups_on else 'OFF'}`"
        f"{' · 🛡 Protected' if protected else ''}"
        f"{' · 🔒 Locked by Hetzner' if server.get('locked') else ''}\n\n"
        f"💰 *Pricing*\n"
        f"📦 Server Cost: `{monthly_price}/month`{price_note}\n"
        f"📊 Overage Cost: `€{overage_eur:.2f}` (€{per_tb:.2f}/TB)\n\n"
        f"{emoji} *Traffic Usage*\n"
        f"📊 `{traffic_bar(traffic_pct / 100)}` {traffic_pct:.1f}%\n"
        f"      {format_traffic(traffic_bytes, limit_tb)}\n"
    )
    panel = (await _node_tags()).get(_server_ip(server))
    if panel:
        text += f"🧩 Node of panel `{panel}`\n"
    for sched in list_schedules(server_id):
        text += f"⏰ Reset {describe(sched)} — next `{fmt_time(sched['next_run'])}`\n"
    auto_line = auto_reset.server_line(server_id)
    if auto_line:
        text += auto_line + "\n"
    for w in list_watches():
        if w.get("server_id") == server_id and is_return(w) and w.get("plan") != stype:
            text += f"↩️ Waiting to move back to `{w.get('plan')}` (left here by a reset)\n"
    text += await _running_lines(server, _get_api(context))

    power_btn = InlineKeyboardButton(
        "🔴 Power OFF" if status == "running" else "🟢 Power ON",
        callback_data=f"poweroff_{server_id}" if status == "running" else f"poweron_{server_id}",
    )
    # Everyday actions up front; the rest sit under ⚙️ Manage and 🌐 Network.
    keyboard = [
        [
            power_btn,
            InlineKeyboardButton("💻 SSH Console", callback_data=f"console_{server_id}"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{server_id}"),
        ],
        [
            InlineKeyboardButton("♻️ Reset Traffic", callback_data=f"reset_{server_id}"),
            InlineKeyboardButton("⏰ Reset Schedule", callback_data=f"rsch_{server_id}"),
        ],
        [
            InlineKeyboardButton("⬇️ Prev Plan", callback_data=f"resizenav_{server_id}_prev"),
            InlineKeyboardButton("⚖️ Change Plan", callback_data=f"resize_{server_id}"),
            InlineKeyboardButton("⬆️ Next Plan", callback_data=f"resizenav_{server_id}_next"),
        ],
        [
            InlineKeyboardButton("⚙️ Manage", callback_data=f"srvmng_{server_id}"),
            InlineKeyboardButton("🌐 Network", callback_data=f"srvnet_{server_id}"),
        ]
        + ([copy_button("📋 Copy IP", ip)] if ip != "N/A" else []),
        [InlineKeyboardButton("⬅️ Back to List", callback_data="list_servers")],
    ]

    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
    )


def is_protected(server):
    """Delete / rebuild protection is on."""
    prot = (server or {}).get("protection") or {}
    return bool(prot.get("delete") or prot.get("rebuild"))


async def _running_lines(server, api):
    """'⏳ …' lines for the server page: the bot's own work and, while Hetzner
    has the server locked, its running actions with their progress."""
    sid = server.get("id")
    lines = ""
    for a in activity():
        if a["server_id"] == sid:
            step = f" (step {a['step']}/{a['steps']})" if a.get("steps") and a.get("step") else ""
            lines += f"⏳ {escape_markdown(a['title'])}{step}: {escape_markdown(a['stage'])}\n"
    if server.get("locked") and api is not None:
        actions = await api.list_server_actions(status="running", server_id=sid)
        for a in actions or []:
            lines += f"⏳ Hetzner: {command_label(a.get('command'))} {a.get('progress') or 0}%\n"
    return lines


async def show_manage(query, context, server_id, notice=""):
    """⚙️ Manage: the less frequent and the destructive server actions."""
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    backups_on = bool(server.get("backup_window"))
    protected = is_protected(server)
    keyboard = [
        [
            InlineKeyboardButton("🔧 Rebuild OS", callback_data=f"rebuild_{server_id}"),
            InlineKeyboardButton("📸 Snapshots", callback_data=f"srvsnap_{server_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Rename", callback_data=f"rename_{server_id}"),
            InlineKeyboardButton("💶 Edit Price", callback_data=f"price_{server_id}"),
        ],
        [
            InlineKeyboardButton(
                f"💾 Backups: {'ON 🟢' if backups_on else 'OFF 🔴'}",
                callback_data=f"backup_{server_id}",
            ),
            InlineKeyboardButton("🔑 Reset Password", callback_data=f"resetpw_{server_id}"),
        ],
        [InlineKeyboardButton(
            "🛡 Protection: ON 🟢" if protected else "🛡 Protection: OFF",
            callback_data=f"prot_{server_id}_{0 if protected else 1}")],
        [InlineKeyboardButton("🗑 Delete Server", callback_data=f"delete_{server_id}")],
        [InlineKeyboardButton("⬅️ Back to Server", callback_data=f"server_{server_id}")],
    ]
    text = f"⚙️ *Manage* — `{server.get('name')}`\n\n"
    if notice:
        text += notice + "\n\n"
    text += "Rebuild, snapshots, name, price, backups, root password, protection, delete.\n\n"
    text += ("🛡 Protection is *on*: Hetzner refuses to delete or rebuild this server "
             "until it is turned off." if protected else
             "🛡 Protection is off. Turn it on and Hetzner refuses to delete or rebuild the "
             "server (by mistake, from the bot, the Mini App or the console).")
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def set_protection(query, context, server_id, on):
    """🛡 Delete + rebuild protection on / off, then back to ⚙️ Manage."""
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    result = await _get_api(context).change_server_protection(server_id, bool(on),
                                                               return_error=True)
    if result is None or (isinstance(result, dict) and result.get("error")):
        msg = (result or {}).get("error", {}).get("message", "no answer") \
            if isinstance(result, dict) else "no answer"
        notice = f"❌ Protection not changed: {escape_markdown(msg)}"
    else:
        await asyncio.sleep(2)
        notice = "🛡 Protection on." if on else "🛡 Protection off."
    await show_manage(query, context, server_id, notice)


async def send_protected(query, server_id, name, what):
    """A delete / rebuild refused before asking: the server is protected."""
    await query.edit_message_text(
        f"🛡 *Protected* — `{name}`\n\nThis server has delete and rebuild protection on, so "
        f"Hetzner refuses to {what} it. Turn the protection off first if you really mean it.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛡 Turn protection off", callback_data=f"prot_{server_id}_0")],
            [InlineKeyboardButton("⬅️ Back to Server", callback_data=f"server_{server_id}")],
        ]),
        parse_mode="Markdown")


async def show_network(query, context, server_id):
    """🌐 Network: addresses (with copy buttons), floating IPs, volumes, panel."""
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    ipv4 = _server_ip(server)
    ipv6_net = (server.get("public_net", {}).get("ipv6") or {}).get("ip", "")
    ipv6 = (ipv6_host(ipv6_net) or ("",))[0] if ipv6_net else ""
    text = f"🌐 *Network* — `{server.get('name')}`\n\n"
    text += f"IPv4: `{ipv4}`\n" if ipv4 else "IPv4: none\n"
    text += f"IPv6: `{ipv6}`\n" if ipv6 else "IPv6: off\n"
    copies = []
    if ipv4:
        copies.append(copy_button("📋 Copy IPv4", ipv4))
    if ipv6:
        copies.append(copy_button("📋 Copy IPv6", ipv6))
    keyboard = [copies] if copies else []
    keyboard.append([
        InlineKeyboardButton("🌐 Floating IPs", callback_data=f"srvfip_{server_id}"),
        InlineKeyboardButton("💽 Volumes", callback_data=f"volmenu_{server_id}"),
    ])
    if ipv4:
        keyboard.append([InlineKeyboardButton("🔁 Change IPv4", callback_data=f"ipchg_{server_id}")])
    if node_setup.can_register():
        keyboard.append([InlineKeyboardButton("🧩 Add to Panel", callback_data=f"nodeadd_{server_id}")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Server", callback_data=f"server_{server_id}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def power_action(query, context, server_id, action):
    api = _get_api(context)
    await query.edit_message_text(f"⚙️ {'Starting' if action == 'on' else 'Stopping'} server...")
    result = await (api.power_on(server_id) if action == "on" else api.power_off(server_id))
    if result:
        await api.wait_for_status(server_id, "running" if action == "on" else "off")
        await show_server_detail(query, context, server_id, refresh=True)
    else:
        await query.edit_message_text("❌ Power action failed. Please try again.")


async def reset_traffic_confirm(query, context, server_id):
    """Ask before a reset: it powers the server off for a few minutes."""
    server = await _resolve_server(context, server_id, fresh=True)
    if not server:
        await send_not_found(query)
        return
    limit_tb = included_traffic_tb(server)
    traffic = server.get("outgoing_traffic", 0)
    plan = server.get("server_type", {}).get("name", "?")
    over = overage_tb(server)
    text = (
        f"♻️ *Reset Traffic*\n\n"
        f"Server: `{server.get('name')}` (`{plan}`)\n"
        f"Traffic: `{format_traffic(traffic, limit_tb)}`\n"
    )
    if over > 0:
        text += f"Overage so far: `€{overage_cost(server):.2f}` — saved to the cost history.\n"
    text += (
        f"\nThe server moves to another plan and back, which zeroes the "
        f"counter. It is *powered off for a few minutes*, twice.\n\n"
        f"Start the reset?"
    )
    keyboard = [[
        InlineKeyboardButton("✅ Yes, reset", callback_data=f"resetgo_{server_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"server_{server_id}"),
    ]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def reset_traffic(query, context, server_id):
    # Act on the server's own account, whatever is selected now.
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    name = server.get("name", str(server_id))
    await query.edit_message_text(progress_text(name, [], (1, "Checking the server")),
                                  parse_mode="Markdown")
    shown = []

    async def update_progress(logs, stage):
        # One status line instead of the whole log; edit only when it changes.
        text = progress_text(name, logs, stage)
        if shown and shown[-1] == text:
            return
        shown.append(text)
        try:
            await query.edit_message_text(text, parse_mode="Markdown")
        except Exception:
            pass

    result = await reset_with_return(_get_api(context), _current_account(context),
                                     server_id, query.message.chat_id, update_progress)
    final = summary_text("♻️ *Traffic Reset*", name, result)

    keyboard = []
    if result['home']:
        # Stranded on another plan: offer a one-tap retry of the way back.
        keyboard.append([InlineKeyboardButton(
            f"⚖️ Retry moving to {result['home']}",
            callback_data=f"resizet_{server_id}_{result['home']}",
        )])

    keyboard.append([
        details_button(server_id),
        InlineKeyboardButton("🔄 Refresh Status", callback_data=f"refresh_{server_id}"),
    ])
    keyboard.append([InlineKeyboardButton("⬅️ Back to List", callback_data="list_servers")])
    await query.edit_message_text(final, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def show_reset_log(query, context, server_id):
    """📜 Details: the full log of the server's last reset."""
    entry = last_log(server_id)
    back = [[InlineKeyboardButton("🖥 Back to Server", callback_data=f"server_{server_id}")]]
    if not entry:
        text = ("📜 No reset log kept for this server: logs live in memory and are "
                "lost when the bot restarts.")
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(back))
        return
    logs = entry['logs']
    more = f"_(first {len(logs) - 60} lines left out)_\n" if len(logs) > 60 else ""
    await query.edit_message_text(
        f"📜 *Reset log* — {fmt_time(entry['at'])} (Iran time)\n\n{more}{log_text(logs[-60:])}",
        reply_markup=InlineKeyboardMarkup(back), parse_mode="Markdown")


async def reset_password(query, context, server_id):
    server = await _resolve_server(context, server_id)
    name = server.get("name", "Server") if server else "Server"
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, reset it", callback_data=f"resetpw_confirm_{server_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"server_{server_id}"),
        ]
    ]
    await query.edit_message_text(
        f"🔑 *Reset Root Password*\n\n"
        f"Server: `{name}`\n\n"
        f"⚠️ This will generate a new random root password.\n"
        f"The server must be running with qemu-guest-agent installed.\n\n"
        f"Are you sure?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def reset_password_confirm(query, context, server_id):
    await query.edit_message_text("🔑 Resetting root password...", parse_mode="Markdown")
    result = await _get_api(context).reset_password(server_id)
    keyboard = [[InlineKeyboardButton("⬅️ Back to Server", callback_data=f"server_{server_id}")]]
    if result and result.get("root_password"):
        pw = result["root_password"]
        keyboard.insert(0, [copy_button("📋 Copy password", pw)])
        await query.edit_message_text(
            f"✅ *Password Reset Successful*\n\n"
            f"🔑 New root password:\n`{pw}`\n\n"
            f"⚠️ Save this password now — it won't be shown again.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            "❌ *Password reset failed.*\n\n"
            "Make sure qemu-guest-agent is installed and the server is running.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )


async def backup_toggle_confirm(query, context, server_id):
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    enabled = bool(server.get("backup_window"))
    name = server.get("name", "Server")
    if enabled:
        text = (
            f"💾 *Disable Backups*\n\n"
            f"Server: `{name}`\n\n"
            f"⚠️ Automatic backups will stop and *existing backups will be deleted*.\n\n"
            f"Are you sure?"
        )
        yes = InlineKeyboardButton("✅ Yes, disable", callback_data=f"backupgo_{server_id}_off")
    else:
        text = (
            f"💾 *Enable Backups*\n\n"
            f"Server: `{name}`\n\n"
            f"Hetzner keeps 7 daily backups. Cost: *+20% of the server plan price* per month.\n\n"
            f"Enable backups?"
        )
        yes = InlineKeyboardButton("✅ Yes, enable", callback_data=f"backupgo_{server_id}_on")
    keyboard = [[yes, InlineKeyboardButton("❌ Cancel", callback_data=f"server_{server_id}")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def backup_toggle_go(query, context, server_id, mode):
    api = _get_api(context)
    await query.edit_message_text("💾 Updating backup settings...")
    result = await (api.enable_backup(server_id) if mode == "on" else api.disable_backup(server_id))
    if result:
        await asyncio.sleep(2)
        await show_server_detail(query, context, server_id, refresh=True)
    else:
        await query.edit_message_text(
            "❌ Failed to change backup settings. Check the logs and try again.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🖥 Back to Server", callback_data=f"server_{server_id}")]]
            ),
        )


WAIT_RENAME = 30
_RENAME_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9.\-]{0,61}[a-zA-Z0-9])?$")


async def _start_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != Config.ADMIN_ID:
        await query.answer("⛔ Unauthorized", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    server_id = int(query.data.split("_")[1])
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return ConversationHandler.END
    context.user_data['rename_id'] = server_id
    await query.edit_message_text(
        f"✏️ *Rename Server*\n\n"
        f"Current name: `{server.get('name', '?')}`\n\n"
        f"Send the new name.\n"
        f"Letters, numbers, hyphens and dots — 1–63 chars, starting and "
        f"ending with a letter or number.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=f"rename_cancel_{server_id}")]]
        ),
        parse_mode="Markdown",
    )
    return WAIT_RENAME


async def recv_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != Config.ADMIN_ID:
        return ConversationHandler.END
    server_id = context.user_data.get('rename_id')
    if not server_id:
        return ConversationHandler.END
    new_name = update.message.text.strip()
    if not _RENAME_RE.match(new_name):
        await update.message.reply_text(
            "⚠️ Invalid name. Use 1–63 chars: letters, numbers, hyphens or dots, "
            "starting and ending with a letter or number. Send another, or tap Cancel above."
        )
        return WAIT_RENAME
    result = await _get_api(context).rename_server(server_id, new_name)
    back = [[InlineKeyboardButton("🖥 Back to Server", callback_data=f"server_{server_id}")]]
    if result and not (isinstance(result, dict) and result.get('error')):
        await update.message.reply_text(
            f"✅ *Server Renamed*\n\nNew name: `{new_name}`",
            reply_markup=InlineKeyboardMarkup(back),
            parse_mode="Markdown",
        )
    else:
        msg = (result or {}).get('error', {}).get('message', 'Unknown error') \
            if isinstance(result, dict) else 'Unknown error'
        await update.message.reply_text(
            f"❌ *Rename failed*\n\n{escape_markdown(msg)}",
            reply_markup=InlineKeyboardMarkup(back),
            parse_mode="Markdown",
        )
    context.user_data.pop('rename_id', None)
    return ConversationHandler.END


async def cancel_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_id = context.user_data.pop('rename_id', None)
    if server_id is None:
        try:
            server_id = int(query.data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await query.edit_message_text("Cancelled.")
            return ConversationHandler.END
    await show_server_detail(query, context, server_id, refresh=True)
    return ConversationHandler.END


WAIT_PRICE = 31


async def _start_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != Config.ADMIN_ID:
        await query.answer("⛔ Unauthorized", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    server_id = int(query.data.split("_")[1])
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return ConversationHandler.END

    account = _current_account(context)
    list_price = _server_type_price(server.get("server_type", {}), server_location(server))
    override = get_override(account, server_id)
    context.user_data['price_id'] = server_id

    text = (
        f"💶 *Edit Price*\n\n"
        f"Server: `{server.get('name')}`\n"
        f"Hetzner list price: `€{list_price:.2f}/month`\n"
    )
    if override is not None:
        text += f"Your price: `€{override:.2f}/month`\n"
    text += (
        f"\nSend what this server actually costs you per month — useful when "
        f"you are grandfathered into an older, cheaper rate that the API does "
        f"not report.\n\n"
        f"Example: `4.51`"
    )
    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data=f"price_cancel_{server_id}")]]
    if override is not None:
        keyboard.insert(0, [InlineKeyboardButton(
            "↩️ Use Hetzner's price", callback_data=f"price_clear_{server_id}")])
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown",
    )
    return WAIT_PRICE


async def recv_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != Config.ADMIN_ID:
        return ConversationHandler.END
    server_id = context.user_data.get('price_id')
    if not server_id:
        return ConversationHandler.END

    raw = update.message.text.strip().replace(',', '.').lstrip('€').strip()
    try:
        amount = float(raw)
    except ValueError:
        await update.message.reply_text(
            "⚠️ That is not a number. Send the monthly price like `4.51`, "
            "or tap Cancel above.", parse_mode="Markdown")
        return WAIT_PRICE
    if amount < 0:
        await update.message.reply_text("⚠️ The price cannot be negative.")
        return WAIT_PRICE

    set_override(_current_account(context), server_id, amount)
    context.user_data.pop('price_id', None)
    await update.message.reply_text(
        f"✅ *Price set*\n\nThis server now counts as `€{amount:.2f}/month` in "
        f"the cost report.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🖥 Back to Server", callback_data=f"server_{server_id}")]]
        ),
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def clear_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_id = context.user_data.pop('price_id', None)
    if server_id is None:
        try:
            server_id = int(query.data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await query.edit_message_text("Cancelled.")
            return ConversationHandler.END
    clear_override(_current_account(context), server_id)
    await show_server_detail(query, context, server_id, refresh=True)
    return ConversationHandler.END


async def cancel_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    server_id = context.user_data.pop('price_id', None)
    if server_id is None:
        try:
            server_id = int(query.data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            await query.edit_message_text("Cancelled.")
            return ConversationHandler.END
    await show_server_detail(query, context, server_id, refresh=True)
    return ConversationHandler.END


async def delete_server_confirm(query, context, server_id):
    server = await _resolve_server(context, server_id)
    if not server:
        await send_not_found(query)
        return
    name = server.get("name", "Server")
    if is_protected(server):
        await send_protected(query, server_id, name, "delete")
        return
    stype = server.get("server_type", {}).get("name", "?")
    ip = server.get("public_net", {}).get("ipv4", {}).get("ip", "N/A")
    keyboard = [
        [InlineKeyboardButton("🗑 Yes, delete permanently", callback_data=f"deletego_{server_id}")],
    ]
    keep_note = ""
    if ip_change.current_ipv4(server):
        keyboard.append([InlineKeyboardButton(f"🗑 Delete, but keep the IP {ip}",
                                              callback_data=f"deletekeep_{server_id}")])
        keep_note = (f"\n\n📍 *Keep the IP*: `{ip}` stays in the account as a free primary IP "
                     f"(billed monthly) and can go on a new server in the same location.")
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data=f"server_{server_id}")])
    await query.edit_message_text(
        f"🗑 *Delete Server*\n\n"
        f"Server: `{name}`\n"
        f"Type: `{stype}`\n"
        f"IP: `{ip}`\n\n"
        f"⚠️ This *permanently* deletes the server and all its data. "
        f"This cannot be undone.{keep_note}\n\n"
        f"Are you absolutely sure?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


def forget_server(server_id):
    """Drop what the bot keeps about a deleted server (schedules, alerts, auto reset)."""
    remove_schedules_for(server_id)
    for w in list_watches():
        if w.get("server_id") == server_id:
            remove_watches_for(server_id, w.get("plan"))
    auto_reset.set_included(server_id, True)
    auto_reset.set_server_time(server_id, None)


async def delete_server_go(query, context, server_id, keep_ip=False):
    server = await _resolve_server(context, server_id)
    name = server.get("name", "Server") if server else str(server_id)
    api = _get_api(context)
    if is_protected(server):
        await send_protected(query, server_id, name, "delete")
        return
    kept = None
    if keep_ip and server:
        kept, error = await ip_change.keep_ipv4(api, server)
        if error:
            await query.edit_message_text(
                f"❌ *Not deleted*: the IP could not be kept ({escape_markdown(error)}).",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🖥 Back to Server", callback_data=f"server_{server_id}")]]),
                parse_mode="Markdown")
            return
    await query.edit_message_text(f"🗑 Deleting `{name}`...", parse_mode="Markdown")
    result = await api.delete_server(server_id)
    if result and not (isinstance(result, dict) and result.get("error")):
        context.user_data.get('server_account_map', {}).pop(server_id, None)
        forget_server(server_id)
        text = f"✅ *Server Deleted*\n\n`{name}` has been permanently deleted."
        if kept:
            text += (f"\n\n📍 `{kept}` was kept as a free primary IP (billed monthly). "
                     f"➕ Create Server offers it on the confirm screen for a new server in "
                     f"`{server_location(server)}`; delete it in 📍 Primary IPs when no longer needed.")
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📊 Back to List", callback_data="list_servers")]]
            ),
            parse_mode="Markdown",
        )
    else:
        msg = (result or {}).get("error", {}).get("message", "Unknown error") \
            if isinstance(result, dict) else "Unknown error"
        await query.edit_message_text(
            f"❌ *Delete failed*\n\n{escape_markdown(msg)}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🖥 Back to Server", callback_data=f"server_{server_id}")]]
            ),
            parse_mode="Markdown",
        )


async def _stale_back_to_server(query, context, server_id):
    """A Cancel button of a prompt that already ended: just go back."""
    context.user_data.pop('rename_id', None)
    context.user_data.pop('price_id', None)
    await show_server_detail(query, context, server_id, refresh=True)


async def _stale_clear_price(query, context, server_id):
    """'Use Hetzner's price' pressed after its prompt ended: still honour it."""
    context.user_data.pop('price_id', None)
    await _resolve_server(context, server_id)
    clear_override(_current_account(context), server_id)
    await show_server_detail(query, context, server_id, refresh=True)


async def _stale_console(query, context, _action):
    """A console button from a session that already ended or timed out."""
    _close_session(query.from_user.id)
    server_id = (context.user_data.get("console") or {}).get("server_id")
    if server_id:
        await show_server_detail(query, context, server_id, refresh=True)
    else:
        await query.edit_message_text(
            "🔌 This console session has ended.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📊 Servers", callback_data="list_servers")]]
            ),
        )


router.add(r"list_servers", _show_all_servers)
router.add(r"page_(\d+)", show_server_list, int)
router.add(r"server_(\d+)", show_server_detail, int)
router.add(r"srvmng_(\d+)", show_manage, int)
router.add(r"srvnet_(\d+)", show_network, int)
router.add(r"lsort_(traffic|name)", set_list_sort)
router.add(r"lnear", toggle_near_limit)
router.add(r"refresh_(\d+)", lambda q, c, sid: show_server_detail(q, c, sid, refresh=True), int)
router.add(r"poweron_(\d+)", lambda q, c, sid: power_action(q, c, sid, "on"), int)
router.add(r"poweroff_(\d+)", lambda q, c, sid: power_action(q, c, sid, "off"), int)
router.add(r"reset_(\d+)", reset_traffic_confirm, int)
router.add(r"resetgo_(\d+)", reset_traffic, int)
router.add(r"resetlog_(\d+)", show_reset_log, int)
router.add(r"resetpw_confirm_(\d+)", reset_password_confirm, int)
router.add(r"resetpw_(\d+)", reset_password, int)
router.add(r"backup_(\d+)", backup_toggle_confirm, int)
router.add(r"backupgo_(\d+)_(on|off)", backup_toggle_go, int)
router.add(r"delete_(\d+)", delete_server_confirm, int)
router.add(r"deletego_(\d+)", delete_server_go, int)
router.add(r"deletekeep_(\d+)", lambda q, c, sid: delete_server_go(q, c, sid, keep_ip=True), int)
router.add(r"prot_(\d+)_([01])", set_protection, int, int)
# Conversation buttons land here only once their conversation has ended
# (main.py's ConversationHandlers take them first while it is open).
router.add(r"rename_cancel_(\d+)", _stale_back_to_server, int)
router.add(r"price_cancel_(\d+)", _stale_back_to_server, int)
router.add(r"price_clear_(\d+)", _stale_clear_price, int)
router.add(r"console_(back_panel|back_port|back_user|cancel|disconnect)", _stale_console)
router.add(r"auth_(password|key)", _stale_console)
