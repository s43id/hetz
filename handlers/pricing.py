"""Helpers for reading prices out of Hetzner's ``/pricing`` payloads.

Hetzner returns ``gross``/``net`` strings nested in a few different shapes
depending on the resource; these helpers normalise that into plain floats.
"""


def _gross(price_dict):
    try:
        return float(price_dict.get("gross", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _server_type_price(stype, location):
    """Monthly gross price of a server type in ``location``.

    Hetzner returns one price entry per location, so reading ``prices[0]``
    blindly reports some other datacenter's price.
    """
    prices = stype.get("prices", [])
    for p in prices:
        if p.get("location") == location:
            return _gross(p.get("price_monthly", {}))
    return _gross(prices[0].get("price_monthly", {})) if prices else 0.0


def _image_price_per_gb(pricing):
    return _gross(pricing.get("image", {}).get("price_per_gb_month", {}))


def _floating_ip_price(pricing, fip):
    loc = fip.get("home_location", {}).get("name")
    for entry in pricing.get("floating_ips", []):
        if entry.get("type") == fip.get("type"):
            for p in entry.get("prices", []):
                if p.get("location") == loc:
                    return _gross(p.get("price_monthly", {}))
    return _gross(pricing.get("floating_ip", {}).get("price_monthly", {}))


def _primary_ip_price(pricing, pip):
    loc = ((pip.get("location") or {}).get("name")
           or (pip.get("datacenter") or {}).get("location", {}).get("name"))
    for entry in pricing.get("primary_ips", []):
        if entry.get("type") == pip.get("type"):
            for p in entry.get("prices", []):
                if p.get("location") == loc:
                    return _gross(p.get("price_monthly", {}))
    return 0.0
