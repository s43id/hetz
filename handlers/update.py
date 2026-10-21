"""⬆️ Update: running vs latest version, changelog, update and rollback (/update)."""
import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import updater
from utils import escape_markdown

from .router import router

_tasks = set()   # running result reports (kept so they are not garbage-collected)


async def show_update(query, context):
    current = updater.current_version()
    await query.edit_message_text(f"⏳ Checking for a new version (running {current})…")
    latest, changelog, error = await asyncio.to_thread(updater.fetch_latest)
    back_to = await asyncio.to_thread(updater.rollback_version)
    text = f"🧩 *Bot version*\n\nRunning: `{current}`\n"
    keyboard = []
    if error:
        text += f"\n⚠️ Could not check for a new version:\n`{error.replace('`', '')}`\n"
        keyboard.append([InlineKeyboardButton("⬆️ Update anyway", callback_data="upd_go")])
    elif updater.is_newer(latest, current):
        text += f"🆕 New: `{latest}`\n"
        changes = updater.changes_since(changelog, current)
        if changes:
            text += f"\n{escape_markdown(changes)}\n"
        keyboard.append([InlineKeyboardButton(f"⬆️ Update to {latest}", callback_data="upd_go")])
    else:
        text += f"✅ This is the latest version (`{latest}`).\n"
        # e.g. to repair an install whose files or packages were changed by hand
        keyboard.append([InlineKeyboardButton("🔁 Run the update again", callback_data="upd_go")])
    keyboard.append([InlineKeyboardButton("🔄 Check again", callback_data="upd")])
    if back_to:
        keyboard.append([InlineKeyboardButton(f"↩️ Roll back to {back_to}", callback_data="upd_rb")])
    keyboard.append([InlineKeyboardButton("⬅️ Back to Menu", callback_data="start_menu")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                  parse_mode="Markdown")


async def confirm(query, context, action):
    question = ("⬆️ Update the bot and restart it?\n\nIt is offline for a few seconds; running "
                "resets and plan changes are interrupted, so start it when none is running."
                if action == "go" else
                f"↩️ Go back to version {updater.rollback_version() or '?'} and restart?")
    await query.edit_message_text(question, reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=f"upd_{action}_yes"),
        InlineKeyboardButton("❌ Cancel", callback_data="upd"),
    ]]))


async def run(query, context, action):
    kind = "update" if action == "go" else "rollback"
    error = updater.start(kind, query.message.chat_id)
    if error:
        await query.edit_message_text(f"⚠️ {error}", reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back", callback_data="upd")]]))
        return
    await query.edit_message_text(
        f"⏳ {'Update' if kind == 'update' else 'Rollback'} started.\n\n"
        f"The bot restarts in a few seconds; the result is reported here.")
    # Reports here if the script fails before restarting this bot; otherwise
    # the restarted bot reports (main.on_startup).
    _tasks.add(task := asyncio.create_task(updater.report_when_done(context.bot)))
    task.add_done_callback(_tasks.discard)


router.add(r"upd", show_update)
router.add(r"upd_(go|rb)", confirm)
router.add(r"upd_(go|rb)_yes", run)
