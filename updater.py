"""Version and self-update (/update): check origin/main, update, roll back, notice.

The version is ``VERSION`` in this folder and the changelog notes per version
are in ``CHANGELOG.md`` (Persian, "## x.y.z" sections). The bot folder is a
git checkout of ``main``; the latest version is read from ``origin/main``
with a fetch.

An update runs ``hetzner-bot.sh update`` (rollback: ``... rollback``) as its
own systemd unit, because the script restarts this bot. A copy of the
script runs, since ``git pull`` replaces the file while bash is reading it.
The script records the commit before the pull (``.update_prev``, the
rollback target) and writes "<exit code> <unix time>" to ``.update_result``.
The bot that started it reports the result when it is back
(``.update_request`` says to which chat).
"""
import asyncio
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

BOT_DIR = Path(__file__).resolve().parent
BRANCH = "main"
CHECK_INTERVAL_SECONDS = 60     # looks at the commit main points to (git ls-remote)
FULL_CHECK_SECONDS = 30 * 60    # full check when that look fails
REPORT_TIMEOUT = 600          # seconds to wait for the update script to finish
TMP_SCRIPT = "/tmp/hetzner-bot-update.sh"


def _path(name):
    return BOT_DIR / name


def current_version():
    try:
        return _path("VERSION").read_text(encoding="utf-8").strip() or "?"
    except OSError:
        return "?"


def parse_version(version):
    try:
        return tuple(int(x) for x in str(version).strip().split("."))
    except ValueError:
        return (0,)


def is_newer(latest, current):
    return bool(latest) and parse_version(latest) > parse_version(current)


def changes_since(changelog, current, limit=2500):
    """The changelog sections ("## 1.2.0") newer than ``current``."""
    sections, keep = [], False
    for line in (changelog or "").splitlines():
        if line.startswith("## "):
            keep = is_newer(line[3:].split()[0], current)
        if keep:
            sections.append(line)
    text = "\n".join(sections).strip()
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n…"


def _git(*args, timeout=60):
    # The service may run without HOME, where git looks for its credentials.
    env = dict(os.environ, HOME=os.environ.get("HOME") or "/root")
    return subprocess.run(["git", "-C", str(BOT_DIR), *args], capture_output=True, text=True,
                          timeout=timeout, env=env)


def _prefix():
    """This folder's path inside the repository, e.g. 'bot/hetzner/'."""
    return _git("rev-parse", "--show-prefix").stdout.strip()


def fetch_latest():
    """(latest version, changelog, error) from origin/main. Blocking: run in a thread."""
    try:
        if _git("rev-parse", "--git-dir").returncode != 0:
            return None, "", "the bot folder is not a git checkout"
        result = _git("fetch", "-q", "origin", BRANCH)
        if result.returncode != 0:
            return None, "", (result.stderr.strip() or "git fetch failed")[-300:]
        prefix = _prefix()
        version = _git("show", f"FETCH_HEAD:{prefix}VERSION").stdout.strip()
        changelog = _git("show", f"FETCH_HEAD:{prefix}CHANGELOG.md").stdout
    except Exception as e:
        return None, "", str(e)[-300:]
    return (version or None), changelog, ("" if version else "no VERSION on origin/main")


def remote_head():
    """The commit main points to on GitHub, or None. Blocking. A cheap
    ls-remote: no download and no lock in the checkout, so it can run every
    minute without getting in the way of `git pull` in an update."""
    try:
        result = _git("ls-remote", "origin", f"refs/heads/{BRANCH}", timeout=30)
    except Exception:
        return None
    words = result.stdout.split()
    return words[0] if result.returncode == 0 and words else None


def rollback_version():
    """The version a rollback would return to, or None (no earlier update recorded)."""
    try:
        prev = _path(".update_prev").read_text().strip()
    except OSError:
        return None
    if not re.fullmatch(r"[0-9a-f]{7,40}", prev):
        return None
    try:
        shown = _git("show", f"{prev}:{_prefix()}VERSION")
    except Exception:
        return None
    return (shown.stdout.strip() or "?") if shown.returncode == 0 else "?"


def _read_request():
    try:
        return json.loads(_path(".update_request").read_text())
    except (OSError, ValueError):
        return None


def _systemd_run(command):
    subprocess.run(
        ["systemd-run", "--unit", f"hetzner-bot-update-{int(time.time())}", "--collect",
         "--quiet", "--setenv=HOME=/root", "/bin/bash", "-c", command],
        check=True, capture_output=True, text=True, timeout=30)


def start(action, chat_id):
    """Start "update" or "rollback" in its own systemd unit. Error text, or ''."""
    script = _path("hetzner-bot.sh")
    if not script.is_file():
        return f"{script} not found"
    request = _read_request()
    if request and time.time() - request.get("time", 0) < REPORT_TIMEOUT:
        return "An update is already running; wait a few minutes."
    if action == "rollback" and not rollback_version():
        return "No earlier version recorded yet (it is kept from the next update on)."
    _path(".update_request").write_text(json.dumps({
        "chat_id": chat_id, "action": action, "from_version": current_version(),
        "time": time.time()}))
    arg = "rollback" if action == "rollback" else "update"
    log = _path(".update.log")
    try:
        _systemd_run(f"cp '{script}' {TMP_SCRIPT} && bash {TMP_SCRIPT} {arg} > '{log}' 2>&1")
    except Exception as e:
        _path(".update_request").unlink(missing_ok=True)
        detail = getattr(e, "stderr", "") or str(e)
        return f"Could not start the update: {str(detail).strip()[-300:]}"
    logger.warning("%s started from chat %s", action, chat_id)
    return ""


def _log_tail(chars=1500):
    try:
        text = _path(".update.log").read_text(errors="replace")
    except OSError:
        return ""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)[-chars:].strip()


async def report_when_done(bot):
    """Tell the chat that started an update how it went, once the script finished.

    Called right after starting it and again at startup (the update restarts
    the bot); whichever call claims the request file reports.
    """
    request = _read_request()
    if not request:
        return
    started = request.get("time", 0)
    result = None
    while time.time() < started + REPORT_TIMEOUT:
        try:
            if _path(".update_result").stat().st_mtime >= started:
                result = _path(".update_result").read_text().split()
                break
        except (OSError, ValueError):
            pass
        await asyncio.sleep(3)
    try:
        _path(".update_request").unlink()
    except FileNotFoundError:
        return  # the other call reported already
    what = "Rollback" if request.get("action") == "rollback" else "Update"
    code = result[0] if result else None
    if code == "0":
        text = (f"✅ {what} done\n"
                f"Version: {request.get('from_version', '?')} → {current_version()}")
    else:
        status = "did not finish in time" if code is None else "failed"
        text = (f"❌ {what} {status}\n\n{_log_tail()}\n\n"
                f"If the bot misbehaves: /update → Roll back, or on the server: hetzner rollback")
    try:
        await bot.send_message(chat_id=request.get("chat_id"), text=text)
    except Exception as e:
        logger.error(f"Could not report the update result: {e}")


# ------------------------------------------------------------------ new-version notice

def _state():
    try:
        return json.loads(_path("update_state.json").read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state):
    try:
        _path("update_state.json").write_text(json.dumps(state))
    except OSError as e:
        logger.error(f"Could not save update_state.json: {e}")


_last_check = {"head": None, "full": 0.0}


def update_running():
    """An update started from the bot has not been reported yet."""
    request = _read_request()
    return bool(request) and time.time() - request.get("time", 0) < REPORT_TIMEOUT


async def check_for_update(bot, chat_id):
    """Scheduled every minute: tell the admin once about each new version on
    origin/main. Only a commit not checked yet is fetched (or, when ls-remote
    fails, a full check every FULL_CHECK_SECONDS)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    head = await asyncio.to_thread(remote_head)
    if head:
        if head == _last_check["head"]:
            return
    elif time.time() - _last_check["full"] < FULL_CHECK_SECONDS:
        return
    if update_running():
        return
    _last_check["full"] = time.time()
    latest, changelog, error = await asyncio.to_thread(fetch_latest)
    if error:
        return
    _last_check["head"] = head
    current = current_version()
    if not is_newer(latest, current) or _state().get("notified") == latest:
        return
    changes = changes_since(changelog, current)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=(f"🆕 New version of the Hetzner bot: {current} → {latest}"
                  + (f"\n\n{changes}" if changes else "")),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"⬆️ Update to {latest}", callback_data="upd_go")]]),
        )
    except Exception as e:
        logger.error(f"Could not send the new-version notice: {e}")
        _last_check["head"] = None
        return
    _save_state(dict(_state(), notified=latest))
