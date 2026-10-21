import ipaddress

from config import Config

TB = 1024 ** 4


def escape_markdown(text):
    """Make arbitrary text safe inside a legacy-Markdown message.

    Only ``_ * ` [`` open an entity in Telegram's legacy Markdown, so those
    are all that need escaping — escaping anything else just leaves visible
    backslashes. Hetzner error codes like ``resource_unavailable`` would
    otherwise open an italic run that never closes, and Telegram rejects the
    whole message with "can't find end of the entity".
    """
    out = str(text)
    for ch in ('_', '*', '`', '['):
        out = out.replace(ch, '\\' + ch)
    return out


def is_capacity_error(message):
    """Whether Hetzner refused a change for want of capacity, not for good.

    Those clear up on their own and are worth retrying or watching; a disk or
    architecture refusal never will be. Takes only the error message, which is
    all a plan change reports back.
    """
    text = (message or "").lower()
    return "placement" in text or "unavailable" in text or "resource" in text


def is_capacity_refusal(result):
    """Same question for a full API error body ``{"error": {code, message}}``.

    Stricter than is_capacity_error on purpose: it is used for creates, where
    "resource" would also match ``resource_limit_exceeded`` (the account's
    server limit), which never clears by waiting.
    """
    error = (result or {}).get('error') or {}
    code = (error.get('code') or '').lower()
    message = (error.get('message') or '').lower()
    return (code in ('resource_unavailable', 'placement_error')
            or 'placement' in message or 'unavailable' in message)


def api_error_text(result):
    """Hetzner's error message plus its code, for showing to the user."""
    error = (result or {}).get('error') or {}
    message = error.get('message') or error.get('code') or 'no response from Hetzner'
    code = error.get('code')
    return f"{message} ({code})" if code and code not in message else message


def type_family(name):
    """'cx' for 'cx23', 'ccx' for 'ccx13'."""
    return "".join(c for c in (name or "") if c.isalpha()).lower()


def type_location_entry(stype, location):
    """A server type's entry for one location, or None if not offered there.

    Hetzner removed /datacenters on 2026-06-02 and moved stock onto each
    server type: `locations[]` names where the plan is sold, and each entry's
    `available` flag says whether it can be ordered there right now.
    """
    for entry in (stype or {}).get('locations') or []:
        if entry.get('name') == location:
            return entry
    return None


def type_available_at(stype, location):
    """Can this plan be ordered in ``location`` right now?"""
    entry = type_location_entry(stype, location)
    return bool(entry and entry.get('available'))


def type_offered_at(stype, location):
    """Is this plan sold in ``location`` at all (stock aside)?"""
    return type_location_entry(stype, location) is not None


def type_has_location_data(stype):
    """Whether the payload carries the locations array we read stock from."""
    return bool((stype or {}).get('locations'))


def server_location(server):
    """Location name of a server, across both payload shapes.

    Hetzner nests the location under ``datacenter`` on some responses and
    returns it at the top level on others (some servers come back with no
    ``datacenter`` key at all). Reading only the nested form silently yields
    "", which shows as an unknown location and picks the wrong
    per-location price.
    """
    s = server or {}
    loc = (s.get('datacenter') or {}).get('location') or s.get('location') or {}
    return loc.get('name') or ''


def server_datacenter(server):
    """Datacenter name if the payload carries one, else ''."""
    return ((server or {}).get('datacenter') or {}).get('name') or ''


def included_traffic_bytes(server):
    """Traffic included with this server, in bytes.

    Hetzner sets this per plan and location — EU datacenters include 20 TB
    while US/Singapore ones include far less — so trust the value the API
    reports and only fall back to the configured default.
    """
    included = (server or {}).get('included_traffic')
    return included if included else Config.TRAFFIC_LIMIT_BYTES


def included_traffic_tb(server):
    return included_traffic_bytes(server) / TB


def overage_tb(server):
    """Outgoing traffic beyond what this server includes, in TB."""
    over = (server or {}).get('outgoing_traffic', 0) - included_traffic_bytes(server)
    return max(0.0, over / TB)


def overage_price_per_tb(server):
    """What one TB of extra traffic costs for this server (gross, €).

    Hetzner publishes it per location in the plan's prices
    (``prices[].price_per_tb_traffic``); fall back to the configured default
    when the payload does not carry it.
    """
    s = server or {}
    location = server_location(s)
    for price in (s.get('server_type') or {}).get('prices') or []:
        if price.get('location') == location:
            raw = (price.get('price_per_tb_traffic') or {}).get('gross')
            try:
                if raw is not None:
                    return float(raw)
            except (TypeError, ValueError):
                pass
            break
    return Config.OVERAGE_PRICE_PER_TB


def overage_cost(server):
    """This server's current overage, in €."""
    return overage_tb(server) * overage_price_per_tb(server)


def server_disk_gb(server):
    """The server's real disk size in GB.

    A disk upgrade sticks to the server, so ``primary_disk_size`` can exceed
    the plan's nominal disk — and it is that number a new plan has to match,
    since Hetzner never shrinks a disk.
    """
    s = server or {}
    return s.get('primary_disk_size') or (s.get('server_type') or {}).get('disk') or 0


def format_traffic(bytes_value, limit_tb=None):
    tb = bytes_value / TB
    limit = Config.TRAFFIC_LIMIT_TB if limit_tb is None else limit_tb
    return f"{tb:.2f}/{limit:g} TB"

def get_traffic_emoji(traffic_tb, limit_tb=None):
    limit = Config.TRAFFIC_LIMIT_TB if limit_tb is None else limit_tb
    percentage = (traffic_tb / limit) * 100 if limit else 0

    if percentage >= 85:
        return "🔴"
    elif percentage >= 70:
        return "🟠"
    elif percentage >= 50:
        return "🟡"
    elif percentage >= 25:
        return "🟢"
    else:
        return "⚪"

def get_location_info(location_code):
    locations = {
        'nbg1': ('Germany', '🇩🇪'),
        'fsn1': ('Germany', '🇩🇪'),
        'hel1': ('Finland', '🇫🇮'),
        'ash': ('USA', '🇺🇸'),
        'hil': ('USA', '🇺🇸'),
        'sin': ('Singapore', '🇸🇬'),
    }
    
    return locations.get(location_code.lower(), ('Unknown', '🏳️'))


def floating_ip_command(fip):
    """The command that makes a server answer on a floating IP, as Hetzner shows it.

    IPv4: ``sudo ip addr add 1.2.3.4 dev eth0``. A floating IPv6 is a /64
    network; the server takes its first address (``2a01:db8::1/64``). It
    lasts until the next reboot.
    """
    addr = fip.get("ip", "")
    if fip.get("type") == "ipv6" or ":" in addr:
        host = ipv6_host(addr)
        if host:
            addr = f"{host[0]}/{host[1]}"
    return f"sudo ip addr add {addr} dev eth0"


def ipv6_host(network):
    """(first address, prefix length) of an IPv6 network like '2a01:db8::/64', or None."""
    try:
        net = ipaddress.ip_network(network, strict=False)
    except ValueError:
        return None
    return str(net.network_address + 1), net.prefixlen


def traffic_fraction(server):
    """Outgoing traffic as a fraction of the server's included traffic."""
    limit = included_traffic_bytes(server)
    return (server or {}).get('outgoing_traffic', 0) / limit if limit else 0.0


def traffic_bar(fraction, width=10):
    """'▓▓▓▓▓▓░░░░' for 0.6; rounds down, so it is full only at 100% and above."""
    filled = min(width, max(0, int(fraction * width + 1e-9)))
    return "▓" * filled + "░" * (width - filled)


def copy_button(label, text):
    """A button that copies ``text`` to the clipboard (Telegram caps it at 256 chars)."""
    from telegram import CopyTextButton, InlineKeyboardButton
    return InlineKeyboardButton(label, copy_text=CopyTextButton(str(text)[:256]))
