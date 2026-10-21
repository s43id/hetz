import logging
import re

from telegram.error import BadRequest

from config import Config

logger = logging.getLogger(__name__)


class CallbackRouter:
    """Maps Telegram ``callback_data`` strings to handler coroutines.

    Each route is a *full-match* regex. Capture groups are passed to the
    handler as positional arguments, after optionally running each group
    through a per-group converter (e.g. ``int``).

    Full-match semantics make routes order-independent: ``reset_5`` and
    ``resetpw_5`` can never collide, unlike the old ``str.startswith``
    dispatcher where ordering bugs were easy to introduce.
    """

    def __init__(self):
        self._routes = []

    def add(self, pattern, handler, *converters):
        """Register ``handler`` for callback data fully matching ``pattern``.

        ``converters`` are applied positionally to the regex capture groups,
        so ``add(r"server_(\\d+)", show, int)`` calls ``show(query, ctx, 5)``
        for callback data ``server_5``.
        """
        self._routes.append((re.compile(pattern), handler, converters))
        return handler

    async def dispatch(self, query, context):
        """Run the handler for ``query``; returns False if no route matched."""
        data = query.data or ""
        for regex, handler, converters in self._routes:
            match = regex.fullmatch(data)
            if match is None:
                continue
            args = [
                converters[i](group) if i < len(converters) and converters[i] else group
                for i, group in enumerate(match.groups())
            ]
            await handler(query, context, *args)
            return True
        logger.warning("No route matched callback data: %s", data)
        return False


router = CallbackRouter()


async def _report(query, context, text):
    """Tell the user a button failed, as plain text so it cannot itself fail."""
    try:
        await context.bot.send_message(chat_id=query.message.chat_id, text=text)
    except Exception:
        logger.exception("Could not deliver failure notice for %r", query.data)


async def button_handler(update, context):
    """Single entry point for every inline-keyboard button press."""
    query = update.callback_query
    if query.from_user.id != Config.ADMIN_ID:
        await query.answer("⛔ Unauthorized", show_alert=True)
        return
    # Without this, a handler that raises (or a callback with no route) leaves
    # the button looking simply dead — no reply, no error, nothing to debug.
    # query.answer() is inside the try too: it fails on a stale callback query,
    # which would otherwise abort the press before any handler ran.
    try:
        try:
            await query.answer()
        except Exception:
            logger.warning("Could not ack callback %r (stale query?)", query.data)
        logger.info("Callback: %s", query.data)
        if not await router.dispatch(query, context):
            await _report(query, context, f"⚠️ This button is not wired up: {query.data}")
    except BadRequest as e:
        # Re-rendering a screen that already shows exactly this content. The
        # user is looking at the right thing, so treat it as a no-op instead
        # of an error — but never hide any other BadRequest.
        if "not modified" in str(e).lower():
            logger.info("Callback %r re-rendered identical content", query.data)
            return
        logger.exception("Callback %r failed", query.data)
        await _report(
            query, context,
            f"⚠️ That button failed.\n\n{type(e).__name__}: {e}\n\nData: {query.data}",
        )
    except Exception as e:
        logger.exception("Callback %r failed", query.data)
        await _report(
            query, context,
            f"⚠️ That button failed.\n\n{type(e).__name__}: {e}\n\nData: {query.data}",
        )
