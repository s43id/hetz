"""⏸ Pause automations: the kill switch for everything the bot does on its own.

While paused, the background jobs that change servers do nothing:

- 🌙 auto reset and ⏰ scheduled resets are skipped (each skipped run is
  reported, the next one is planned as usual);
- stock alerts (switch when in stock, including the return to a server's own
  plan) and auto-create alerts wait: they stay saved and act once resumed.

Manual buttons (chat and Mini App) keep working, a reset already running
finishes, and the read-only jobs (traffic alerts, 🩺 health checks, node
setups, update notices) go on. A pause can end by itself after some hours
(``until``) or last until the admin resumes.

State in ``automation.json``: ``{paused, since, until}`` (``until`` None =
until resumed).
"""
import json
import logging
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import Config

logger = logging.getLogger(__name__)

STATE_FILE = Path('automation.json')
CHECK_INTERVAL_MINUTES = 1
PAUSE_HOURS = (1, 6, 24)

# What a pause stops, for the screens.
WHAT = ("auto reset, scheduled resets, stock alerts (switch when in stock, the move "
        "back after a reset) and auto-create")


def fmt_time(ts):
    # reset_schedule imports this module (pause check), so import it late.
    from reset_schedule import fmt_time as fmt
    return fmt(ts)


def _load():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            if isinstance(data, dict):
                return data
            logger.warning("%s is not an object, ignoring it", STATE_FILE)
        except Exception as e:
            logger.error(f"Could not read {STATE_FILE}: {e}")
    return {}


def _save(data):
    try:
        STATE_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.error(f"Failed to save the automation state: {e}")


def state(now=None):
    """{paused, since, until}; a pause whose time is up counts as over."""
    data = _load()
    paused = bool(data.get('paused'))
    until = data.get('until')
    if paused and until and until <= (now or time.time()):
        paused = False
    return {'paused': paused, 'since': data.get('since'), 'until': until if paused else None}


def is_paused():
    return state()['paused']


def pause(hours=None):
    """Pause every automation, for ``hours`` or (None / 0) until resumed."""
    now = time.time()
    _save({'paused': True, 'since': int(now),
           'until': int(now + hours * 3600) if hours else None})


def resume():
    _save({'paused': False})


def status_text(st=None):
    """'⏸ Automations paused until …' or '' when they run."""
    st = st or state()
    if not st['paused']:
        return ""
    if st['until']:
        return f"⏸ Automations paused until {fmt_time(st['until'])} (Iran time)"
    return "⏸ Automations paused until you resume them"


def skipped_line():
    """Why a run was skipped, for the job messages."""
    st = state()
    return ("Automations are paused" + (f" until {fmt_time(st['until'])}" if st['until'] else "")
            + "; resume them in 🩺 Health.")


async def check_pause_end(bot):
    """A timed pause that ran out: clear it and say so. Returns True when it ended."""
    data = _load()
    if not data.get('paused') or not data.get('until') or data['until'] > time.time():
        return False
    resume()
    try:
        await bot.send_message(
            chat_id=Config.ADMIN_ID,
            text="▶️ *Automations resumed*: the pause ended. Auto reset, scheduled resets "
                 "and stock alerts work again.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "🩺 Health", callback_data="hl")]]),
            parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Failed to send the resume message: {e}")
    return True
