import asyncio
import logging
import sys
import urllib.error
import urllib.request
import warnings
from telegram.warnings import PTBUserWarning
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, TypeHandler, filters, ConversationHandler,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import Config
from hetzner_api import api_registry
from handlers import (
    start_handler, button_handler, _start_console, COMMAND_HANDLERS, set_bot_commands,
    _start_server_name, recv_server_name, cancel_server_name,
    WAIT_SERVER_NAME,
    _start_rename, recv_rename, cancel_rename, WAIT_RENAME,
    _start_price, recv_price, clear_price, cancel_price, WAIT_PRICE,
    _start_node_key, recv_node_key, cancel_node_key, WAIT_NODE_KEY,
)
from monitor import traffic_monitor
from plan_watch import check_plan_watches, CHECK_INTERVAL_MINUTES
from create_watch import check_create_watches
import reset_schedule
import auto_reset
import automation
import health
import node_setup
import webapp
import updater
from shell_handler import (
    recv_port, recv_user, recv_auth_type,
    recv_password, recv_key, recv_command,
    console_cancel, console_disconnect,
    console_back_panel, console_back_port, console_back_user, _close_session,
    WAIT_PORT, WAIT_USER, WAIT_AUTH_TYPE,
    WAIT_PASSWORD, WAIT_KEY, WAIT_COMMAND,
)


def setup_logging():
    level = logging.INFO if Config.DEBUG_MODE else logging.WARNING
    logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=level)
    # httpx logs one line per request, including the ~2s polling loop, and each
    # line contains the bot token in the URL. Keep it quiet even under
    # DEBUG_MODE so our own logs stay readable and the token stays out of them.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def check_hetzner_tokens():
    """Stop only when every account's token is rejected: with one bad account the
    bot keeps serving the others, and 🩺 health.py tells the admin in the chat."""
    rejected = []
    for name in api_registry.names():
        token = Config.HETZNER_ACCOUNTS[name]
        req = urllib.request.Request(
            f"{Config.HETZNER_API_BASE}/servers?per_page=1",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            urllib.request.urlopen(req, timeout=15)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                rejected.append(name)
                logging.warning(f"Hetzner token for account '{name}' is invalid (401)")
            else:
                logging.warning(f"Hetzner API returned HTTP {e.code} for account '{name}'")
        except Exception as e:
            logging.warning(f"Could not verify Hetzner token for account '{name}' (network issue?): {e}")
    if rejected and len(rejected) == len(api_registry.names()):
        sys.exit(
            f"❌ Every HETZNER token is invalid (401): {', '.join(rejected)}.\n"
            "Update the tokens in .env and restart the bot."
        )


# An open text prompt (rename, price, server name, node API key, SSH console) ends after
# this long without input, so a forgotten one cannot swallow later text.
PROMPT_TIMEOUT = 15 * 60


def _end_prompt(update, context):
    """Forget whatever text prompt was open, including an SSH session."""
    for key in ('rename_id', 'price_id', 'node_key'):
        context.user_data.pop(key, None)
    user = getattr(update, "effective_user", None)
    if user:
        _close_session(user.id)


def _fresh_prompt(entry):
    """Start a prompt after closing the previous one (allow_reentry)."""
    async def start(update, context):
        _end_prompt(update, context)
        return await entry(update, context)
    return start


async def leave_prompt(update, context):
    """Another button pressed while a prompt waits: close it, then run the button."""
    _end_prompt(update, context)
    await button_handler(update, context)
    return ConversationHandler.END


async def prompt_timed_out(update, context):
    _end_prompt(update, context)


async def on_error(update, context):
    """Catch anything the individual handlers did not, so a failure is never
    invisible — python-telegram-bot otherwise just logs 'No error handlers
    are registered' and the user sees nothing at all."""
    logging.exception("Unhandled error while processing update", exc_info=context.error)
    chat = getattr(getattr(update, "effective_chat", None), "id", None)
    if chat is None:
        return
    try:
        await context.bot.send_message(
            chat_id=chat,
            text=f"⚠️ Something went wrong.\n\n"
                 f"{type(context.error).__name__}: {context.error}",
        )
    except Exception:
        logging.exception("Could not deliver the error notice")


async def on_startup(app):
    """Command menu, then the Mini App server (if configured) on this event loop."""
    await set_bot_commands(app)
    # An in-bot update restarted us: report how it went to the chat that asked.
    app.bot_data["update_report"] = asyncio.create_task(updater.report_when_done(app.bot))
    try:
        await webapp.start(app.bot)
    except Exception:
        # The bot must keep working even if the Mini App cannot start
        # (port taken, certificate unreadable...).
        logging.exception("Mini App could not start")


async def on_shutdown(app):
    await webapp.stop()


def build_prompt_conversation():
    # Every text prompt lives in this one ConversationHandler, so only one can
    # be open per chat: starting another prompt replaces it (allow_reentry),
    # pressing any other button ends it and handles that button
    # (leave_prompt), and an untouched prompt expires (PROMPT_TIMEOUT).
    prompt_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(_fresh_prompt(_start_console), pattern=r"^console_\d+$"),
            CallbackQueryHandler(_fresh_prompt(_start_server_name), pattern=r"^srvname_custom$"),
            CallbackQueryHandler(_fresh_prompt(_start_rename), pattern=r"^rename_\d+$"),
            CallbackQueryHandler(_fresh_prompt(_start_price), pattern=r"^price_\d+$"),
            CallbackQueryHandler(_fresh_prompt(_start_node_key),
                                 pattern=r"^nodeaddp_\d+_[a-z0-9-]+$"),
        ],
        states={
            WAIT_PORT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_port),
                CallbackQueryHandler(console_back_panel, pattern="^console_back_panel$"),
            ],
            WAIT_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_user),
                CallbackQueryHandler(console_back_port, pattern="^console_back_port$"),
            ],
            WAIT_AUTH_TYPE: [
                CallbackQueryHandler(recv_auth_type, pattern="^auth_"),
                CallbackQueryHandler(console_back_user, pattern="^console_back_user$"),
            ],
            WAIT_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_password),
                CallbackQueryHandler(console_back_user, pattern="^console_back_user$"),
            ],
            WAIT_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_key),
                CallbackQueryHandler(console_back_user, pattern="^console_back_user$"),
            ],
            WAIT_COMMAND: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_command),
            ],
            WAIT_SERVER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_server_name),
            ],
            WAIT_RENAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_rename),
            ],
            WAIT_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_price),
            ],
            WAIT_NODE_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recv_node_key),
            ],
            ConversationHandler.TIMEOUT: [
                TypeHandler(Update, prompt_timed_out),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(console_cancel,      pattern="^console_cancel$"),
            CallbackQueryHandler(console_disconnect,  pattern="^console_disconnect$"),
            CallbackQueryHandler(console_back_panel,  pattern="^console_back_panel$"),
            CallbackQueryHandler(console_back_port,   pattern="^console_back_port$"),
            CallbackQueryHandler(console_back_user,   pattern="^console_back_user$"),
            CallbackQueryHandler(cancel_server_name,  pattern="^srvname_cancel$"),
            CallbackQueryHandler(cancel_rename,       pattern=r"^rename_cancel_\d+$"),
            CallbackQueryHandler(clear_price,         pattern=r"^price_clear_\d+$"),
            CallbackQueryHandler(cancel_price,        pattern=r"^price_cancel_\d+$"),
            CallbackQueryHandler(cancel_node_key,     pattern=r"^nodekey_cancel_\d+$"),
            # Must stay last: any other button closes the prompt first.
            CallbackQueryHandler(leave_prompt),
        ],
        allow_reentry=True,
        conversation_timeout=PROMPT_TIMEOUT,
        per_message=False,
        per_chat=True,
        per_user=True,
    )
    return prompt_conv


def main():
    setup_logging()
    check_hetzner_tokens()
    warnings.filterwarnings("ignore", category=PTBUserWarning)
    app = (Application.builder().token(Config.TELEGRAM_TOKEN)
           .post_init(on_startup).post_shutdown(on_shutdown).build())
    prompt_conv = build_prompt_conversation()

    app.add_handler(CommandHandler("start", start_handler))
    for command, handler in COMMAND_HANDLERS.items():
        app.add_handler(CommandHandler(command, handler))
    app.add_handler(prompt_conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(on_error)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(traffic_monitor, "interval", hours=1, args=[app.bot])
    scheduler.add_job(
        check_plan_watches, "interval",
        minutes=CHECK_INTERVAL_MINUTES, args=[app.bot],
    )
    scheduler.add_job(
        check_create_watches, "interval",
        minutes=CHECK_INTERVAL_MINUTES, args=[app.bot],
    )
    scheduler.add_job(
        updater.check_for_update, "interval",
        seconds=updater.CHECK_INTERVAL_SECONDS, args=[app.bot, Config.ADMIN_ID],
    )
    scheduler.add_job(
        reset_schedule.check_reset_schedules, "interval",
        minutes=reset_schedule.CHECK_INTERVAL_MINUTES, args=[app.bot],
    )
    scheduler.add_job(
        auto_reset.check_auto_reset, "interval",
        minutes=auto_reset.CHECK_INTERVAL_MINUTES, args=[app.bot],
    )
    scheduler.add_job(
        node_setup.check_node_setups, "interval",
        minutes=node_setup.CHECK_INTERVAL_MINUTES, args=[app.bot],
    )
    scheduler.add_job(
        health.check_health, "interval",
        minutes=health.CHECK_INTERVAL_MINUTES, args=[app.bot],
    )
    scheduler.add_job(health.check_daily_report, "interval", minutes=1, args=[app.bot])
    scheduler.add_job(
        automation.check_pause_end, "interval",
        minutes=automation.CHECK_INTERVAL_MINUTES, args=[app.bot],
    )
    scheduler.start()

    accounts_info = ", ".join(api_registry.names())
    logging.info(f"🚀 Bot started — accounts: {accounts_info}")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
