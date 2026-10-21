"""Guided server-creation flow: category → type → location → image → IPv6 →
count → name → confirm → create. Includes the custom-name conversation.

🧬 From a snapshot (``snapclone_<id>``, snapshot detail): the wizard keeps
``srv_snap`` = {id, label, arch, disk, from}, lists only plans of the
snapshot's architecture with a big enough disk and skips the image step. The
image step also offers the account's snapshots. On the confirm screen a
kept primary IPv4 in the location (📍, from a deleted server or an IP change)
can be used instead of a new one (``srv_pip``).
"""
import asyncio
import re
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from config import Config
from utils import (
    get_location_info, escape_markdown, is_capacity_refusal, api_error_text,
    type_available_at, type_offered_at, type_has_location_data, copy_button,
)
from create_watch import add_create_watch
import ip_change
import node_setup
from plan_watch import CHECK_INTERVAL_MINUTES

from .router import router
from .context import _get_api, _current_account

WAIT_SERVER_NAME = 20
MAX_SNAPSHOTS = 8      # snapshots offered on the image step
MAX_KEPT_IPS = 3       # kept IPv4s offered on the confirm screen


_CREATE_CATEGORIES = [
    ('cost',    '💰 Cost-Optimized',      ['cx', 'cax'], 'Shared vCPU — Intel or ARM64'),
    ('regular', '⚡ Regular Performance',  ['cpx'], 'Shared vCPU — AMD'),
    ('general', '🔧 General Purpose',      ['ccx'], 'Dedicated vCPU — AMD'),
]

# Cost-optimized spans two CPU architectures, so it gets an extra pick step.
_COST_VENDORS = [
    ('intel', '🔵 Intel (CX)',   ['cx']),
    ('arm',   '💪 ARM64 (CAX)',  ['cax']),
]


def _alert_mode(context):
    """True while the wizard is setting up an auto-create alert, not a server."""
    return bool(context.user_data.get('srv_alert'))


def _clear_alert(context):
    for key in ('srv_alert', 'srv_alert_locs'):
        context.user_data.pop(key, None)


def _plan_price_label(stype, locations):
    """Monthly price for the plan list, before a location is chosen.

    Prices differ per location, so show the cheapest of the locations given
    (where the plan is in stock, else wherever it is sold) and mark it "from"
    when other locations cost more.
    """
    prices = {}
    for p in stype.get('prices', []):
        try:
            prices[p.get('location')] = float(p.get('price_monthly', {}).get('gross', 0))
        except (TypeError, ValueError):
            continue
    wanted = [prices[l] for l in locations if l in prices] or list(prices.values())
    if not wanted:
        return "€?"
    low, high = min(wanted), max(wanted)
    return f"€{low:.2f}" if high - low < 0.005 else f"from €{low:.2f}"


def _snap(context):
    """The snapshot this wizard clones (🧬), or None."""
    return context.user_data.get('srv_snap')


def _category_cb(context):
    return "srvcs" if _snap(context) else "srvc"


def _image_label(context):
    image = context.user_data.get('srv_img', '')
    return context.user_data.get('srv_img_label') or image


def _clone_base(snap):
    base = re.sub(r"[^a-z0-9-]+", "-", (snap.get('from') or "server").lower()).strip("-")
    return (base or "server")[:40].rstrip("-")


def _where(context):
    """The location(s) to show in step headers."""
    if _alert_mode(context):
        return " / ".join(context.user_data.get('srv_alert_locs') or [])
    return context.user_data.get('srv_loc', '')


async def srv_pick_category(query, context):
    _clear_alert(context)
    # A new wizard: forget which names an earlier run already created.
    for key in ('srv_done', 'srv_snap', 'srv_pip', 'srv_img_label'):
        context.user_data.pop(key, None)
    await _category_screen(query, context)


async def _category_screen(query, context):
    snap = _snap(context)
    keyboard = []
    for cat_id, label, prefixes, desc in _CREATE_CATEGORIES:
        if snap and snap.get('arch') == 'arm' and cat_id != 'cost':
            continue   # ARM plans are all CAX
        keyboard.append([InlineKeyboardButton(f"{label}\n{desc}", callback_data=f"srvcat_{cat_id}")])
    if snap:
        keyboard.append([InlineKeyboardButton("⬅️ Back to Snapshot", callback_data=f"snap_{snap['id']}")])
        text = (f"🧬 *New server from a snapshot*\n\n📸 `{snap['label']}` "
                f"({snap.get('arch')}, {snap.get('disk') or '?'} GB disk)\n\n"
                f"Choose a category; only plans with room for the snapshot are listed:")
    else:
        keyboard.append([InlineKeyboardButton("⬅️ Back to List", callback_data="list_servers")])
        text = "➕ *Create Server*\n\nChoose a server category:"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def srv_from_snapshot(query, context, image_id):
    """🧬 From the snapshot detail: start the wizard with this snapshot as the image."""
    img = await _get_api(context).get_image(image_id)
    if not img or img.get('status') != 'available':
        await query.edit_message_text(
            "⚠️ This snapshot is not available (still being made, or deleted).",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back to Snapshots", callback_data="snapshots")]]))
        return
    _clear_alert(context)
    for key in ('srv_done', 'srv_pip', 'srv_img_label'):
        context.user_data.pop(key, None)
    context.user_data['srv_snap'] = {
        'id': image_id,
        'label': img.get('description') or img.get('name') or str(image_id),
        'arch': img.get('architecture') or 'x86',
        'disk': img.get('disk_size') or 0,
        'from': (img.get('created_from') or {}).get('name'),
    }
    await _category_screen(query, context)


async def _availability_by_location(api):
    """Per location, the server-type ids in stock and the ids offered at all.

    Reads each server type's ``locations`` array, which is where Hetzner moved
    this after removing /datacenters: an entry means the plan is sold there,
    and its ``available`` flag means it can be ordered right now.

    Returns ``(available, supported)``, each mapping location name -> id set.
    """
    available, supported = {}, {}
    for t in await api.get_server_types():
        type_id = t.get('id')
        for entry in t.get('locations') or []:
            name = entry.get('name')
            if not name:
                continue
            supported.setdefault(name, set()).add(type_id)
            if entry.get('available'):
                available.setdefault(name, set()).add(type_id)
    return available, supported


async def _show_plans(query, context, label, prefixes, back_cb):
    api = _get_api(context)
    types, (avail, supported) = await asyncio.gather(
        api.get_server_types(),
        _availability_by_location(api),
    )
    snap = _snap(context)
    candidates = [
        t for t in types
        if any(t['name'].lower().startswith(p) for p in prefixes)
        and not t.get('deprecation')
        and (not snap or (t.get('architecture') == snap.get('arch')
                          and (t.get('disk') or 0) >= (snap.get('disk') or 0)))
    ]
    candidates.sort(key=lambda t: (t.get('cores', 0), t.get('memory', 0)))

    # Stock is reported per plan; a plan without a locations array is shown
    # unmarked rather than wrongly called sold out.
    have_stock_data = any(type_has_location_data(t) for t in candidates)

    keyboard = []
    sold_out = 0
    for t in candidates:
        in_stock_at = [loc for loc, ids in avail.items() if t.get('id') in ids]
        sold_at = [loc for loc, ids in supported.items() if t.get('id') in ids]
        price = _plan_price_label(t, in_stock_at or sold_at)
        n_locs = len(in_stock_at)
        if not type_has_location_data(t):
            mark = ""
        elif n_locs:
            mark = f"✅{n_locs} "
        else:
            mark = "⛔ "
            sold_out += 1
        keyboard.append([InlineKeyboardButton(
            f"{mark}{t['name']} | {t.get('cores')}C / {t.get('memory'):.0f}GB / {t.get('disk')}GB | {price}/mo",
            callback_data=f"srvt_{t['name']}",
        )])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=back_cb)])

    text = f"➕ *Create Server* — {label}\n\nChoose a plan:"
    if snap:
        text = (f"🧬 *From snapshot* `{snap['label']}` — {label}\n\n"
                + ("Choose a plan:" if candidates else
                   f"No plan here has room for the snapshot ({snap.get('disk')} GB disk, "
                   f"{snap.get('arch')}). Go back and pick another category."))
    if have_stock_data:
        text += "\n\n✅ = number of locations in stock"
        if sold_out:
            text += (f"\n⛔ = sold out everywhere right now ({sold_out}) — tap one "
                     f"to have it created automatically when it is back")
    else:
        # Better to say the signal is missing than to leave the plans bare and
        # look like the stock check silently stopped working.
        text += ("\n\nℹ️ Hetzner is not reporting stock right now, so plans are "
                 "shown unmarked. Creation may still be refused for capacity.")
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def srv_pick_type(query, context, cat_id: str):
    cat = next((c for c in _CREATE_CATEGORIES if c[0] == cat_id), None)
    if not cat:
        # A wizard started before a category was removed can still send its id.
        await srv_pick_category(query, context)
        return
    _, label, prefixes, _ = cat
    context.user_data['srv_cat'] = cat_id

    # This category covers both Intel and AMD — pick the vendor first; a
    # snapshot already decides it.
    snap = _snap(context)
    if cat_id == 'cost' and snap:
        await srv_pick_vendor(query, context, 'arm' if snap.get('arch') == 'arm' else 'intel')
        return
    if cat_id == 'cost':
        keyboard = [
            [InlineKeyboardButton(vlabel, callback_data=f"srvven_{vid}")]
            for vid, vlabel, _ in _COST_VENDORS
        ]
        keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=_category_cb(context))])
        await query.edit_message_text(
            f"➕ *Create Server* — {label}\n\nChoose a CPU vendor:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return

    await _show_plans(query, context, label, prefixes, back_cb=_category_cb(context))


async def srv_pick_vendor(query, context, vendor_id: str):
    vendor = next((v for v in _COST_VENDORS if v[0] == vendor_id), None)
    if not vendor:
        await query.edit_message_text("⚠️ Unknown CPU vendor.")
        return
    _, label, prefixes = vendor
    context.user_data['srv_cat'] = 'cost'
    context.user_data['srv_vendor'] = vendor_id
    await _show_plans(query, context, label, prefixes,
                      back_cb="srvcs" if _snap(context) else "srvcat_cost")


async def srv_pick_location(query, context, server_type: str):
    context.user_data['srv_type'] = server_type
    _clear_alert(context)
    api = _get_api(context)
    locations, types, (avail, supported) = await asyncio.gather(
        api.list_locations(),
        api.get_server_types(),
        _availability_by_location(api),
    )
    stype = next((t for t in types if t.get('name') == server_type), {})
    type_id = stype.get('id')
    cat_id = context.user_data.get('srv_cat', '')
    back = [InlineKeyboardButton("⬅️ Back", callback_data=f"srvcat_{cat_id}")]

    def _label(loc):
        name = loc.get('name', '')
        return f"{get_location_info(name)[1]} {loc.get('city', name)}"

    # Only offer locations that actually have this plan in stock; without
    # availability data, fall back to offering everything. A location that
    # supports the plan but has none left is sold out; one that never offers
    # it (ARM64 outside the EU, say) is a different thing worth saying.
    # Stock is reported per plan (server_types[].locations), so whether we can
    # trust it is a question about this plan: a plan sold out everywhere still
    # has data, and must get the alert offer rather than look unknown.
    have_stock_data = type_has_location_data(stype)
    in_stock, sold_out, not_offered = [], [], []
    for loc in locations:
        name = loc.get('name', '')
        if not have_stock_data or type_id is None or type_id in avail.get(name, set()):
            in_stock.append(loc)
        elif type_id in supported.get(name, set()):
            sold_out.append(loc)
        else:
            not_offered.append(loc)

    alert_button = [InlineKeyboardButton(
        "🔔 Create automatically when back in stock", callback_data="srvalert")]

    if not in_stock:
        if sold_out:
            reason = (
                f"⛔ `{server_type}` is *sold out* right now in every location "
                f"that offers it: {', '.join(_label(l) for l in sold_out)}.\n\n"
                f"Tap 🔔 and the bot will create it for you as soon as it is "
                f"back, or go back and pick another plan."
            )
        else:
            reason = (
                f"🚫 `{server_type}` is not offered in any location available "
                f"to this account.\n\nGo back and pick another plan."
            )
        rows = [alert_button, back] if sold_out else [back]
        await query.edit_message_text(
            f"➕ *Create Server* — `{server_type}`\n\n{reason}",
            reply_markup=InlineKeyboardMarkup(rows),
            parse_mode="Markdown",
        )
        return

    keyboard = []
    row = []
    for loc in in_stock:
        loc_name = loc.get('name', '')
        _, flag = get_location_info(loc_name)
        city = loc.get('city', loc_name)
        row.append(InlineKeyboardButton(f"{flag} {city}", callback_data=f"srvl_{loc_name}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    if sold_out:
        keyboard.append(alert_button)
    keyboard.append(back)

    text = f"➕ *Create Server* — `{server_type}`\n\nChoose a location:"
    if sold_out:
        text += f"\n\n⛔ Sold out right now: {', '.join(_label(l) for l in sold_out)}"
    if not_offered:
        text += f"\n🚫 Not offered here: {', '.join(_label(l) for l in not_offered)}"
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def srv_alert_locations(query, context):
    """Pick every location an auto-create alert may use, in order of preference."""
    server_type = context.user_data.get('srv_type', '')
    api = _get_api(context)
    locations, types = await asyncio.gather(api.list_locations(), api.get_server_types())
    stype = next((t for t in types if t.get('name') == server_type), {})
    offered = [l for l in locations if type_offered_at(stype, l.get('name'))] or locations
    chosen = context.user_data.setdefault('srv_alert_locs', [])

    keyboard = []
    for loc in offered:
        name = loc.get('name', '')
        _, flag = get_location_info(name)
        mark = f"{chosen.index(name) + 1}️⃣" if name in chosen else "⬜"
        stock = ""
        if type_has_location_data(stype) and type_available_at(stype, name):
            stock = " · in stock now"
        keyboard.append([InlineKeyboardButton(
            f"{mark} {flag} {loc.get('city', name)}{stock}",
            callback_data=f"srvaloc_{name}",
        )])
    if chosen:
        keyboard.append([InlineKeyboardButton(
            f"✅ Continue ({len(chosen)} selected)", callback_data="srvaok")])
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"srvt_{server_type}")])

    await query.edit_message_text(
        f"🔔 *Create when in stock* — `{server_type}`\n\n"
        f"Tap every location you would accept. The bot creates the server in "
        f"the first one with room, trying them in the order you tap them."
        + (f"\n\nSelected: {' → '.join(chosen)}" if chosen else ""),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def srv_alert_toggle(query, context, location: str):
    chosen = context.user_data.setdefault('srv_alert_locs', [])
    if location in chosen:
        chosen.remove(location)
    else:
        chosen.append(location)
    await srv_alert_locations(query, context)


async def srv_alert_ok(query, context):
    chosen = context.user_data.get('srv_alert_locs') or []
    if not chosen:
        await srv_alert_locations(query, context)
        return
    context.user_data['srv_alert'] = True
    await srv_pick_image(query, context, chosen[0])


async def srv_pick_image(query, context, location: str):
    context.user_data['srv_loc'] = location
    snap = _snap(context)
    if snap:
        # 🧬 The snapshot is the image.
        await srv_pick_ipv6(query, context, str(snap['id']))
        return
    api = _get_api(context)
    server_type_name = context.user_data.get('srv_type', '')

    # Determine architecture
    types = await api.get_server_types()
    stype = next((t for t in types if t['name'] == server_type_name), {})
    arch = stype.get('architecture', 'x86')

    images, snapshots = await asyncio.gather(api.list_images('system'), api.list_images())
    usable = [
        i for i in images
        if i.get('architecture') == arch and not i.get('deprecated')
    ]
    usable.sort(key=lambda i: i.get('name', ''))
    own = [i for i in snapshots
           if i.get('status') == 'available' and i.get('architecture', arch) == arch
           and (i.get('disk_size') or 0) <= (stype.get('disk') or 0)]
    own.sort(key=lambda i: i.get('created') or '', reverse=True)

    keyboard = []
    row = []
    for img in usable:
        row.append(InlineKeyboardButton(
            img.get('description') or img.get('name'),
            callback_data=f"srvi_{img.get('name')}",
        ))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    for img in own[:MAX_SNAPSHOTS]:
        label = img.get('description') or img.get('name') or str(img['id'])
        keyboard.append([InlineKeyboardButton(f"📸 {label[:40]}", callback_data=f"srvi_{img['id']}")])
    back_cb = "srvalert" if _alert_mode(context) else f"srvt_{server_type_name}"
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=back_cb)])
    await query.edit_message_text(
        f"➕ *Create Server* — `{server_type_name}` @ `{_where(context)}`\n\nChoose an OS"
        + (" or one of your snapshots (📸):" if own else ":"),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def srv_pick_ipv6(query, context, image: str):
    context.user_data['srv_img'] = image
    snap = _snap(context)
    if image.isdigit():
        if snap and str(snap['id']) == image:
            label = snap['label']
        else:
            img = await _get_api(context).get_image(int(image)) or {}
            label = img.get('description') or img.get('name') or image
        context.user_data['srv_img_label'] = f"📸 {label}"
    else:
        context.user_data.pop('srv_img_label', None)
    server_type_name = context.user_data.get('srv_type', '')
    location = context.user_data.get('srv_loc', '')
    back_cb = f"srvt_{server_type_name}" if snap else f"srvl_{location}"
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, enable IPv6", callback_data="srvipv6_1"),
            InlineKeyboardButton("🚫 No IPv6", callback_data="srvipv6_0"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data=back_cb)],
    ]
    await query.edit_message_text(
        f"➕ *Create Server* — `{server_type_name}` @ `{_where(context)}`\n\n"
        f"OS: `{_image_label(context)}`\n\n"
        f"Do you want IPv6 enabled on this server?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def srv_pick_count(query, context, enable_ipv6: bool):
    context.user_data['srv_ipv6'] = enable_ipv6
    if _alert_mode(context):
        # An alert creates one server; there is no count to choose.
        await srv_pick_name(query, context, 1)
        return
    server_type_name = context.user_data.get('srv_type', '')
    location = context.user_data.get('srv_loc', '')
    image = context.user_data.get('srv_img', '')
    keyboard = [
        [
            InlineKeyboardButton("1", callback_data="srvcount_1"),
            InlineKeyboardButton("2", callback_data="srvcount_2"),
            InlineKeyboardButton("3", callback_data="srvcount_3"),
        ],
        [
            InlineKeyboardButton("5", callback_data="srvcount_5"),
            InlineKeyboardButton("10", callback_data="srvcount_10"),
        ],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"srvi_{image}")],
    ]
    await query.edit_message_text(
        f"➕ *Create Server* — `{server_type_name}` @ `{location}`\n\n"
        f"OS: `{_image_label(context)}`\n"
        f"IPv6: `{'Enabled' if enable_ipv6 else 'Disabled'}`\n\n"
        f"How many servers do you want to create?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def srv_pick_name(query, context, count: int):
    context.user_data['srv_count'] = count
    default_name = _default_server_name(context)
    names = _server_names(default_name, count)
    if _alert_mode(context):
        # The automatic name is chosen when the server is made, so it can carry
        # the location actually used and the time it was created.
        back_cb = f"srvi_{context.user_data.get('srv_img', '')}"
        await query.edit_message_text(
            f"➕ *Create Server* — Name\n\n"
            f"Auto names are picked when the server is created, e.g. "
            f"`{default_name}`.\n\nUse an automatic name or enter your own:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏷 Auto name", callback_data="srvname_auto")],
                [InlineKeyboardButton("✍️ Custom Name", callback_data="srvname_custom")],
                [InlineKeyboardButton("⬅️ Back", callback_data=back_cb)],
            ]),
            parse_mode="Markdown",
        )
        return
    keyboard = [
        [InlineKeyboardButton(f"🏷 Auto: {default_name}", callback_data="srvname_auto")],
        [InlineKeyboardButton("✍️ Custom Name", callback_data="srvname_custom")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"srvipv6_{1 if context.user_data.get('srv_ipv6', True) else 0}")],
    ]
    await query.edit_message_text(
        f"➕ *Create Server* — Name\n\n"
        f"Count: `{count}`\n"
        f"Auto name preview:\n{_format_name_preview(names)}\n\n"
        f"Choose automatic naming or enter your own base name:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def srv_confirm_auto_name(query, context):
    context.user_data['srv_name_auto'] = True
    context.user_data['srv_name_base'] = _default_server_name(context)
    await srv_confirm(query, context)


async def _start_server_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != Config.ADMIN_ID:
        await query.answer("⛔ Unauthorized", show_alert=True)
        return ConversationHandler.END
    await query.answer()
    count = context.user_data.get('srv_count', 1)
    await query.edit_message_text(
        f"✍️ *Custom Server Name*\n\n"
        f"Send the base name for the server"
        f"{'s' if count > 1 else ''}.\n\n"
        f"Use letters, numbers, and hyphens only. "
        f"If count is more than 1, the bot will create names like `name-1`, `name-2`, ...",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="srvname_cancel")]]
        ),
        parse_mode="Markdown",
    )
    return WAIT_SERVER_NAME


async def recv_server_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != Config.ADMIN_ID:
        return ConversationHandler.END

    count = context.user_data.get('srv_count', 1)
    base_name = update.message.text.strip().lower()
    error = _server_name_error(base_name, count)
    if error:
        await update.message.reply_text(
            f"⚠️ {error}\n\nSend another name, or tap Cancel in the previous message.",
            parse_mode="Markdown",
        )
        return WAIT_SERVER_NAME

    context.user_data['srv_name_auto'] = False
    context.user_data['srv_name_base'] = base_name
    await srv_confirm_message(update.message, context)
    return ConversationHandler.END


async def cancel_server_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await srv_pick_name(query, context, context.user_data.get('srv_count', 1))
    return ConversationHandler.END


_SERVER_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _server_name_error(base_name: str, count: int):
    if not base_name:
        return "Server name cannot be empty."
    if not _SERVER_NAME_RE.match(base_name):
        return "Use 1-63 characters: lowercase letters, numbers, and hyphens. It must start and end with a letter or number."
    if count > 1 and len(f"{base_name}-{count}") > 63:
        return f"Name is too long for {count} servers because suffixes like `-{count}` are added."
    return None


def _default_server_name(context):
    stamp = datetime.now().strftime('%m%d%H%M')
    snap = _snap(context)
    if snap:
        return f"{_clone_base(snap)}-clone-{stamp}"
    server_type_name = context.user_data.get('srv_type', 'server')
    location = context.user_data.get('srv_loc', 'loc')
    return f"{server_type_name}-{location}-{stamp}".lower()


def _server_names(base_name: str, count: int):
    if count <= 1:
        return [base_name]
    return [f"{base_name}-{i}" for i in range(1, count + 1)]


def _format_name_preview(names):
    shown = names if len(names) <= 5 else names[:3] + ["..."] + names[-1:]
    return "\n".join(f"• `{name}`" for name in shown)


async def _build_srv_confirm(context):
    api = _get_api(context)
    server_type_name = context.user_data.get('srv_type', '')
    location = context.user_data.get('srv_loc', '')
    image = context.user_data.get('srv_img', '')
    enable_ipv6 = context.user_data.get('srv_ipv6', True)
    count = context.user_data.get('srv_count', 1)
    base_name = context.user_data.get('srv_name_base') or _default_server_name(context)
    names = _server_names(base_name, count)
    context.user_data['srv_name_base'] = base_name
    account = _current_account(context)

    types, servers = await asyncio.gather(
        api.get_server_types(),
        api.list_servers(),
    )
    stype = next((t for t in types if t['name'] == server_type_name), {})
    prices = stype.get('prices', [])

    # Find price for chosen location
    price = 0.0
    for p in prices:
        if p.get('location') == location:
            price = float(p.get('price_monthly', {}).get('gross', 0))
            break
    if not price and prices:
        price = float(prices[0].get('price_monthly', {}).get('gross', 0))

    _, flag = get_location_info(location)

    if _alert_mode(context):
        return _build_alert_confirm(context, account, server_type_name, stype, image,
                                    enable_ipv6, price, base_name, servers)

    # 📍 A kept primary IPv4 in this location can go on the (single) new server.
    kept = (await ip_change.kept_ipv4s(api, location))[:MAX_KEPT_IPS] if count == 1 else []
    chosen = next((p for p in kept if p.get('id') == context.user_data.get('srv_pip')), None)
    if not chosen:
        context.user_data.pop('srv_pip', None)
    ipv4_line = (f"📍 IPv4: `{chosen.get('ip')}` (kept)\n" if chosen
                 else "📍 IPv4: new\n" if kept else "")

    text = (
        f"➕ *Confirm Server Creation*\n\n"
        f"🔑 Account: `{account}`\n"
        f"🔧 Type: `{server_type_name}` — {stype.get('cores')}C / {stype.get('memory'):.0f}GB / {stype.get('disk')}GB\n"
        f"📍 Location: `{flag} {location}`\n"
        f"💿 Image: `{_image_label(context)}`\n"
        f"🌐 IPv6: `{'Enabled' if enable_ipv6 else 'Disabled'}`\n"
        f"{ipv4_line}"
        f"🔢 Count: `{count}`\n"
        f"🏷 Names:\n{_format_name_preview(names)}\n"
        f"💰 Cost: `€{price:.2f}/month each`"
    )
    if count > 1:
        text += f" (`€{price * count:.2f}/month total`)"
    text += (
        f"\n\n"
        f"⚠️ Server will be created immediately."
    )
    existing_names = {s.get('name') for s in servers}
    duplicate_names = [name for name in names if name in existing_names]
    keyboard = [
        [InlineKeyboardButton("⬅️ Back", callback_data=f"srvcount_{count}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="list_servers")],
    ]
    if duplicate_names:
        text += (
            "\n\n❌ *Name conflict:* these server names already exist:\n"
            + _format_name_preview(duplicate_names)
            + "\n\nChoose another custom name or change the count."
        )
    else:
        keyboard.insert(0, [InlineKeyboardButton("✅ Create Server", callback_data="srvgo")])
        # A PasarGuard node: the server runs setup.sh --auto at first boot and
        # the bot adds it to the chosen panel once it answers (node_setup.py).
        if node_setup.can_create_nodes() and image.startswith("ubuntu"):
            for row, panel in enumerate(node_setup.panel_names(), start=1):
                keyboard.insert(row, [InlineKeyboardButton(
                    f"🧩 Create as node → {panel}", callback_data=f"srvnode_{panel}")])
        at = len(keyboard) - 2
        if chosen:
            keyboard.insert(at, [InlineKeyboardButton("🆕 New IPv4 instead",
                                                      callback_data="srvpip_0")])
        else:
            for p in reversed(kept):
                keyboard.insert(at, [InlineKeyboardButton(
                    f"📍 Use kept IP {p.get('ip')}", callback_data=f"srvpip_{p['id']}")])
    return text, keyboard


async def srv_pick_kept_ip(query, context, pip_id):
    """📍 Use a kept primary IPv4 for the new server (0 = a new one)."""
    if pip_id:
        context.user_data['srv_pip'] = pip_id
    else:
        context.user_data.pop('srv_pip', None)
    await srv_confirm(query, context)


def _build_alert_confirm(context, account, plan, stype, image, enable_ipv6,
                         price, base_name, servers):
    locations = context.user_data.get('srv_alert_locs') or []
    where = " → ".join(f"{get_location_info(l)[1]} {l}" for l in locations)
    auto = context.user_data.get('srv_name_auto', True)
    name_line = (f"auto, picked at creation (e.g. `{base_name}`)" if auto
                 else f"`{base_name}`")
    text = (
        f"🔔 *Confirm Auto-Create Alert*\n\n"
        f"🔑 Account: `{account}`\n"
        f"🔧 Type: `{plan}` — {stype.get('cores')}C / {stype.get('memory'):.0f}GB / {stype.get('disk')}GB\n"
        f"📍 Locations, in order: {where}\n"
        f"💿 Image: `{image}`\n"
        f"🌐 IPv6: `{'Enabled' if enable_ipv6 else 'Disabled'}`\n"
        f"🏷 Name: {name_line}\n"
        f"💰 Cost once created: `€{price:.2f}/month`\n\n"
        f"Every {CHECK_INTERVAL_MINUTES} minutes the bot checks whether `{plan}` "
        f"has room in one of these locations, and creates the server the moment "
        f"it does, then sends you its IP. Nothing is created or billed until then."
    )
    if not auto and base_name in {s.get('name') for s in servers}:
        text += f"\n\n⚠️ `{base_name}` already exists, so a `-2` suffix will be added."
    keyboard = [
        [InlineKeyboardButton("🔔 Set Alert", callback_data="srvalertgo")],
        [InlineKeyboardButton("⬅️ Back", callback_data="srvcount_1")],
        [InlineKeyboardButton("❌ Cancel", callback_data="list_servers")],
    ]
    return text, keyboard


async def srv_alert_go(query, context):
    data = context.user_data
    locations = data.get('srv_alert_locs') or []
    if not (_alert_mode(context) and locations and data.get('srv_type') and data.get('srv_img')):
        await query.edit_message_text(
            "⚠️ This alert setup has expired — please start again.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📊 Servers", callback_data="list_servers")]]
            ),
        )
        return

    plan = data['srv_type']
    name = None if data.get('srv_name_auto', True) else data.get('srv_name_base')
    add_create_watch(_current_account(context), plan, locations, data['srv_img'],
                     data.get('srv_ipv6', False), name, query.message.chat_id)
    _clear_alert(context)
    await query.edit_message_text(
        f"✅ *Alert set*\n\n"
        f"The bot will create a `{plan}` server in {' or '.join(f'`{l}`' for l in locations)} "
        f"as soon as there is room, and message you its IP.\n\n"
        f"See or cancel it under 🔔 Alerts & Schedules.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔔 Alerts & Schedules", callback_data="watches")],
            [InlineKeyboardButton("📊 Servers", callback_data="list_servers")],
        ]),
        parse_mode="Markdown",
    )


async def srv_confirm(query, context):
    text, keyboard = await _build_srv_confirm(context)
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def srv_confirm_message(message, context):
    text, keyboard = await _build_srv_confirm(context)
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def srv_create(query, context, panel=None):
    """Create the wizard's servers; with ``panel``, as PasarGuard nodes for it."""
    if panel is not None and not (node_setup.can_create_nodes()
                                  and panel in node_setup.panel_names()):
        await query.edit_message_text(
            f"⚠️ Panel `{panel}` is not configured any more.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("📊 Servers", callback_data="list_servers")]]),
            parse_mode="Markdown",
        )
        return
    api = _get_api(context)
    base_name = context.user_data.get('srv_name_base') or _default_server_name(context)
    count = context.user_data.get('srv_count', 1)
    names = _server_names(base_name, count)
    server_type = context.user_data.get('srv_type', '')
    location = context.user_data.get('srv_loc', '')
    image = context.user_data.get('srv_img', '')
    enable_ipv6 = context.user_data.get('srv_ipv6', True)

    await query.edit_message_text(
        f"⏳ Creating {len(names)} server{'s' if len(names) > 1 else ''}...",
        parse_mode="Markdown",
    )

    # Use existing SSH keys if available
    ssh_keys_data = await api.list_ssh_keys()
    ssh_keys = [k['name'] for k in ssh_keys_data] if ssh_keys_data else None

    # 🔄 Retry after a partial failure lands here again: create only what is
    # still missing, or the servers that did get made fail as duplicates.
    done = context.user_data.setdefault('srv_done', set())
    results = []
    for index, name in enumerate(names, start=1):
        if name in done:
            results.append((name, 'done'))
            continue
        await query.edit_message_text(
            f"⏳ Creating server {index}/{len(names)}: `{name}`...",
            parse_mode="Markdown",
        )
        api_key = node_setup.new_api_key() if panel else None
        result = await api.create_server(
            name, server_type, image, location, ssh_keys, enable_ipv6=enable_ipv6,
            user_data=node_setup.cloud_init_script(api_key) if panel else None,
            primary_ipv4=context.user_data.get('srv_pip') if len(names) == 1 else None,
        )
        if result and result.get('server'):
            done.add(name)
            pip = context.user_data.pop('srv_pip', None)
            if pip and len(names) == 1:
                await ip_change.adopt(api, pip)
            if panel:
                server = result['server']
                node_setup.add_job(
                    _current_account(context), server.get('id'), server.get('name'),
                    server.get('public_net', {}).get('ipv4', {}).get('ip'),
                    panel, api_key, query.message.chat_id,
                )
        results.append((name, result))

    lines = []
    ok = 0
    passwords = []
    copy_rows = []
    capacity_failed = False

    for name, result in results:
        if result == 'done':
            ok += 1
            lines.append(f"✅ `{name}` — created earlier")
            continue
        if not result or not result.get('server'):
            error = escape_markdown(api_error_text(result))
            lines.append(f"❌ `{name}` — {error}")
            if is_capacity_refusal(result):
                capacity_failed = True
            continue

        ok += 1
        server = result["server"]
        ip = server.get('public_net', {}).get('ipv4', {}).get('ip', 'N/A')
        lines.append(f"✅ `{server.get('name')}` — `{ip}`")
        pw = result.get('root_password')
        if pw:
            passwords.append(f"• `{server.get('name')}`: `{pw}`")
            copy_rows.append([copy_button(f"📋 Password of {server.get('name')}", pw)])

    text = (
        f"{'✅' if ok == len(results) else '⚠️'} *Server Creation Finished* ({ok}/{len(results)})\n\n"
        f"🔧 Type: `{server_type}`\n"
        f"📍 Location: `{location}`\n"
        f"💿 Image: `{_image_label(context)}`\n"
        f"🌐 IPv6: `{'Enabled' if enable_ipv6 else 'Disabled'}`\n\n"
        + "\n".join(lines)
    )
    if passwords:
        text += "\n\n🔑 *Root passwords:*\n" + "\n".join(passwords)
        text += "\n\n⚠️ Save these passwords now — they won't be shown again."
    elif ok:
        text += "\n\n🗝 SSH keys were installed."
    if panel and ok:
        text += (
            f"\n\n🧩 *Node setup started.* Each new server installs itself "
            f"(about 5-10 minutes, then reboots itself once); the bot adds it to panel `{panel}` and "
            f"messages you here."
        )

    keyboard = copy_rows[:10]
    if capacity_failed and ok < len(results):
        # Hetzner is out of capacity for this type/location right now — help the
        # user pick a different location or retry rather than dead-end.
        text += (
            f"\n\n💡 `{server_type}` has no capacity in `{location}` right now. "
            f"Try another location, or retry shortly."
        )
        keyboard.append([InlineKeyboardButton("📍 Change Location", callback_data=f"srvt_{server_type}")])
        keyboard.append([InlineKeyboardButton(
            "🔄 Retry", callback_data=f"srvnode_{panel}" if panel else "srvgo")])
    keyboard.append([InlineKeyboardButton("📊 View Servers", callback_data="list_servers")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# The custom-name prompt's Cancel, pressed after the prompt ended (main.py's
# conversation handles it while the prompt is open).
router.add(r"srvname_cancel",
           lambda q, c: srv_pick_name(q, c, c.user_data.get('srv_count', 1)))
router.add(r"srvc", srv_pick_category)
router.add(r"srvcs", _category_screen)
router.add(r"snapclone_(\d+)", srv_from_snapshot, int)
router.add(r"srvpip_(\d+)", srv_pick_kept_ip, int)
router.add(r"srvcat_(.+)", srv_pick_type)
router.add(r"srvven_(.+)", srv_pick_vendor)
router.add(r"srvt_(.+)", srv_pick_location)
router.add(r"srvl_(.+)", srv_pick_image)
router.add(r"srvi_(.+)", srv_pick_ipv6)
router.add(r"srvipv6_(\d)", lambda q, c, v: srv_pick_count(q, c, v == "1"))
router.add(r"srvcount_(\d+)", srv_pick_name, int)
router.add(r"srvname_auto", srv_confirm_auto_name)
router.add(r"srvgo", srv_create)
router.add(r"srvnode_([a-z0-9-]+)", srv_create)
router.add(r"srvalert", srv_alert_locations)
router.add(r"srvaloc_(.+)", srv_alert_toggle)
router.add(r"srvaok", srv_alert_ok)
router.add(r"srvalertgo", srv_alert_go)
