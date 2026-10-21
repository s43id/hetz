"""Per-server price overrides for legacy (grandfathered) contracts.

Hetzner's API only reports the current list price, so a server bought years
ago on a cheaper rate is billed at one figure and reported at another. An
override pins what a specific server actually costs, and the cost report and
server panel use it instead of the list price.

Stored in ``price_overrides.json`` as ``{account: {server_id: monthly}}``.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

OVERRIDES_FILE = Path('price_overrides.json')


def _load():
    if OVERRIDES_FILE.exists():
        try:
            data = json.loads(OVERRIDES_FILE.read_text())
            if isinstance(data, dict):
                return data
            logger.warning("%s is not an object, ignoring it", OVERRIDES_FILE)
        except Exception as e:
            logger.error(f"Could not read {OVERRIDES_FILE}: {e}")
    return {}


def _save(data):
    try:
        OVERRIDES_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.error(f"Failed to save price overrides: {e}")


def get_override(account, server_id):
    """The pinned monthly price for this server, or None."""
    raw = _load().get(account, {}).get(str(server_id))
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("Ignoring malformed price override for %s/%s: %r",
                       account, server_id, raw)
        return None


def set_override(account, server_id, monthly):
    data = _load()
    data.setdefault(account, {})[str(server_id)] = float(monthly)
    _save(data)


def clear_override(account, server_id):
    """Drop the override; returns False if there was nothing to drop."""
    data = _load()
    per_account = data.get(account) or {}
    if per_account.pop(str(server_id), None) is None:
        return False
    if not per_account:
        data.pop(account, None)
    _save(data)
    return True


def effective_price(account, server_id, list_price):
    """What this server actually costs: the override if set, else list price."""
    override = get_override(account, server_id)
    return list_price if override is None else override
