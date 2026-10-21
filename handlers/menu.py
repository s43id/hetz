"""Start command, main menu, and account switching."""
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import Config
from hetzner_api import api_registry

from .router import router
from .context import _current_account
from .ui import _main_menu_keyboard, menu_text
from .servers import show_server_list
from .cost import show_overage_cost
from .snapshots import show_snapshots
from .ips import show_ip_list

# Account-wide screens that offer "show this for another account" buttons
# (ui.account_switch_rows), by the screen key used in their callback data.
_ACCOUNT_SCREENS = {
    "overage_cost": show_overage_cost,
    "snapshots": show_snapshots,
    "fips": lambda q, c: show_ip_list(q, c, "fip"),
    "pips": lambda q, c: show_ip_list(q, c, "pip"),
}


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != Config.ADMIN_ID:
        return
    account = _current_account(context)
    keyboard = _main_menu_keyboard(account)
    await update.message.reply_text(
        await menu_text(),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def set_account(query, context, account_name: str):
    if account_name not in api_registry.names():
        # button_handler already answered this callback, so an alert would be
        # invisible — Telegram only shows the first response to a query.
        await query.edit_message_text(
            f"⚠️ Unknown account: `{account_name}`",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="start_menu")]]
            ),
            parse_mode="Markdown",
        )
        return
    context.user_data['account'] = account_name
    context.user_data['server_filter'] = account_name
    context.user_data.pop('fip_sel', None)
    context.user_data.pop('pip_sel', None)
    await show_server_list(query, context)


async def show_start_menu(query, context):
    account = _current_account(context)
    keyboard = _main_menu_keyboard(account)
    await query.edit_message_text(
        await menu_text(),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


router.add(r"start_menu", show_start_menu)
router.add(r"setaccount_(.+)", set_account)


async def use_account(query, context, screen, account_name):
    """Re-show an account-wide screen for another account."""
    if account_name in api_registry.names():
        context.user_data['account'] = account_name
        context.user_data.pop('fip_sel', None)
        context.user_data.pop('pip_sel', None)
    await _ACCOUNT_SCREENS[screen](query, context)


router.add(r"useacct_(overage_cost|snapshots|fips|pips)_(.+)", use_account)
