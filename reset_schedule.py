"""Traffic resets: the shared run (manual and scheduled) and the schedules.

A schedule resets one server's traffic at a time of day in **Iran time**
(Asia/Tehran), once or every day / week / month. A job runs every minute,
claims the schedules that are due (moves them to their next run, or removes a
one-off) and runs each reset in its own task, so a reset that takes minutes
does not hold up the others.

Every reset, manual or scheduled, goes through ``reset_with_return``:

- a server with a switch watch (typically a return watch left by an earlier
  reset) tries that plan first, so a reset can also bring it home;
- when the trip back to the server's own plan is refused, the server stays
  up on the other plan and a return watch (plan_watch, ``kind='return'``)
  moves it back automatically once the plan is in stock.
"""
import asyncio
import calendar
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import automation
from hetzner_api import api_registry
from overage_tracker import get_overage_tracker
from plan_watch import add_watch, home_plan_of, remove_watches_for
from server_manager import reset_server_traffic, is_busy, RESET_STEPS
from utils import (
    escape_markdown, server_datacenter, server_location, format_traffic,
    included_traffic_tb,
)

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
    IRAN = ZoneInfo("Asia/Tehran")
except Exception:  # no tz database on the host: Iran has kept +03:30 since 2022
    IRAN = timezone(timedelta(hours=3, minutes=30), "Asia/Tehran")

SCHEDULE_FILE = Path('reset_schedules.json')
CHECK_INTERVAL_MINUTES = 1
# A run found more than this late (the bot was down) is skipped and reported:
# a reset powers the server off, which must not land at an unplanned hour.
MISSED_GRACE_MINUTES = 60

REPEATS = {'o': "once", 'd': "every day", 'w': "every week", 'm': "every month"}
# Python weekday numbers (Monday = 0), listed from Saturday like the Iranian week.
WEEKDAYS = [(5, "Saturday"), (6, "Sunday"), (0, "Monday"), (1, "Tuesday"),
            (2, "Wednesday"), (3, "Thursday"), (4, "Friday")]
WEEKDAY_NAME = dict(WEEKDAYS)

# Tasks of running scheduled resets (kept so they are not garbage-collected).
_tasks = set()

# The full log of each server's last reset, for the 📜 Details button
# (memory only: gone after a restart).
_last_logs = {}


# ---------------------------------------------------------------- time

def iran_now():
    return datetime.now(IRAN)


def fmt_time(ts):
    """'Sat 18 Oct 04:00' in Iran time."""
    return datetime.fromtimestamp(ts, IRAN).strftime('%a %d %b %H:%M')


def _at(day, hour, minute):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=IRAN).timestamp()


def next_occurrence(sched, after):
    """Epoch of the first run strictly after ``after``, or None (a past one-off)."""
    hour, minute = sched['hour'], sched['minute']
    repeat, day = sched['repeat'], sched.get('day')
    today = datetime.fromtimestamp(after, IRAN).date()
    if repeat == 'o':
        d = datetime.strptime(str(day), '%Y%m%d').date()
        ts = _at(d, hour, minute)
        return ts if ts > after else None
    if repeat == 'd':
        for i in range(2):
            ts = _at(today + timedelta(days=i), hour, minute)
            if ts > after:
                return ts
    if repeat == 'w':
        d = today + timedelta(days=(int(day) - today.weekday()) % 7)
        for i in range(2):
            ts = _at(d + timedelta(days=7 * i), hour, minute)
            if ts > after:
                return ts
    if repeat == 'm':
        year, month = today.year, today.month
        for _ in range(3):
            last = calendar.monthrange(year, month)[1]
            ts = _at(datetime(year, month, min(int(day), last)), hour, minute)
            if ts > after:
                return ts
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return None


def describe(sched):
    """'every month on day 1 at 04:00' (Iran time)."""
    at = f"{sched['hour']:02d}:{sched['minute']:02d}"
    repeat, day = sched['repeat'], sched.get('day')
    if repeat == 'o':
        d = datetime.strptime(str(day), '%Y%m%d')
        return f"once on {d.strftime('%a %d %b %Y')} at {at}"
    if repeat == 'd':
        return f"every day at {at}"
    if repeat == 'w':
        return f"every {WEEKDAY_NAME.get(int(day), '?')} at {at}"
    return f"every month on day {day} at {at}"


# ---------------------------------------------------------------- storage

def _load():
    if SCHEDULE_FILE.exists():
        try:
            data = json.loads(SCHEDULE_FILE.read_text())
            if isinstance(data, list):
                return data
            logger.warning("%s is not a list, ignoring it", SCHEDULE_FILE)
        except Exception as e:
            logger.error(f"Could not read {SCHEDULE_FILE}: {e}")
    return []


def _save(items):
    try:
        SCHEDULE_FILE.write_text(json.dumps(items, indent=2))
    except Exception as e:
        logger.error(f"Failed to save reset schedules: {e}")


def list_schedules(server_id=None):
    items = _load()
    if server_id is not None:
        items = [s for s in items if s.get('server_id') == server_id]
    return sorted(items, key=lambda s: s.get('next_run') or 0)


def add_schedule(account, server_id, name, chat_id, repeat, day, hour, minute):
    """Save a schedule. Returns (schedule, error); error is a short reason."""
    sched = {
        'account': account, 'server_id': server_id, 'name': name, 'chat_id': chat_id,
        'repeat': repeat, 'day': day, 'hour': hour, 'minute': minute,
    }
    next_run = next_occurrence(sched, time.time())
    if next_run is None:
        return None, "that time has already passed (Iran time)"
    items = _load()
    for s in items:
        if s.get('server_id') == server_id and all(
                s.get(k) == sched[k] for k in ('repeat', 'day', 'hour', 'minute')):
            return None, "this schedule already exists"
    sched.update(id=secrets.token_hex(4), next_run=next_run, created_at=int(time.time()))
    items.append(sched)
    _save(items)
    return sched, None


def remove_schedule(sched_id):
    items = _load()
    kept = [s for s in items if s.get('id') != sched_id]
    if len(kept) != len(items):
        _save(kept)
    return len(items) - len(kept)


def remove_schedules_for(server_id):
    items = _load()
    kept = [s for s in items if s.get('server_id') != server_id]
    if len(kept) != len(items):
        _save(kept)
    return len(items) - len(kept)


def _advance(sched, now):
    """Claim a due schedule: move it to its next run, or drop a one-off."""
    items = [s for s in _load() if s.get('id') != sched.get('id')]
    nxt = None if sched['repeat'] == 'o' else next_occurrence(sched, now)
    if nxt:
        items.append(dict(sched, next_run=nxt, last_run=int(now)))
    _save(items)
    return nxt


# ---------------------------------------------------------------- the reset

async def reset_with_return(api, account, server_id, chat_id, progress_cb=None):
    """Run a traffic reset and keep the server's plan straight afterwards.

    Returns a dict: ok, logs, plan (the plan the server is on now), home (the
    plan it should be on, when that differs), watching (a return watch is set).
    """
    home = home_plan_of(account, server_id)
    before = await api.get_server(server_id, fresh=True) or {}
    traffic_before, limit_tb = before.get('outgoing_traffic'), included_traffic_tb(before)
    ok, logs, pending = await reset_server_traffic(
        server_id, progress_cb, api, get_overage_tracker(account), home_plan=home)
    server = await api.get_server(server_id, fresh=True)
    plan = (server or {}).get('server_type', {}).get('name')
    _last_logs[server_id] = {'at': time.time(), 'logs': logs}
    result = {'ok': ok, 'logs': logs, 'plan': plan, 'home': None, 'watching': False,
              # a stranded reset still zeroed the counter
              'reset': ok or pending is not None,
              'traffic': (traffic_before, (server or {}).get('outgoing_traffic')),
              'limit_tb': limit_tb}
    if plan and home and plan == home:
        remove_watches_for(server_id, home)   # home again: nothing to wait for
        return result
    target = home or pending
    if target and plan and plan != target:
        result['home'] = target
        scope = server_datacenter(server) or server_location(server)
        if scope:
            add_watch(account, scope, target, server_id, chat_id, kind='return')
            result['watching'] = True
    return result


def result_text(result):
    """The closing lines of a reset report (Markdown)."""
    home, plan = result['home'], result['plan']
    if not result['reset']:
        text = "❌ *Process failed. Check logs above.*"
        if home:
            text += (f"\n\n↩️ The server is still on `{plan}`; the bot keeps waiting to "
                     f"move it back to `{home}`.")
        return text
    if not home:
        return "✅ *Process completed successfully!*"
    text = (
        f"⚠️ *Still on* `{plan}` *, not* `{home}`\n\n"
        f"Traffic was reset, but `{home}` had no capacity to move back "
        f"to. You are being billed for `{plan}` until it does.\n"
    )
    if result['watching']:
        text += (f"\n↩️ The bot will switch it back to `{home}` "
                 f"automatically as soon as it is in stock, and tell you.")
    return text


def last_log(server_id):
    """{'at', 'logs'} of the server's last reset since the bot started, or None."""
    return _last_logs.get(server_id)


def progress_text(name, logs, stage):
    """The live one-line status of a running reset (Markdown)."""
    n, label = stage
    last = f"\n\n{logs[-1][0]} {escape_markdown(logs[-1][1])}" if logs else ""
    return (f"♻️ *Traffic Reset* — `{name}`\n\n"
            f"⏳ Step {n}/{RESET_STEPS}: {escape_markdown(label)}…\n"
            f"{'▰' * n}{'▱' * (RESET_STEPS - n)}{last}")


def summary_text(title, name, result):
    """A finished reset: traffic before → after, then result_text (Markdown)."""
    text = f"{title} — `{name}`\n\n"
    before, after = result.get('traffic') or (None, None)
    if result['reset'] and before is not None and after is not None:
        limit = result.get('limit_tb')
        text += (f"📊 Traffic: `{format_traffic(before, limit)}` → "
                 f"`{format_traffic(after, limit)}`\n\n")
    return text + result_text(result)


def details_button(server_id):
    return InlineKeyboardButton("📜 Details", callback_data=f"resetlog_{server_id}")


def log_text(logs):
    return "\n".join(f"{e} {escape_markdown(m)}" for e, m in logs)


# ---------------------------------------------------------------- the job

async def check_reset_schedules(bot):
    """Start the resets that are due. Returns the started tasks (for tests)."""
    now = time.time()
    started = []
    for sched in _load():
        due = sched.get('next_run') or 0
        if due > now:
            continue
        nxt = _advance(sched, now)
        late = now - due
        if late > MISSED_GRACE_MINUTES * 60:
            await _send(bot, sched,
                        f"⏰ *Scheduled reset missed*\n\n"
                        f"`{sched.get('name')}` was due for a traffic reset at "
                        f"{fmt_time(due)} (Iran time), but the bot was not running "
                        f"then. It was skipped so the server is not powered off at an "
                        f"unplanned hour."
                        + (f"\nNext run: {fmt_time(nxt)}." if nxt else ""),
                        server_id=sched.get('server_id'))
            continue
        if automation.is_paused():
            await _send(bot, sched,
                        f"⏸ Scheduled reset of `{sched.get('name')}` skipped. "
                        f"{automation.skipped_line()}"
                        + (f"\nNext run: {fmt_time(nxt)}." if nxt else ""),
                        server_id=sched.get('server_id'))
            continue
        task = asyncio.create_task(_run(bot, sched, nxt))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        started.append(task)
    return started


async def _run(bot, sched, nxt):
    account, server_id = sched.get('account'), sched.get('server_id')
    name = sched.get('name') or str(server_id)
    try:
        api = api_registry.get(account)
        server = await api.get_server(server_id, fresh=True) if api else None
        if not server:
            remove_schedules_for(server_id)
            await _send(bot, sched,
                        f"⏰ The scheduled traffic reset of `{name}` was cancelled: the "
                        f"server no longer exists. Its schedules were removed.")
            return
        name = server.get('name', name)
        if is_busy(server_id):
            await _send(bot, sched,
                        f"⏰ Scheduled reset of `{name}` skipped: a reset or plan change "
                        f"was already running on it.", server_id=server_id)
            return
        await _send(bot, sched,
                    f"⏰ *Scheduled traffic reset started*\n\n`{name}` ({describe(sched)}, "
                    f"Iran time). It is powered off for a few minutes, twice; I will "
                    f"report when it is done.")
        result = await reset_with_return(api, account, server_id, sched.get('chat_id'))
        text = summary_text("⏰ *Scheduled Traffic Reset*", name, result)
        if nxt:
            text += f"\n\nNext run: {fmt_time(nxt)} (Iran time)."
        await _send(bot, sched, text, server_id=server_id, details=True)
    except Exception as e:
        logger.exception("Scheduled reset of %s failed", server_id)
        await _send(bot, sched, f"❌ Scheduled reset of `{name}` failed: "
                                f"{escape_markdown(str(e))}", server_id=server_id)


async def _send(bot, sched, text, server_id=None, details=False):
    keyboard = None
    if server_id is not None:
        row = [InlineKeyboardButton("🖥 Open Server", callback_data=f"server_{server_id}")]
        if details:
            row.insert(0, details_button(server_id))
        keyboard = InlineKeyboardMarkup([row])
    try:
        await bot.send_message(chat_id=sched.get('chat_id'), text=text,
                               reply_markup=keyboard, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Failed to send reset-schedule message: {e}")
