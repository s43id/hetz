"""🌙 Auto reset: a nightly traffic reset of the servers that need one.

Once a night, at an hour the admin picks (Iran time, default 05:00), the bot
looks at every running server and resets the traffic of each one that

- has used ``threshold`` % (default 90) or more of its included traffic, or
- would pass 100 % before the next night at the pace of the last 24 hours
  (``samples`` keeps each server's counter from the previous night).

Every server takes part unless the admin excludes it; the whole feature can be
switched off. A server can have its own time (``times``); the others use the
default time. Each time is a **slot** ("HH:MM") with its own next run, and a
slot's run looks only at its servers. Resets run one after another (gentle on
the Hetzner API) through ``reset_with_return``, so a stranded server gets its
return watch like any other reset. One message says which servers are being
reset, one more the results; a night with nothing to do stays silent. While
automations are paused (automation.py) a due run is skipped and reported.

Settings and samples live in ``auto_reset.json``:
``{enabled, hour, minute, threshold, excluded: [server ids], times: {sid: "HH:MM"},
next_run (default slot), next_runs: {"HH:MM": epoch} (own-time slots), last_run,
samples: {sid: {bytes, at}}, last_summary}``.
"""
import asyncio
import json
import logging
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import automation
from config import Config
from hetzner_api import api_registry
from reset_schedule import (
    MISSED_GRACE_MINUTES, fmt_time, next_occurrence, reset_with_return,
)
from server_manager import is_busy
from utils import escape_markdown, included_traffic_bytes

logger = logging.getLogger(__name__)

STATE_FILE = Path('auto_reset.json')
CHECK_INTERVAL_MINUTES = 1
DEFAULT_HOUR, DEFAULT_MINUTE, DEFAULT_THRESHOLD = 5, 0, 90
THRESHOLDS = (75, 80, 85, 90, 95)
MINUTES = (0, 10, 20, 30, 40, 50)
DAY = 86400

# The running night's task (kept so it is not garbage-collected).
_tasks = set()


# ---------------------------------------------------------------- storage

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
        logger.error(f"Failed to save auto reset settings: {e}")


def _next_run(hour, minute, after=None):
    return next_occurrence({'repeat': 'd', 'hour': hour, 'minute': minute},
                           time.time() if after is None else after)


def _hm(text):
    """'03:30' → (3, 30)."""
    hour, minute = str(text).split(':')
    return int(hour), int(minute)


def settings():
    """The settings with defaults filled in (and next runs set on first use)."""
    data = _load()
    out = {
        'enabled': data.get('enabled', True),
        'hour': int(data.get('hour', DEFAULT_HOUR)),
        'minute': int(data.get('minute', DEFAULT_MINUTE)),
        'threshold': int(data.get('threshold', DEFAULT_THRESHOLD)),
        'excluded': [str(s) for s in data.get('excluded', [])],
        'times': {str(k): v for k, v in (data.get('times') or {}).items()},
        'next_run': data.get('next_run'),
        'next_runs': dict(data.get('next_runs') or {}),
        'last_run': data.get('last_run'),
        'last_summary': data.get('last_summary'),
    }
    changes = {}
    if not out['next_run']:
        out['next_run'] = changes['next_run'] = _next_run(out['hour'], out['minute'])
    default = time_text(out)
    # Own-time slots: one next run each; slots nobody uses any more are dropped.
    wanted = {t for t in out['times'].values() if t != default}
    runs = {t: out['next_runs'].get(t) or _next_run(*_hm(t)) for t in wanted}
    if runs != out['next_runs']:
        out['next_runs'] = changes['next_runs'] = runs
    if changes:
        _update(**changes)
    return out


def _update(**changes):
    data = _load()
    data.update(changes)
    _save(data)


def set_enabled(on):
    """Switch on or off. Switching on starts from the next runs (no catch-up)."""
    s = settings()
    _update(enabled=bool(on), next_run=_next_run(s['hour'], s['minute']),
            next_runs={t: _next_run(*_hm(t)) for t in s['next_runs']})


def _check_time(hour, minute):
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("hour 0-23, minute 0-59")


def set_time(hour, minute):
    """The default time (servers without their own time)."""
    _check_time(hour, minute)
    _update(hour=hour, minute=minute, next_run=_next_run(hour, minute))
    settings()   # a server whose own time is now the default joins its slot


def set_server_time(server_id, hour=None, minute=None):
    """A server's own time, or (None) back to the default time."""
    times = settings()['times']
    if hour is None:
        times.pop(str(server_id), None)
    else:
        _check_time(hour, minute)
        times[str(server_id)] = f"{hour:02d}:{minute:02d}"
    _update(times=times)
    settings()   # plans the new slot's first run, drops an unused one


def slot_of(server_id, s=None):
    """The "HH:MM" this server is checked at."""
    s = s or settings()
    return s['times'].get(str(server_id)) or time_text(s)


def has_own_time(server_id, s=None):
    s = s or settings()
    own = s['times'].get(str(server_id))
    return bool(own) and own != time_text(s)


def set_threshold(percent):
    if not 50 <= percent <= 100:
        raise ValueError("threshold 50-100%")
    _update(threshold=int(percent))


def is_included(server_id):
    return str(server_id) not in settings()['excluded']


def set_included(server_id, on):
    excluded = set(settings()['excluded'])
    (excluded.discard if on else excluded.add)(str(server_id))
    _update(excluded=sorted(excluded))


def time_text(s=None):
    s = s or settings()
    return f"{s['hour']:02d}:{s['minute']:02d}"


def next_run_of(slot, s=None):
    """Epoch of a slot's next run."""
    s = s or settings()
    return s['next_run'] if slot == time_text(s) else s['next_runs'].get(slot)


def server_line(server_id):
    """The server page's line, or '' when auto reset does not cover it."""
    s = settings()
    if not s['enabled'] or str(server_id) in s['excluded']:
        return ""
    own = " (its own time)" if has_own_time(server_id, s) else ""
    return (f"🌙 Auto reset at {s['threshold']}%+ — nightly {slot_of(server_id, s)}"
            f"{own} (Iran time)")


# ---------------------------------------------------------------- the rule

def reason(server, sample, threshold, now, horizon=DAY):
    """Why this server needs a reset tonight ('threshold' / 'pace'), or None.

    ``sample`` is the counter at the previous run; the pace since then, kept up
    for ``horizon`` seconds, must not take it past 100 %. A counter that went
    down since (a reset) gives no pace.
    """
    used = server.get('outgoing_traffic') or 0
    limit = included_traffic_bytes(server)
    if not limit:
        return None
    if used / limit * 100 >= threshold:
        return 'threshold'
    if sample and now > sample.get('at', now) and used >= sample.get('bytes', 0):
        rate = (used - sample['bytes']) / (now - sample['at'])
        if used + rate * horizon >= limit:
            return 'pace'
    return None


# ---------------------------------------------------------------- the job

async def check_auto_reset(bot):
    """Start the runs that are due (one task, slot after slot), or None."""
    s = settings()
    now = time.time()
    if not s['enabled']:
        return None
    default = time_text(s)
    due = [(t, s['next_run'] if t == default else s['next_runs'][t])
           for t in [default, *sorted(s['next_runs'])]]
    due = [(t, at) for t, at in due if at <= now]
    if not due:
        return None
    runs = dict(s['next_runs'])
    for t, _at in due:
        if t == default:
            _update(next_run=_next_run(s['hour'], s['minute'], now))
        else:
            runs[t] = _next_run(*_hm(t), now)
    _update(next_runs=runs, last_run=int(now))
    slots = []
    for t, at in due:
        if now - at > MISSED_GRACE_MINUTES * 60:
            await _send(bot, f"🌙 *Auto reset missed* ({t})\n\nIt was due at {fmt_time(at)} "
                             f"(Iran time), but the bot was not running then. It was skipped "
                             f"so no server is powered off at an unplanned hour.")
        elif automation.is_paused():
            await _send(bot, f"🌙 *Auto reset skipped* ({t} Iran time)\n\n"
                             f"{automation.skipped_line()}")
        else:
            slots.append(t)
    if not slots:
        return None
    task = asyncio.create_task(_run_slots(bot, slots, default))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def _run_slots(bot, slots, default):
    done = []
    for t in slots:
        done += await run_night(bot, slot=t, default=default)
    return done


async def run_night(bot, slot=None, default=None):
    """Check the included running servers of ``slot`` (None: every server) and
    reset the ones that need it.

    Returns the list of {name, reason, fraction, result | skipped} (for tests).
    """
    s = settings()
    now = time.time()
    samples = _load().get('samples', {})
    fresh_samples, todo, seen, complete = dict(samples), [], set(), True
    for account in api_registry.names():
        api = api_registry.get(account)
        try:
            servers = await api.list_servers(fresh=True) or []
        except Exception as e:
            logger.error(f"Auto reset could not list {account}: {e}")
            servers, complete = [], False
        for server in servers:
            sid = str(server.get('id'))
            seen.add(sid)
            if slot is not None and slot_of(sid, s) != slot:
                continue   # checked at its own time
            fresh_samples[sid] = {'bytes': server.get('outgoing_traffic') or 0, 'at': now}
            if sid in s['excluded'] or server.get('status') != 'running':
                continue
            why = reason(server, samples.get(sid), s['threshold'], now)
            if why:
                todo.append((api, account, server, why))
    if complete:   # forget deleted servers (not when an account did not answer)
        fresh_samples = {k: v for k, v in fresh_samples.items() if k in seen}
    _update(samples=fresh_samples)
    if not todo:
        return []

    at = slot or time_text(s)
    names = ", ".join(f"`{srv.get('name')}`" for _, _, srv, _ in todo)
    await _send(bot, f"🌙 *Auto reset started* ({at})\n\n{names}\n\nOne after another; each "
                     f"is powered off for a few minutes, twice. I will report when done.")
    done = []
    for api, account, server, why in todo:
        sid, name = server['id'], server.get('name', str(server['id']))
        entry = {'name': name, 'reason': why,
                 'fraction': (server.get('outgoing_traffic') or 0)
                 / (included_traffic_bytes(server) or 1)}
        if is_busy(sid):
            entry['skipped'] = "a reset or plan change was already running"
        else:
            try:
                entry['result'] = await reset_with_return(api, account, sid, Config.ADMIN_ID)
            except Exception as e:
                logger.exception("Auto reset of %s failed", sid)
                entry['skipped'] = f"error: {e}"
        after = (entry.get('result') or {}).get('traffic', (None, None))[1]
        if after is not None:
            data = _load()
            data.setdefault('samples', {})[str(sid)] = {'bytes': after, 'at': time.time()}
            _save(data)
        done.append(entry)
    await _send(bot, report_text(done, s, at))
    _update(last_summary=summary_line(done, now))
    return done


def summary_line(done, at):
    """'Mon 20 Oct 05:00: 2 reset, 1 failed' for the 🩺 daily report."""
    reset = sum(1 for e in done if (e.get('result') or {}).get('reset'))
    failed = sum(1 for e in done if 'result' in e and not e['result'].get('reset'))
    skipped = sum(1 for e in done if 'skipped' in e)
    parts = [f"{reset} reset"] + ([f"{failed} failed"] if failed else []) \
        + ([f"{skipped} skipped"] if skipped else [])
    return f"{fmt_time(at)}: {', '.join(parts)}"


def report_text(done, s, at=None):
    lines = [f"🌙 *Auto reset* — {at or time_text(s)} Iran time\n"]
    for e in done:
        why = (f"{e['fraction'] * 100:.0f}%" if e['reason'] == 'threshold'
               else f"{e['fraction'] * 100:.0f}%, on pace to pass 100%")
        name = f"`{e['name']}` ({why})"
        if 'skipped' in e:
            lines.append(f"⏭ {name}: skipped, {escape_markdown(e['skipped'])}")
            continue
        r = e['result']
        if not r['reset']:
            lines.append(f"❌ {name}: failed (open the server → 📜 Details)")
        elif r['home']:
            lines.append(f"⚠️ {name}: reset, but still on `{r['plan']}`; "
                         f"moves back to `{r['home']}` once it is in stock")
        else:
            lines.append(f"✅ {name}: reset")
    nxt = next_run_of(at or time_text(s))
    if nxt:
        lines.append(f"\nNext run: {fmt_time(nxt)} (Iran time).")
    return "\n".join(lines)


async def _send(bot, text):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
        "🌙 Auto Reset settings", callback_data="ar")]])
    try:
        await bot.send_message(chat_id=Config.ADMIN_ID, text=text,
                               reply_markup=keyboard, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Failed to send auto reset message: {e}")
