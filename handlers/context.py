"""Per-user account context helpers.

The bot can drive several Hetzner accounts at once. Which account a given
chat is currently acting on is stored in ``context.user_data['account']``;
these helpers resolve that into the right API client and overage tracker.
"""
import logging

from hetzner_api import api_registry, HetznerAPI
from overage_tracker import get_overage_tracker, OverageTracker

logger = logging.getLogger(__name__)


def _current_account(context) -> str:
    names = api_registry.names()
    return context.user_data.get('account', names[0] if names else '')


def _get_api(context) -> HetznerAPI:
    name = _current_account(context)
    return api_registry.get(name) or api_registry.default()


def _get_tracker(context) -> OverageTracker:
    return get_overage_tracker(_current_account(context))


async def _resolve_server(context, server_id, fresh=False):
    """Fetch a server, switching the session to whichever account owns it.

    A button can be pressed long after the screen that produced it, possibly
    while a different account is selected — asking the wrong account gives a
    404 and a misleading "server not found". Try the recorded owner first,
    then the current account, then the rest, and remember what we find.
    """
    account_map = context.user_data.setdefault('server_account_map', {})
    names = api_registry.names()
    preferred = [account_map.get(server_id), _current_account(context)]

    order = [n for n in preferred if n in names]
    order += [n for n in names if n not in order]

    tried = []
    for name in order:
        api = api_registry.get(name)
        if not api:
            continue
        tried.append(name)
        server = await api.get_server(server_id, fresh=fresh)
        if server:
            context.user_data['account'] = name
            account_map[server_id] = name
            logger.info("Server %s resolved to account '%s'", server_id, name)
            return server
    logger.warning(
        "Server %s not found in any account (tried: %s)", server_id, ", ".join(tried) or "none"
    )
    return None
