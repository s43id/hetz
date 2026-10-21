"""Monthly cost report across servers, snapshots, IPs, volumes and backups."""
import asyncio
import calendar
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from utils import format_traffic, included_traffic_tb, overage_cost, server_location
from price_overrides import effective_price

from .router import router
from .context import _get_api, _get_tracker, _current_account
from .ui import account_header, account_switch_rows
from .pricing import (
    _gross, _image_price_per_gb, _floating_ip_price, _primary_ip_price,
    _server_type_price,
)


async def collect_costs(api, account, tracker):
    """This month's costs of one account, as data (the report below and the Mini App).

    None when the account lists no servers. Also refreshes each server's
    live overage in the tracker.
    """
    servers, pricing, snapshots, floating_ips, primary_ips, volumes = await asyncio.gather(
        api.list_servers(),
        api.get_pricing(),
        api.list_images(),
        api.list_floating_ips(),
        api.list_primary_ips(),
        api.list_volumes(),
    )
    if not servers:
        return None

    backup_pct = 20.0
    try:
        backup_pct = float(pricing.get("server_backup", {}).get("percentage", 20) or 20)
    except (TypeError, ValueError):
        pass

    rows = []
    total_server_cost = backup_cost = 0
    backup_count = 0
    for s in servers:
        loc = server_location(s)
        sp = effective_price(account, s["id"],
                             _server_type_price(s.get("server_type", {}), loc))
        total_server_cost += sp
        tracker.update_live_overage(s["id"], overage_cost(s))
        backups = bool(s.get("backup_window"))
        if backups:
            backup_count += 1
            backup_cost += sp * backup_pct / 100
        rows.append({
            "id": s["id"], "name": s.get("name", "Unnamed"),
            "plan": s.get("server_type", {}).get("name", "?"), "price": sp,
            "traffic": s.get("outgoing_traffic", 0), "limit_tb": included_traffic_tb(s),
            "backups": backups, "overage": tracker.get_server_month_overage(s["id"]),
        })

    monthly_overage = tracker.get_current_month_overage()
    snap_size = sum(i.get("image_size") or 0 for i in snapshots)
    snapshot_cost = snap_size * _image_price_per_gb(pricing)
    floating_cost = sum(_floating_ip_price(pricing, f) for f in floating_ips)
    assigned_pips = [p for p in primary_ips if p.get("assignee_id")]
    unassigned_pips = [p for p in primary_ips if not p.get("assignee_id")]
    extra_primary_cost = sum(_primary_ip_price(pricing, p) for p in unassigned_pips)
    vol_per_gb = _gross(pricing.get("volume", {}).get("price_per_gb_month", {}) or pricing.get("volume", {}))
    vol_size = sum(v.get("size") or 0 for v in volumes)
    volume_cost = vol_size * vol_per_gb

    now = datetime.now()
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    return {
        "servers": rows,
        "server_cost": total_server_cost,
        "backup_count": backup_count, "backup_cost": backup_cost,
        "snapshot_count": len(snapshots), "snapshot_gb": snap_size, "snapshot_cost": snapshot_cost,
        "volume_count": len(volumes), "volume_gb": vol_size, "volume_cost": volume_cost,
        "floating_count": len(floating_ips), "floating_cost": floating_cost,
        "primary_assigned": len(assigned_pips), "primary_unassigned": len(unassigned_pips),
        "primary_cost": extra_primary_cost,
        "overage": monthly_overage,
        "projected_overage": monthly_overage / now.day * days_in_month,
        "total": (total_server_cost + monthly_overage + backup_cost + snapshot_cost
                  + floating_cost + extra_primary_cost + volume_cost),
        "overage_all_time": tracker.get_total_overage(),
        "history": tracker.get_monthly_breakdown(),
    }


async def show_overage_cost(query, context):
    api = _get_api(context)
    account = _current_account(context)
    c = await collect_costs(api, account, _get_tracker(context))
    if not c:
        await query.edit_message_text(
            f"{account_header(context)}⚠️ No servers found or API error occurred.",
            reply_markup=InlineKeyboardMarkup(
                account_switch_rows(context, "overage_cost")
                + [[InlineKeyboardButton("⬅️ Back", callback_data="start_menu")]]
            ),
            parse_mode="Markdown",
        )
        return

    server_details = []
    for r in c["servers"]:
        line = f"• `{r['name']}` ({r['plan']}): €{r['price']:.2f} | {format_traffic(r['traffic'], r['limit_tb'])}"
        if r["backups"]:
            line += " | 💾"
        if r["overage"] > 0:
            line += f" | ⚠️ €{r['overage']:.2f} overage"
        server_details.append(line)

    primary_line = f"📍 Primary IPs: {c['primary_assigned']} on servers (free)"
    if c["primary_unassigned"]:
        primary_line += f" | {c['primary_unassigned']} unassigned → €{c['primary_cost']:.2f}"

    text = (
        f"💸 *COST REPORT*\n\n"
        f"{account_header(context)}"
        f"📦 *Servers (This Month)*\n" + "\n".join(server_details) + "\n\n"
        f"🧩 *Other Resources*\n"
        f"📸 Snapshots ({c['snapshot_count']}): {c['snapshot_gb']:.1f} GB → €{c['snapshot_cost']:.2f}\n"
        f"💽 Volumes ({c['volume_count']}): {c['volume_gb']} GB → €{c['volume_cost']:.2f}\n"
        f"💾 Backups ({c['backup_count']} servers): €{c['backup_cost']:.2f}\n"
        f"🌐 Floating IPs ({c['floating_count']}): €{c['floating_cost']:.2f}\n"
        f"{primary_line}\n\n"
        f"📊 *Summary*\n"
        f"📦 Server costs: €{c['server_cost']:.2f}\n"
        f"📈 Overage: €{c['overage']:.2f}\n"
        f"📸 Snapshots: €{c['snapshot_cost']:.2f}\n"
        f"💽 Volumes: €{c['volume_cost']:.2f}\n"
        f"💾 Backups: €{c['backup_cost']:.2f}\n"
        f"🌐 Floating IPs: €{c['floating_cost']:.2f}\n"
    )
    if c["primary_unassigned"]:
        text += f"📍 Extra primary IPs: €{c['primary_cost']:.2f}\n"
    text += f"💰 Total: €{c['total']:.2f}\n\n"
    if c["overage"] > 0:
        text += (
            f"🔮 *Projected Month-End Overage*\n"
            f"~€{c['projected_overage']:.2f} at the current usage rate\n\n"
        )
    text += f"🔴 *Total Overage Loss (All Time)*\n€{c['overage_all_time']:.2f}\n"
    if c["history"] and len(c["history"]) > 1:
        text += "\n*Monthly History:*\n"
        for month, cost in c["history"][:6]:
            text += f"• {month}: €{cost:.2f}\n"
    text += f"\n🕓 Updated: `{datetime.now().strftime('%H:%M:%S')}`"

    keyboard = account_switch_rows(context, "overage_cost") + [
        [InlineKeyboardButton("🔄 Refresh", callback_data="overage_cost")],
        [InlineKeyboardButton("📊 Server Management", callback_data="list_servers")],
        [InlineKeyboardButton("⬅️ Back", callback_data="start_menu")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


router.add(r"overage_cost", show_overage_cost)
