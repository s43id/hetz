"""🩺 Health: watch the Hetzner accounts and send a daily check.

Every ``CHECK_INTERVAL_MINUTES`` (2) ``check_health``:

- **account access**: one uncached ``GET /servers?per_page=1`` per account
  (``HetznerAPI.check_access``). A rejected token (401), a refusal (403) or a
  rate limit (429) is reported at once; no answer / a server error after
  ``DOWN_CHECKS_BEFORE_ALERT`` checks in a row (a short network hiccup stays
  quiet). One message when it goes wrong, one when it answers again.
- every ``ACTIONS_CHECK_MINUTES`` (5), for each account that answers:
  - **locked servers**: a server Hetzner keeps ``locked`` for
    ``LOCKED_ALERT_MINUTES`` (30) while the bot runs nothing on it (a stuck
    action or a block) is reported once, and again when it is unlocked;
  - **failed actions** (``GET /servers/actions?status=error``): new ones are
    logged; ``FAILED_ALERT_COUNT`` (3) within ``FAILED_WINDOW_MINUTES`` (60) on
    one account sends one alert (then quiet for that window). The first check
    after a start only notes where the list is.

``check_daily_report`` (every minute) sends the 🩺 daily check at the time set
in the bot (default ``DEFAULT_REPORT_HOUR``:00 Iran time): per account access,
servers (running / off / locked), near or over the traffic limit, failed
actions in 24 h and the API requests left; last night's 🌙 auto reset, the
pause state and the alerts count.

State in ``health.json``: ``{accounts: {name: {status, code, message, since,
fails, alerted, last_ok}}, locked: {sid: {account, name, since, alerted}},
failed_seen: {account: action id}, failed: [{account, id, server_id, server,
command, error, at}], failed_alert_at: {account: epoch}, actions_at: {account:
epoch}, report: {enabled, hour, minute, next_run, last_run}}``.
"""
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import automation
from config import Config
from hetzner_api import api_registry
from reset_schedule import fmt_time, next_occurrence
from server_manager import is_busy
from utils import escape_markdown, traffic_fraction

logger = logging.getLogger(__name__)

STATE_FILE = Path('health.json')
CHECK_INTERVAL_MINUTES = 2
ACTIONS_CHECK_MINUTES = 5
DOWN_CHECKS_BEFORE_ALERT = 2
LOCKED_ALERT_MINUTES = 30
FAILED_ALERT_COUNT = 3
FAILED_WINDOW_MINUTES = 60
FAILED_KEEP_HOURS = 48
FAILED_KEEP_MAX = 200
DEFAULT_REPORT_HOUR, DEFAULT_REPORT_MINUTE = 9, 0
REPORT_MINUTES = (0, 10, 20, 30, 40, 50)

STATUS = {
    'ok': ("✅", "API ok"),
    'auth': ("🚨", "token rejected (401)"),
    'forbidden': ("🚨", "access refused (403)"),
    'limited': ("⚠️", "rate limited (429)"),
    'down': ("⚠️", "not answering"),
    'error': ("⚠️", "API error"),
}

COMMANDS = {
    'change_type': "plan change", 'poweroff': "power off", 'poweron': "power on",
    'shutdown': "shutdown", 'reboot': "reboot", 'reset': "hard reset",
    'create_image': "snapshot", 'rebuild_server': "rebuild", 'create_server': "create",
    'delete_server': "delete", 'start_server': "start", 'stop_server': "stop",
    'enable_backup': "backups on", 'disable_backup': "backups off",
    'change_protection': "protection", 'reset_password': "password reset",
    'attach_volume': "attach volume", 'detach_volume': "detach volume",
    'assign_primary_ip': "assign IP", 'unassign_primary_ip': "unassign IP",
}


def command_label(command):
    return COMMANDS.get(command, (command or "?").replace('_', ' '))


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
        logger.error(f"Failed to save the health state: {e}")


def classify(status):
    if status == 200:
        return 'ok'
    if status == 401:
        return 'auth'
    if status == 403:
        return 'forbidden'
    if status == 429:
        return 'limited'
    if status == 0 or status >= 500:
        return 'down'
    return 'error'


def account_state(name, data=None):
    data = data if data is not None else _load()
    return (data.get('accounts') or {}).get(name) or {'status': 'ok'}


def account_line(name, st, api=None):
    """'✅ `main` — API ok · 3,412 of 3,600 requests left' (Markdown)."""
    emoji, label = STATUS.get(st.get('status'), STATUS['error'])
    line = f"{emoji} `{name}` — {label}"
    if st.get('status') != 'ok':
        if st.get('message'):
            line += f": {escape_markdown(st['message'][:120])}"
        if st.get('since'):
            line += f" · since {fmt_time(st['since'])}"
    elif api is not None and api.rate_remaining is not None:
        line += f" · {api.rate_remaining:,} of {api.rate_limit or 3600:,} requests left"
    return line


def problems():
    """[(account, state)] of the accounts that are not fine now."""
    data = _load()
    return [(n, account_state(n, data)) for n in api_registry.names()
            if account_state(n, data).get('status', 'ok') != 'ok']


def locked_list():
    return sorted((dict(v, server_id=int(k)) for k, v in (_load().get('locked') or {}).items()),
                  key=lambda x: x.get('since') or 0)


def failed_since(seconds, account=None):
    cut = time.time() - seconds
    return [f for f in (_load().get('failed') or [])
            if (f.get('at') or 0) >= cut and (account is None or f.get('account') == account)]


# ---------------------------------------------------------------- the checks

async def check_health(bot):
    """Account access every run; locks and failed actions every few minutes."""
    data = _load()
    accounts = data.setdefault('accounts', {})
    now = time.time()
    for name in api_registry.names():
        api = api_registry.get(name)
        try:
            status, code, message = await api.check_access()
        except Exception as e:
            status, code, message = 0, '', str(e)
        st = accounts.get(name) or {'status': 'ok'}
        new = classify(status)
        if new == 'ok':
            if st.get('status', 'ok') != 'ok' and st.get('alerted'):
                await _send(bot, f"✅ *Hetzner account* `{name}` *answers again*\n\n"
                                 f"It was {STATUS[st['status']][1]} since "
                                 f"{fmt_time(st.get('since') or now)} (Iran time).")
            accounts[name] = {'status': 'ok', 'last_ok': int(now)}
            if now - ((data.get('actions_at') or {}).get(name) or 0) >= ACTIONS_CHECK_MINUTES * 60 - 5:
                data.setdefault('actions_at', {})[name] = int(now)
                await _check_servers(bot, data, name, api, now)
            continue
        if st.get('status') != new:
            st = {'status': new, 'since': int(now), 'fails': 0,
                  'alerted': st.get('alerted', False) and st.get('status') != 'ok',
                  'last_ok': st.get('last_ok')}
        st.update(code=code, message=message, fails=st.get('fails', 0) + 1)
        if not st.get('alerted') and (new in ('auth', 'forbidden', 'limited')
                                      or st['fails'] >= DOWN_CHECKS_BEFORE_ALERT):
            await _send(bot, _problem_text(name, st), pause=True)
            st['alerted'] = True
        accounts[name] = st
    _save(data)


def _problem_text(name, st):
    detail = escape_markdown(f"{st.get('code') or ''} {st.get('message') or ''}".strip()[:200])
    why = {
        'auth': ("The token was revoked, or the project or account was deleted or blocked. "
                 "Nothing works on this account until it answers again. Check the Hetzner "
                 "console and your email; a new token goes into `.env` (`hetzner env`)."),
        'forbidden': ("The account or project may be locked or blocked, or the token is "
                      "read-only. Check the Hetzner console and your email."),
        'limited': ("Too many requests this hour. The bot slows down by itself; if it keeps "
                    "happening, pause the automations for a while."),
        'down': "No answer from the Hetzner API (network or Hetzner trouble).",
        'error': "The API answered with an error.",
    }[st['status']]
    title = {'auth': "rejects its token", 'forbidden': "refuses access",
             'limited': "is rate limited", 'down': "does not answer",
             'error': "answers with an error"}[st['status']]
    emoji = STATUS[st['status']][0]
    return (f"{emoji} *Hetzner account* `{name}` *{title}*\n\n"
            + (f"Hetzner: {detail}\n\n" if detail else "") + why)


async def _check_servers(bot, data, account, api, now):
    try:
        servers = await api.list_servers(fresh=True) or []
    except Exception as e:
        logger.error(f"Health check could not list {account}: {e}")
        return
    names = {s.get('id'): s.get('name') for s in servers}
    locked = data.setdefault('locked', {})
    for sid, entry in list(locked.items()):
        if entry.get('account') != account:
            continue
        server = next((s for s in servers if str(s.get('id')) == sid), None)
        if server is None or not server.get('locked'):
            if entry.get('alerted') and server is not None:
                await _send(bot, f"🔓 `{entry.get('name')}` is unlocked again.",
                            server_id=int(sid))
            locked.pop(sid)
    for s in servers:
        if not s.get('locked'):
            continue
        sid = str(s.get('id'))
        entry = locked.setdefault(sid, {'account': account, 'name': s.get('name'),
                                        'since': int(now), 'alerted': False})
        if (not entry['alerted'] and not is_busy(s.get('id'))
                and now - entry['since'] >= LOCKED_ALERT_MINUTES * 60):
            await _send(bot, f"🔒 *Server locked* — `{s.get('name')}` ({escape_markdown(account)})\n\n"
                             f"Hetzner has kept it locked for {int((now - entry['since']) // 60)} "
                             f"minutes, and the bot is running nothing on it. While locked "
                             f"nothing can be done on it (power, plan, reset). A lock this long "
                             f"usually means a stuck action or a block: check the Hetzner "
                             f"console and your email.", server_id=s.get('id'), pause=True)
            entry['alerted'] = True

    actions = await api.list_server_actions(status='error')
    if actions is None:
        return
    seen = (data.get('failed_seen') or {}).get(account)
    newest = max((a.get('id') or 0 for a in actions), default=seen or 0)
    data.setdefault('failed_seen', {})[account] = max(newest, seen or 0)
    if seen is None:
        return   # first look: only note where the list is
    log = data.setdefault('failed', [])
    for a in sorted(actions, key=lambda a: a.get('id') or 0):
        if (a.get('id') or 0) <= seen:
            continue
        sid = next((r.get('id') for r in a.get('resources') or [] if r.get('type') == 'server'),
                   None)
        log.append({'account': account, 'id': a.get('id'), 'server_id': sid,
                    'server': names.get(sid) or (f"#{sid}" if sid else "?"),
                    'command': a.get('command'),
                    'error': ((a.get('error') or {}).get('message')
                              or (a.get('error') or {}).get('code') or ''),
                    'at': _epoch(a.get('finished') or a.get('started')) or int(now)})
    keep = now - FAILED_KEEP_HOURS * 3600
    data['failed'] = [f for f in log if (f.get('at') or 0) >= keep][-FAILED_KEEP_MAX:]
    window = FAILED_WINDOW_MINUTES * 60
    recent = [f for f in data['failed'] if f['account'] == account and f['at'] >= now - window]
    last_alert = (data.get('failed_alert_at') or {}).get(account) or 0
    if len(recent) >= FAILED_ALERT_COUNT and now - last_alert >= window:
        data.setdefault('failed_alert_at', {})[account] = int(now)
        lines = "\n".join(failed_line(f) for f in recent[-5:])
        await _send(bot, f"❌ *{len(recent)} failed Hetzner actions in the last hour* — "
                         f"{escape_markdown(account)}\n\n{lines}\n\nMany failures in a row can "
                         f"mean capacity trouble, or that Hetzner limits this account. Pause "
                         f"the automations if it goes on.", pause=True)


def failed_line(f):
    return (f"• `{f.get('server')}`: {command_label(f.get('command'))} — "
            f"{escape_markdown((f.get('error') or '?')[:100])} ({fmt_time(f.get('at') or 0)})")


def _epoch(iso):
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(str(iso).replace('Z', '+00:00')).timestamp())
    except ValueError:
        return None


# ---------------------------------------------------------------- daily report

def report_settings():
    data = _load()
    r = dict(data.get('report') or {})
    out = {'enabled': r.get('enabled', True),
           'hour': int(r.get('hour', DEFAULT_REPORT_HOUR)),
           'minute': int(r.get('minute', DEFAULT_REPORT_MINUTE)),
           'next_run': r.get('next_run'), 'last_run': r.get('last_run')}
    if not out['next_run']:
        out['next_run'] = _next_report(out['hour'], out['minute'])
        _set_report(next_run=out['next_run'])
    return out


def _next_report(hour, minute, after=None):
    return next_occurrence({'repeat': 'd', 'hour': hour, 'minute': minute},
                           time.time() if after is None else after)


def _set_report(**changes):
    data = _load()
    data['report'] = dict(data.get('report') or {}, **changes)
    _save(data)


def set_report_enabled(on):
    r = report_settings()
    _set_report(enabled=bool(on), next_run=_next_report(r['hour'], r['minute']))


def set_report_time(hour, minute):
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("hour 0-23, minute 0-59")
    _set_report(hour=hour, minute=minute, next_run=_next_report(hour, minute))


def report_time_text(r=None):
    r = r or report_settings()
    return f"{r['hour']:02d}:{r['minute']:02d}"


async def check_daily_report(bot):
    """Send the daily check when it is due. Returns True when it was sent."""
    r = report_settings()
    now = time.time()
    if not r['enabled'] or r['next_run'] > now:
        return False
    _set_report(next_run=_next_report(r['hour'], r['minute'], now), last_run=int(now))
    await _send(bot, await report_text(), pause=False, health=True)
    return True


async def report_text():
    """The 🩺 daily check (Markdown), from fresh lists."""
    import auto_reset
    from create_watch import list_create_watches
    from plan_watch import list_watches
    from reset_schedule import list_schedules
    data = _load()
    now = time.time()
    lines = [f"🩺 *Daily check* — {fmt_time(now)} (Iran time)"]
    for name in api_registry.names():
        api = api_registry.get(name)
        st = account_state(name, data)
        lines.append("")
        lines.append(account_line(name, st, api))
        if st.get('status', 'ok') != 'ok':
            continue
        try:
            servers = await api.list_servers(fresh=True) or []
        except Exception:
            servers = []
        running = [s for s in servers if s.get('status') == 'running']
        off = [s for s in servers if s.get('status') != 'running']
        text = f"🖥 {len(servers)} servers"
        if off:
            names = ", ".join(f"`{s.get('name')}`" for s in off[:5]) + (" …" if len(off) > 5 else "")
            text += f": {len(running)} running, {len(off)} not running ({names})"
        elif servers:
            text += ", all running"
        lines.append(text)
        fr = [traffic_fraction(s) for s in servers]
        near = sum(1 for f in fr if Config.WARNING_THRESHOLD <= f < 1)
        over = sum(1 for f in fr if f >= 1)
        if near or over:
            lines.append(f"🔥 {near} near the traffic limit" + (f", {over} over it" if over else ""))
        locked = [s for s in servers if s.get('locked')]
        if locked:
            lines.append("🔒 Locked now: " + ", ".join(f"`{s.get('name')}`" for s in locked[:5]))
        failed = failed_since(86400, name)
        lines.append(f"❌ Failed actions in 24 h: {len(failed)}" if failed
                     else "❌ No failed actions in 24 h")
    ar = auto_reset.settings()
    lines.append("")
    if ar['enabled']:
        lines.append(f"🌙 Auto reset: {escape_markdown(ar.get('last_summary') or 'no run yet')}"
                     f" · next {fmt_time(ar['next_run'])}")
    else:
        lines.append("🌙 Auto reset: off")
    pause = automation.status_text()
    lines.append(pause if pause else "▶️ Automations: running")
    pending = len(list_watches()) + len(list_create_watches()) + len(list_schedules())
    lines.append(f"🔔 Alerts & schedules: {pending}")
    return "\n".join(lines)


async def _send(bot, text, server_id=None, pause=False, health=True):
    rows = []
    if server_id is not None:
        rows.append([InlineKeyboardButton("🖥 Open Server", callback_data=f"server_{server_id}")])
    row = []
    if pause and not automation.is_paused():
        row.append(InlineKeyboardButton("⏸ Pause automations", callback_data="apause"))
    if health:
        row.append(InlineKeyboardButton("🩺 Health", callback_data="hl"))
    if row:
        rows.append(row)
    try:
        await bot.send_message(chat_id=Config.ADMIN_ID, text=text,
                               reply_markup=InlineKeyboardMarkup(rows) if rows else None,
                               parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Failed to send a health message: {e}")
