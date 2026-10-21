"""What the Mini App can read and do (webapp.py serves it over HTTPS).

Every function returns plain JSON-able data or raises ``ApiError``. They reuse
the bot's own pieces (reset_with_return, change_plan, plan_watch,
reset_schedule, create_watch, node_setup), so the app and the chat bot follow
the same rules: a server is never left off, the stock flag is advisory, a
smaller disk is refused before anything is powered off.

The rest (IPs, volumes, snapshots, rebuild, rename, price, backups, root
password, delete, Add to Panel, cost) are direct calls; Hetzner's own error
message comes back as the ``ApiError`` text.

Slow actions (reset, plan change, power, create) run as **jobs**: an asyncio
task whose progress the page polls (``job(id)``); a finished reset, plan
change or create is also reported in the admin's Telegram chat.
"""
import asyncio
import logging
import secrets
import time
from datetime import datetime

import auto_reset
import automation
import create_watch
import health
import ip_change
import node_setup
import plan_watch
import reset_schedule
import updater
from config import Config
from hetzner_api import api_registry
from overage_tracker import get_overage_tracker
from price_overrides import clear_override, effective_price, get_override, set_override
from server_manager import RESET_STEPS, activity, change_plan, is_busy
from utils import (
    api_error_text, floating_ip_command, get_location_info, included_traffic_bytes,
    ipv6_host, is_capacity_refusal, overage_cost, overage_price_per_tb, server_datacenter,
    server_disk_gb, server_location, traffic_fraction, type_available_at,
    type_has_location_data, type_offered_at,
)
from handlers.cost import collect_costs
from handlers.pricing import (
    _floating_ip_price, _gross, _image_price_per_gb, _primary_ip_price, _server_type_price,
)
from handlers.resize import _adjacent_resize_target, _resize_options
from handlers.create_server import _SERVER_NAME_RE, _clone_base, _server_names
from handlers.servers import _RENAME_RE, forget_server, is_protected
from handlers.health import running_items

logger = logging.getLogger(__name__)

# Create-wizard groups: (id, label, name prefixes).
CATEGORIES = [
    ("cx", "Cost-optimized Intel (CX)", ["cx"]),
    ("cax", "Cost-optimized ARM (CAX)", ["cax"]),
    ("cpx", "Regular AMD (CPX)", ["cpx"]),
    ("ccx", "Dedicated vCPU (CCX)", ["ccx"]),
]
MAX_CREATE = 10
MAX_IPS = 5
VOLUME_SIZES = (10, 10240)    # GB, Hetzner's limits
VOLUME_DETACH_WAIT = 5        # seconds between detaching and deleting a volume
PLAN_ATTEMPTS = 3
PLAN_RETRY_SECONDS = 15


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------ reading

def _accounts():
    return [(name, api_registry.get(name)) for name in api_registry.names()]


async def _all_servers():
    """[(account, server)] across every account (each list cached 10 s)."""
    accounts = _accounts()
    lists = await asyncio.gather(*(api.list_servers() for _, api in accounts),
                                 return_exceptions=True)
    return [(name, s) for (name, _), items in zip(accounts, lists)
            if isinstance(items, list) for s in items]


async def _find(server_id, fresh=True):
    """(account, api, server) of a server in any account, or ApiError 404."""
    for account, server in await _all_servers():
        if server.get("id") == server_id:
            api = api_registry.get(account)
            if fresh:
                server = await api.get_server(server_id, fresh=True) or server
            return account, api, server
    raise ApiError("Server not found (deleted?)", 404)


async def _node_tags():
    if not node_setup.can_register():
        return {}
    try:
        return await node_setup.node_panels()
    except Exception:
        return {}


def _ip(server):
    return ((server.get("public_net") or {}).get("ipv4") or {}).get("ip") or ""


def _ipv6(server):
    net = ((server.get("public_net") or {}).get("ipv6") or {}).get("ip") or ""
    host = ipv6_host(net) if net else None
    return host[0] if host else ""


def _return_plan(server_id):
    for w in plan_watch.list_watches():
        if w.get("server_id") == server_id and plan_watch.is_return(w):
            return w.get("plan")
    return None


def server_summary(account, s, tags=None):
    stype = s.get("server_type") or {}
    location = server_location(s)
    country, flag = get_location_info(location)
    list_price = _server_type_price(stype, location)
    schedules = reset_schedule.list_schedules(s.get("id"))
    return {
        "id": s.get("id"),
        "name": s.get("name"),
        "account": account,
        "status": s.get("status"),
        "plan": stype.get("name"),
        "cores": stype.get("cores"),
        "memory": stype.get("memory"),
        "disk": server_disk_gb(s),
        "plan_disk": stype.get("disk"),
        "arch": stype.get("architecture"),
        "location": location,
        "country": country,
        "flag": flag,
        "ip": _ip(s),
        "ipv6": _ipv6(s),
        "traffic": s.get("outgoing_traffic") or 0,
        "traffic_limit": included_traffic_bytes(s),
        "fraction": round(traffic_fraction(s), 4),
        "overage": round(overage_cost(s), 2),
        "overage_per_tb": overage_price_per_tb(s),
        "price": round(effective_price(account, s.get("id"), list_price), 2),
        "list_price": round(list_price, 2),
        "price_override": get_override(account, s.get("id")) is not None,
        "backups": bool(s.get("backup_window")),
        "panel": (tags or {}).get(_ip(s)),
        "return_to": _return_plan(s.get("id")),
        "next_reset": schedules[0]["next_run"] if schedules else None,
        "next_reset_text": reset_schedule.fmt_time(schedules[0]["next_run"]) if schedules else None,
        "busy": is_busy(s.get("id")),
        "protected": is_protected(s),
        "locked": bool(s.get("locked")),
    }


async def dashboard():
    servers, tags = await asyncio.gather(_all_servers(), _node_tags())
    items = [server_summary(a, s, tags) for a, s in servers]
    near = [x for x in items if Config.WARNING_THRESHOLD <= x["fraction"] < 1]
    over = [x for x in items if x["fraction"] >= 1]
    return {
        "accounts": api_registry.names(),
        "servers": len(items),
        "running": sum(1 for x in items if x["status"] == "running"),
        "near": len(near),
        "over": len(over),
        "cost": round(sum(x["price"] for x in items), 2),
        "overage": round(sum(x["overage"] for x in items), 2),
        "alerts": (len(plan_watch.list_watches()) + len(create_watch.list_create_watches())
                   + len(reset_schedule.list_schedules())),
        "top": sorted(items, key=lambda x: -x["fraction"])[:5],
        "warning": Config.WARNING_THRESHOLD,
        "jobs": running_jobs(),
        "iran_now": reset_schedule.iran_now().strftime("%a %d %b %H:%M"),
        "version": updater.current_version(),
        "paused": automation.status_text() or None,
        "problems": [{"account": n, "status": st.get("status"),
                      "label": health.STATUS.get(st.get("status"), health.STATUS["error"])[1]}
                     for n, st in health.problems()],
        "activity": len(activity()),
    }


async def version():
    current = updater.current_version()
    latest, changelog, error = await asyncio.to_thread(updater.fetch_latest)
    return {"current": current, "latest": latest, "error": error,
            "newer": updater.is_newer(latest, current),
            "changes": updater.changes_since(changelog, current) if latest else "",
            "rollback": await asyncio.to_thread(updater.rollback_version)}


def start_update(action):
    """Start an update / rollback; the result is reported in the admin's chat."""
    if action not in ("update", "rollback"):
        raise ApiError("action must be update or rollback")
    error = updater.start(action, Config.ADMIN_ID)
    if error:
        raise ApiError(error, 409)
    return {"ok": True}


async def servers():
    found, tags = await asyncio.gather(_all_servers(), _node_tags())
    return {"servers": [server_summary(a, s, tags) for a, s in found],
            "accounts": api_registry.names(), "warning": Config.WARNING_THRESHOLD}


async def server_detail(server_id):
    account, api, server = await _find(server_id)
    tags = await _node_tags()
    data = server_summary(account, server, tags)
    location = server_location(server)
    disk = server_disk_gb(server)
    plans, offlist = await _resize_options(server, api)
    current = server.get("server_type") or {}
    watching = {w.get("plan"): w for w in plan_watch.list_watches()
                if w.get("server_id") == server_id}
    data["plans"] = sorted(({
        "name": t.get("name"),
        "cores": t.get("cores"),
        "memory": t.get("memory"),
        "disk": t.get("disk"),
        "price": round(_server_type_price(t, location), 2),
        "in_stock": t.get("id") not in offlist,
        "disk_ok": (t.get("disk") or 0) >= disk,
        "bigger": ((t.get("cores") or 0), (t.get("memory") or 0))
                  > ((current.get("cores") or 0), (current.get("memory") or 0)),
        "watching": t.get("name") in watching,
    } for t in plans), key=lambda p: (p["name"][:3], p["price"], p["name"]))
    nav = {}
    for direction in ("prev", "next"):
        plan, _error = await _adjacent_resize_target(server, api, direction)
        nav[direction] = plan.get("name") if plan else None
    data["nav"] = nav
    data["schedules"] = [_schedule_out(s) for s in reset_schedule.list_schedules(server_id)]
    data["watches"] = [_watch_out(w, server) for w in watching.values()]
    data["last_reset"] = _last_reset(server_id)
    ar = auto_reset.settings()
    data["auto_reset"] = dict(_auto_out(), included=auto_reset.is_included(server_id),
                              server_time=auto_reset.slot_of(server_id, ar),
                              own_time=auto_reset.has_own_time(server_id, ar))
    data["jobs"] = [j for j in running_jobs() if j["server_id"] == server_id]
    return data


def _last_reset(server_id):
    entry = reset_schedule.last_log(server_id)
    if not entry:
        return None
    return {"at": reset_schedule.fmt_time(entry["at"]),
            "logs": [f"{e} {m}" for e, m in entry["logs"]]}


def _schedule_out(s):
    return {"id": s.get("id"), "server_id": s.get("server_id"), "name": s.get("name"),
            "account": s.get("account"), "text": reset_schedule.describe(s),
            "repeat": s.get("repeat"), "day": s.get("day"), "hour": s.get("hour"),
            "minute": s.get("minute"), "next_run": s.get("next_run"),
            "next_text": reset_schedule.fmt_time(s.get("next_run") or 0)}


def _watch_out(w, server=None):
    return {"server_id": w.get("server_id"), "plan": w.get("plan"),
            "account": w.get("account"), "location": plan_watch.watch_location(w),
            "auto": w.get("auto", True), "return": plan_watch.is_return(w),
            "failures": w.get("failures") or 0, "retry_after": w.get("retry_after") or 0,
            "server_name": (server or {}).get("name"),
            "current": ((server or {}).get("server_type") or {}).get("name")}


async def alerts():
    found = await _all_servers()
    servers = {s.get("id"): s for _, s in found}
    ar = auto_reset.settings()
    excluded = ar["excluded"]
    return {
        "watches": [_watch_out(w, servers.get(w.get("server_id")))
                    for w in plan_watch.list_watches()],
        "creates": [{
            "id": c.get("id"), "plan": c.get("plan"), "locations": c.get("locations", []),
            "account": c.get("account"), "image": c.get("image"), "ipv6": c.get("ipv6"),
            "name": c.get("name"), "failures": c.get("failures") or 0,
        } for c in create_watch.list_create_watches()],
        "schedules": [_schedule_out(s) for s in reset_schedule.list_schedules()],
        "interval": plan_watch.CHECK_INTERVAL_MINUTES,
        "auto_reset": dict(_auto_out(), servers=sorted(({
            "id": s.get("id"), "name": s.get("name"), "account": a,
            "included": str(s.get("id")) not in excluded, "status": s.get("status"),
            "fraction": round(traffic_fraction(s), 4),
            "time": auto_reset.slot_of(s.get("id"), ar),
            "own_time": auto_reset.has_own_time(s.get("id"), ar),
        } for a, s in found), key=lambda x: x["name"] or "")),
        "paused": automation.status_text() or None,
    }


def _auto_out():
    s = auto_reset.settings()
    return {"enabled": s["enabled"], "hour": s["hour"], "minute": s["minute"],
            "time": auto_reset.time_text(s), "threshold": s["threshold"],
            "thresholds": list(auto_reset.THRESHOLDS), "minutes": list(auto_reset.MINUTES),
            "next_text": reset_schedule.fmt_time(s["next_run"]) if s["enabled"] else None}


def auto_reset_set(enabled=None, hour=None, minute=None, threshold=None):
    """Change the nightly auto reset: any of on / off, time (Iran), threshold %."""
    if hour is not None or minute is not None:
        s = auto_reset.settings()
        try:
            hour = int(s["hour"] if hour is None else hour)
            minute = int(s["minute"] if minute is None else minute)
            auto_reset.set_time(hour, minute)
        except (TypeError, ValueError):
            raise ApiError("Invalid time")
    if threshold is not None:
        try:
            auto_reset.set_threshold(int(threshold))
        except (TypeError, ValueError):
            raise ApiError("The threshold must be 50-100%")
    if enabled is not None:
        auto_reset.set_enabled(bool(enabled))
    return _auto_out()


async def auto_reset_server(server_id, on=None, hour=None, minute=None, default_time=False):
    """One server: take part or not, and its own time (or back to the default)."""
    await _find(server_id, fresh=False)
    if on is not None:
        auto_reset.set_included(server_id, bool(on))
    if default_time:
        auto_reset.set_server_time(server_id, None)
    elif hour is not None or minute is not None:
        try:
            auto_reset.set_server_time(server_id, int(hour), int(minute or 0))
        except (TypeError, ValueError):
            raise ApiError("Invalid time")
    ar = auto_reset.settings()
    return dict(_auto_out(), included=auto_reset.is_included(server_id),
                server_time=auto_reset.slot_of(server_id, ar),
                own_time=auto_reset.has_own_time(server_id, ar))


# ------------------------------------------------------------------ health, pause, running

def health_data():
    data = health._load()
    r = health.report_settings()
    st = automation.state()
    accounts = []
    for name in api_registry.names():
        a = health.account_state(name, data)
        api = api_registry.get(name)
        emoji, label = health.STATUS.get(a.get("status"), health.STATUS["error"])
        accounts.append({"name": name, "status": a.get("status", "ok"), "label": label,
                         "message": a.get("message") or "",
                         "since": reset_schedule.fmt_time(a["since"]) if a.get("since") else None,
                         "rate_remaining": api.rate_remaining, "rate_limit": api.rate_limit})
    return {
        "accounts": accounts,
        "paused": st["paused"], "pause_text": automation.status_text() or None,
        "pause_hours": list(automation.PAUSE_HOURS), "what": automation.WHAT,
        "locked": [dict(x, since=reset_schedule.fmt_time(x.get("since") or 0))
                   for x in health.locked_list()],
        "failed": [dict(f, label=health.command_label(f.get("command")),
                        at=reset_schedule.fmt_time(f.get("at") or 0))
                   for f in reversed(health.failed_since(86400))],
        "report": {"enabled": r["enabled"], "hour": r["hour"], "minute": r["minute"],
                   "time": health.report_time_text(r), "minutes": list(health.REPORT_MINUTES),
                   "next_text": reset_schedule.fmt_time(r["next_run"]) if r["enabled"] else None},
        "checks": {"interval": health.CHECK_INTERVAL_MINUTES,
                   "locked_minutes": health.LOCKED_ALERT_MINUTES,
                   "failed_count": health.FAILED_ALERT_COUNT},
    }


def automation_set(pause_hours=None, resume=False):
    """⏸ Pause (hours, 0 = until resumed) or ▶️ resume every automation."""
    if resume:
        automation.resume()
    else:
        try:
            hours = int(pause_hours or 0)
        except (TypeError, ValueError):
            raise ApiError("Invalid pause length")
        if hours < 0 or hours > 24 * 30:
            raise ApiError("Invalid pause length")
        automation.pause(hours or None)
    return health_data()


def report_set(enabled=None, hour=None, minute=None):
    r = health.report_settings()
    if hour is not None or minute is not None:
        try:
            health.set_report_time(int(r["hour"] if hour is None else hour),
                                   int(r["minute"] if minute is None else minute))
        except (TypeError, ValueError):
            raise ApiError("Invalid time")
    if enabled is not None:
        health.set_report_enabled(bool(enabled))
    return health_data()


async def report_now():
    """The daily check, now (also sent to the chat by webapp.py)."""
    return {"ok": True, "summary": await health.report_text()}


async def running():
    own, hetzner = await running_items()
    return {"bot": own, "hetzner": hetzner}


# ------------------------------------------------------------------ jobs

_jobs = {}
MAX_JOBS = 50


def _new_job(kind, server_id, title):
    job = {"id": secrets.token_hex(6), "kind": kind, "server_id": server_id,
           "title": title, "stage": "Starting…", "step": 0, "steps": 0, "logs": [],
           "done": False, "ok": None, "result": None, "started": time.time()}
    _jobs[job["id"]] = job
    for old in sorted(_jobs.values(), key=lambda j: j["started"])[:-MAX_JOBS]:
        if old["done"]:
            _jobs.pop(old["id"], None)
    return job


def _run(job, coro, notify=None):
    async def runner():
        try:
            await coro
        except ApiError as e:
            job.update(ok=False, result={"text": str(e)})
        except Exception as e:  # never leave the page waiting forever
            logger.exception("Mini App job %s failed", job["kind"])
            job.update(ok=False, result={"text": f"Unexpected error: {e}"})
        finally:
            job["done"] = True
            job["finished"] = time.time()
        if notify:
            try:
                await notify(job)
            except Exception as e:
                logger.error(f"Could not report a Mini App job in Telegram: {e}")
    job["task"] = asyncio.create_task(runner())
    return job


def job(job_id):
    j = _jobs.get(job_id)
    if not j:
        raise ApiError("Job not found (the bot may have restarted)", 404)
    return {k: v for k, v in j.items() if k != "task"}


def running_jobs():
    return [{k: v for k, v in j.items() if k != "task"}
            for j in _jobs.values() if not j["done"]]


def _busy_check(server_id):
    if is_busy(server_id):
        raise ApiError("A reset or plan change is running on this server", 409)


# ------------------------------------------------------------------ actions

async def power(server_id, action, notify=None):
    if action not in ("on", "off"):
        raise ApiError("action must be on or off")
    _account, api, server = await _find(server_id)
    _busy_check(server_id)
    name = server.get("name")
    j = _new_job("power", server_id, f"Power {action} {name}")

    async def work():
        j["stage"] = "Working…"
        result = await (api.power_on(server_id) if action == "on" else api.power_off(server_id))
        if not result:
            raise ApiError("Hetzner refused the request")
        ok = await api.wait_for_status(server_id, "running" if action == "on" else "off")
        j.update(ok=bool(ok), result={"text": "Done" if ok else
                                      "Requested, but the server status has not changed yet"})
    return job(_run(j, work())["id"])


async def reset(server_id, notify=None):
    account, api, server = await _find(server_id)
    _busy_check(server_id)
    name = server.get("name")
    j = _new_job("reset", server_id, f"Traffic reset of {name}")
    j["steps"] = RESET_STEPS

    async def progress(logs, stage):
        j.update(step=stage[0], stage=stage[1], logs=[f"{e} {m}" for e, m in logs])

    async def work():
        result = await reset_schedule.reset_with_return(api, account, server_id,
                                                        Config.ADMIN_ID, progress)
        j["logs"] = [f"{e} {m}" for e, m in result["logs"]]
        before, after = result.get("traffic") or (None, None)
        j.update(ok=bool(result["ok"]), result={
            "reset": result["reset"], "plan": result["plan"], "home": result["home"],
            "watching": result["watching"], "before": before, "after": after,
            "limit": (result.get("limit_tb") or 0) * 1024 ** 4,
            "summary": reset_schedule.summary_text("♻️ *Traffic Reset* (Mini App)", name, result),
        })
    return job(_run(j, work(), notify)["id"])


async def change(server_id, plan, upgrade_disk=False, notify=None):
    account, api, server = await _find(server_id)
    _busy_check(server_id)
    types = await api.get_server_types()
    target = next((t for t in types if t.get("name") == plan), None)
    if not target:
        raise ApiError(f"Plan {plan} not found")
    current = (server.get("server_type") or {}).get("name")
    if plan == current:
        raise ApiError("The server is already on this plan")
    disk = server_disk_gb(server)
    if (target.get("disk") or 0) < disk:
        raise ApiError(f"{plan}'s disk ({target.get('disk')} GB) is smaller than the server's "
                       f"({disk} GB); Hetzner never shrinks a disk")
    if target.get("architecture") != (server.get("server_type") or {}).get("architecture"):
        raise ApiError("That plan has another CPU architecture")
    upgrade_disk = bool(upgrade_disk) and (target.get("disk") or 0) > disk
    name = server.get("name")
    j = _new_job("plan", server_id, f"Plan change of {name} to {plan}")

    async def log(line):
        j["logs"].append(line)
        j["stage"] = line

    async def work():
        outcome = await change_plan(api, server_id, plan, upgrade_disk=upgrade_disk, log=log,
                                    attempts=PLAN_ATTEMPTS, retry_seconds=PLAN_RETRY_SECONDS)
        if outcome["ok"]:
            plan_watch.remove_watches_for(server_id, plan)
        j.update(ok=outcome["ok"], result={
            "text": (f"Now on {plan}" if outcome["ok"] else
                     f"Not changed: {outcome['error']}"),
            "capacity": outcome["capacity"], "plan": plan,
            "summary": (f"⚖️ *Plan change* (Mini App) — `{name}`\n\n"
                        + ("✅ Now on " if outcome["ok"] else "❌ Still on ")
                        + f"`{plan if outcome['ok'] else current}`"),
        })
    return job(_run(j, work(), notify)["id"])


async def set_watch(server_id, plan, on):
    account, api, server = await _find(server_id, fresh=False)
    if on:
        scope = server_datacenter(server) or server_location(server)
        if not scope:
            raise ApiError("The server's location is unknown")
        plan_watch.add_watch(account, scope, plan, server_id, Config.ADMIN_ID)
    else:
        plan_watch.remove_watches_for(server_id, plan)
    return {"ok": True}


async def add_schedule(server_id, repeat, day, hour, minute):
    if repeat not in reset_schedule.REPEATS:
        raise ApiError("Invalid repeat")
    try:
        day, hour, minute = int(day or 0), int(hour), int(minute)
    except (TypeError, ValueError):
        raise ApiError("Invalid day or time")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ApiError("Invalid time")
    if repeat == "w" and not 0 <= day <= 6 or repeat == "m" and not 1 <= day <= 31:
        raise ApiError("Invalid day")
    if repeat == "o":
        try:
            datetime.strptime(str(day), "%Y%m%d")
        except ValueError:
            raise ApiError("Invalid date")
    account, _api, server = await _find(server_id, fresh=False)
    sched, error = reset_schedule.add_schedule(
        account, server_id, server.get("name"), Config.ADMIN_ID, repeat,
        day if repeat != "d" else 0, hour, minute)
    if error:
        raise ApiError(error[:1].upper() + error[1:])
    return _schedule_out(sched)


def delete_schedule(sched_id):
    if not reset_schedule.remove_schedule(sched_id):
        raise ApiError("Schedule not found", 404)
    return {"ok": True}


def delete_create_alert(alert_id):
    create_watch.remove_create_watch(alert_id)
    return {"ok": True}


# ------------------------------------------------------------------ create

async def create_options(account):
    api = api_registry.get(account)
    if not api:
        raise ApiError("Unknown account")
    types, locations, images, keys, snaps, kept = await asyncio.gather(
        api.get_server_types(), api.list_locations(), api.list_images("system"),
        api.list_ssh_keys(), api.list_images(), ip_change.kept_ipv4s(api))
    plans = []
    for t in types:
        if t.get("deprecation"):
            continue
        category = next((c[0] for c in CATEGORIES
                         if any((t.get("name") or "").startswith(p) for p in c[2])), None)
        if not category:
            continue
        prices = {}
        for p in t.get("prices") or []:
            try:
                prices[p.get("location")] = round(float((p.get("price_monthly") or {})
                                                        .get("gross") or 0), 2)
            except (TypeError, ValueError):
                pass
        has_data = type_has_location_data(t)
        plans.append({
            "name": t.get("name"), "category": category, "arch": t.get("architecture"),
            "cores": t.get("cores"), "memory": t.get("memory"), "disk": t.get("disk"),
            "prices": prices, "stock_known": has_data,
            "in_stock": [l.get("name") for l in locations
                         if not has_data or type_available_at(t, l.get("name"))],
            "offered": [l.get("name") for l in locations
                        if not has_data or type_offered_at(t, l.get("name"))],
        })
    plans.sort(key=lambda p: (p["cores"] or 0, p["memory"] or 0, p["name"]))
    return {
        "account": account,
        "accounts": api_registry.names(),
        "categories": [{"id": c[0], "label": c[1]} for c in CATEGORIES],
        "plans": plans,
        "locations": [{"name": l.get("name"), "city": l.get("city") or l.get("name"),
                       "country": get_location_info(l.get("name"))[0],
                       "flag": get_location_info(l.get("name"))[1]} for l in locations],
        "images": [{"name": i.get("name"), "description": i.get("description") or i.get("name"),
                    "arch": i.get("architecture")}
                   for i in sorted(images, key=lambda i: i.get("name") or "")
                   if not i.get("deprecated")],
        "snapshots": [{"id": i.get("id"),
                       "description": i.get("description") or i.get("name") or str(i.get("id")),
                       "arch": i.get("architecture") or "x86", "disk": i.get("disk_size") or 0,
                       "server_name": (i.get("created_from") or {}).get("name")}
                      for i in sorted(snaps, key=lambda i: i.get("created") or "", reverse=True)
                      if i.get("status") == "available"],
        "kept_ips": [_kept_out(p) for p in kept],
        "panels": node_setup.panel_names() if node_setup.can_create_nodes() else [],
        "ssh_keys": len(keys or []),
        "max_count": MAX_CREATE,
        "default_name": datetime.now().strftime("%m%d%H%M"),
    }


async def _validate_create(account, plan, image):
    api = api_registry.get(account)
    if not api:
        raise ApiError("Unknown account")
    types, images, snaps = await asyncio.gather(
        api.get_server_types(), api.list_images("system"), api.list_images())
    stype = next((t for t in types if t.get("name") == plan and not t.get("deprecation")), None)
    if not stype:
        raise ApiError(f"Plan {plan} not found")
    if str(image).isdigit():
        # 🧬 one of the account's snapshots
        snap = next((i for i in snaps if str(i.get("id")) == str(image)), None)
        if not snap or snap.get("status") != "available":
            raise ApiError("That snapshot is not available")
        if snap.get("architecture", stype.get("architecture")) != stype.get("architecture"):
            raise ApiError("That snapshot is for another CPU architecture")
        if (snap.get("disk_size") or 0) > (stype.get("disk") or 0):
            raise ApiError(f"{plan}'s disk ({stype.get('disk')} GB) is smaller than the "
                           f"snapshot's ({snap.get('disk_size')} GB)")
        return api, stype, snap
    img = next((i for i in images if i.get("name") == image
                and i.get("architecture") == stype.get("architecture")), None)
    if not img:
        raise ApiError("That image is not for this plan's architecture")
    return api, stype, None


def _kept_out(p):
    return {"id": p.get("id"), "ip": p.get("ip"), "location": ip_change.pip_location(p)}


def _check_name(base, count):
    if not _SERVER_NAME_RE.match(base or ""):
        raise ApiError("Name: lowercase letters, digits and - only (1-63 characters)")
    if count > 1 and len(f"{base}-{count}") > 63:
        raise ApiError("Name too long")


async def create(account, plan, location, image, ipv6=False, count=1, name=None,
                 panel=None, notify=None, ipv4_id=None):
    api, stype, snap = await _validate_create(account, plan, image)
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise ApiError("Invalid count")
    if not 1 <= count <= MAX_CREATE:
        raise ApiError(f"Count must be 1 to {MAX_CREATE}")
    if panel:
        if not node_setup.can_create_nodes() or panel not in node_setup.panel_names():
            raise ApiError(f"Panel {panel} is not configured")
        if not image.startswith("ubuntu"):
            raise ApiError("Nodes need an Ubuntu image")
    stamp = datetime.now().strftime('%m%d%H%M')
    base = (name or "").strip().lower() or (
        f"{_clone_base({'from': (snap.get('created_from') or {}).get('name')})}-clone-{stamp}"
        if snap else f"{plan}-{location}-{stamp}".lower())
    _check_name(base, count)
    names = _server_names(base, count)
    existing = {s.get("name") for s in await api.list_servers()}
    taken = [n for n in names if n in existing]
    if taken:
        raise ApiError(f"Already taken: {', '.join(taken)}")
    if ipv4_id:
        try:
            ipv4_id = int(ipv4_id)
        except (TypeError, ValueError):
            raise ApiError("Invalid IP")
        if count != 1:
            raise ApiError("A kept IP goes on one server only")
        if not any(p.get("id") == ipv4_id for p in await ip_change.kept_ipv4s(api, location)):
            raise ApiError("That IP is not a free IPv4 in this location")
    j = _new_job("create", None, f"Create {str(count) + ' × ' if count > 1 else ''}{plan} in {location}")

    async def work():
        keys = [k["name"] for k in await api.list_ssh_keys() or []] or None
        results = []
        for i, n in enumerate(names, start=1):
            j["stage"] = f"Creating {i} of {len(names)}: {n}"
            key = node_setup.new_api_key() if panel else None
            result = await api.create_server(
                n, plan, image, location, keys, enable_ipv6=bool(ipv6),
                user_data=node_setup.cloud_init_script(key) if panel else None,
                primary_ipv4=ipv4_id or None)
            if result and result.get("server"):
                s = result["server"]
                if ipv4_id:
                    await ip_change.adopt(api, ipv4_id)
                if panel:
                    node_setup.add_job(account, s.get("id"), s.get("name"), _ip(s), panel,
                                       key, Config.ADMIN_ID)
                results.append({"name": n, "ok": True, "id": s.get("id"), "ip": _ip(s),
                                 "password": result.get("root_password")})
            else:
                results.append({"name": n, "ok": False, "error": api_error_text(result),
                                "capacity": is_capacity_refusal(result)})
            j["logs"].append(("✅ " if results[-1]["ok"] else "❌ ") + n)
        made = sum(1 for r in results if r["ok"])
        lines = []
        for r in results:
            if r["ok"]:
                lines.append(f"✅ `{r['name']}` — `{r['ip']}`"
                             + (f"\n   🔑 `{r['password']}`" if r.get("password") else ""))
            else:
                lines.append(f"❌ `{r['name']}`")
        j.update(ok=made == len(results), result={
            "servers": results, "keys": bool(keys), "panel": panel,
            "summary": (f"➕ *Servers created* (Mini App) — {made}/{len(results)}, "
                        f"`{plan}` in `{location}`\n\n" + "\n".join(lines)
                        + (f"\n\n🧩 Node setup started for panel `{panel}`." if panel and made else "")),
        })
    return job(_run(j, work(), notify)["id"])


async def create_alert(account, plan, locations, image, ipv6=False, name=None):
    _api, stype, _snap = await _validate_create(account, plan, image)
    locations = [l for l in (locations or []) if isinstance(l, str)]
    if not locations:
        raise ApiError("Pick at least one location")
    offered = [l for l in locations if not type_has_location_data(stype)
               or type_offered_at(stype, l)]
    if len(offered) != len(locations):
        raise ApiError("The plan is not sold in some of these locations")
    name = (name or "").strip().lower() or None
    if name:
        _check_name(name, 1)
    alert_id = create_watch.add_create_watch(account, plan, locations, image, bool(ipv6), name,
                                             Config.ADMIN_ID)
    return {"id": alert_id}


# ------------------------------------------------------------------ helpers for the rest

def _checked(result, what):
    """``result`` when Hetzner accepted the call, else ApiError with its message."""
    if result is None or (isinstance(result, dict) and result.get("error")):
        raise ApiError(f"{what}: {api_error_text(result)}", 502)
    return result


def _account_api(account):
    api = api_registry.get(account)
    if not api:
        raise ApiError("Unknown account")
    return api


def _stamp():
    return datetime.now().strftime("%y%m%d%H%M%S")


def _fip_out(f, pricing, names, account=None):
    loc = (f.get("home_location") or {}).get("name", "")
    country, flag = get_location_info(loc)
    owner = f.get("server")
    return {"id": f.get("id"), "ip": f.get("ip"), "type": f.get("type"), "name": f.get("name"),
            "account": account, "location": loc, "country": country, "flag": flag,
            "server_id": owner, "server_name": names.get(owner) if owner else None,
            "price": round(_floating_ip_price(pricing, f), 2),
            "command": floating_ip_command(f) if f.get("ip") else ""}


def _pip_out(p, pricing, names, account=None):
    loc = ((p.get("location") or {}).get("name")
           or ((p.get("datacenter") or {}).get("location") or {}).get("name", ""))
    country, flag = get_location_info(loc)
    owner = p.get("assignee_id")
    return {"id": p.get("id"), "ip": p.get("ip"), "type": p.get("type"), "name": p.get("name"),
            "account": account, "location": loc, "country": country, "flag": flag,
            "server_id": owner, "server_name": names.get(owner) if owner else None,
            # A primary IP on a server is included in the server's price.
            "price": 0 if owner else round(_primary_ip_price(pricing, p), 2)}


def _snap_out(i, per_gb, account=None, names=None):
    size = i.get("image_size") or 0
    source = i.get("created_from") or {}
    return {"id": i.get("id"), "account": account,
            "description": i.get("description") or i.get("name") or str(i.get("id")),
            "size": round(size, 2), "price": round(size * per_gb, 2), "status": i.get("status"),
            "disk": i.get("disk_size") or 0, "arch": i.get("architecture") or "x86",
            "protected": bool((i.get("protection") or {}).get("delete")),
            "created": i.get("created"), "server_id": source.get("id"),
            "server_name": source.get("name"),
            "server_exists": source.get("id") in (names or {})}


def _volume_price(pricing):
    volume = pricing.get("volume", {})
    return _gross(volume.get("price_per_gb_month", {}) or volume)


# ------------------------------------------------------------------ server: network

async def server_network(server_id):
    account, api, server = await _find(server_id)
    fips, volumes, pricing, servers, tags = await asyncio.gather(
        api.list_floating_ips(), api.list_volumes(), api.get_pricing(), api.list_servers(),
        _node_tags())
    names = {s.get("id"): s.get("name") for s in servers}
    loc = server_location(server)
    per_gb = _volume_price(pricing)
    vols = [{"id": v.get("id"), "name": v.get("name"), "size": v.get("size"),
             "device": v.get("linux_device"), "attached": v.get("server") == server_id,
             "price": round((v.get("size") or 0) * per_gb, 2)}
            for v in volumes
            if v.get("server") == server_id
            or (not v.get("server") and (v.get("location") or {}).get("name") == loc)]
    kept = await ip_change.kept_ipv4s(api, loc)
    return {"id": server_id, "name": server.get("name"), "account": account, "location": loc,
            "ip": _ip(server), "ipv6": _ipv6(server),
            "kept_ips": [_kept_out(p) for p in kept],
            "ip_price": round(_primary_ip_price(pricing, {"type": "ipv4",
                                                          "location": {"name": loc}}), 2),
            "busy": is_busy(server_id),
            "floating": [_fip_out(f, pricing, names) for f in fips],
            "volumes": vols, "volume_price": per_gb, "volume_sizes": VOLUME_SIZES,
            "panels": node_setup.panel_names() if node_setup.can_register() else [],
            "panel": tags.get(_ip(server))}


async def fip_action(server_id, fip_id, action):
    """Attach a floating IP of the server's account to it (or move it here), or detach it."""
    if action not in ("assign", "unassign"):
        raise ApiError("action must be assign or unassign")
    _account, api, server = await _find(server_id, fresh=False)
    fip = next((f for f in await api.list_floating_ips() if f.get("id") == fip_id), None)
    if not fip:
        raise ApiError("That floating IP is not in this server's account", 404)
    if action == "assign":
        _checked(await api.assign_floating_ip(fip_id, server_id, return_error=True), "Not attached")
    else:
        if fip.get("server") != server_id:
            raise ApiError("That IP is not attached to this server")
        _checked(await api.unassign_floating_ip(fip_id, return_error=True), "Not detached")
    return {"ok": True, "command": floating_ip_command(fip)}


async def _delete_fip(api, fip):
    """Delete a floating IP, detaching it first when it is on a server."""
    if fip.get("server"):
        _checked(await api.unassign_floating_ip(fip["id"], return_error=True), "Not detached")
    _checked(await api.delete_floating_ip(fip["id"], return_error=True), "Not deleted")


async def fip_delete(server_id, fip_id):
    """Delete one of the server's account's floating IPs (from the server's Network page)."""
    _account, api, _server = await _find(server_id, fresh=False)
    fip = next((f for f in await api.list_floating_ips() if f.get("id") == fip_id), None)
    if not fip:
        raise ApiError("That floating IP is not in this server's account", 404)
    await _delete_fip(api, fip)
    return {"ok": True}


async def fip_create_for(server_id, ip_type):
    """A new floating IP in the server's location, attached to it."""
    if ip_type not in ("ipv4", "ipv6"):
        raise ApiError("Invalid IP type")
    _account, api, server = await _find(server_id, fresh=False)
    loc = server_location(server)
    result = _checked(await api.create_floating_ip(ip_type, loc, f"fip-{_stamp()}-1",
                                                   return_error=True), "Not created")
    fip = result.get("floating_ip") or {}
    assigned = await api.assign_floating_ip(fip.get("id"), server_id, return_error=True)
    if assigned is None or (isinstance(assigned, dict) and assigned.get("error")):
        raise ApiError(f"{fip.get('ip')} was created but not attached: {api_error_text(assigned)}", 502)
    return {"ok": True, "ip": fip.get("ip"), "command": floating_ip_command(fip)}


async def volume_create(server_id, size):
    try:
        size = int(size)
    except (TypeError, ValueError):
        raise ApiError("Invalid size")
    low, high = VOLUME_SIZES
    if not low <= size <= high:
        raise ApiError(f"Size must be {low} to {high} GB")
    _account, api, _server = await _find(server_id, fresh=False)
    result = _checked(await api.create_volume(f"vol-{_stamp()}", size, server_id,
                                              return_error=True), "Volume not created")
    volume = result.get("volume") or {}
    return {"ok": True, "name": volume.get("name"), "device": volume.get("linux_device")}


async def volume_action(server_id, volume_id, action):
    """attach / detach / delete a volume of the server's account."""
    if action not in ("attach", "detach", "delete"):
        raise ApiError("Invalid action")
    _account, api, server = await _find(server_id, fresh=False)
    vol = next((v for v in await api.list_volumes() if v.get("id") == volume_id), None)
    if not vol:
        raise ApiError("Volume not found", 404)
    if action == "attach":
        if vol.get("server"):
            raise ApiError("That volume is attached to another server")
        _checked(await api.attach_volume(volume_id, server_id, return_error=True), "Not attached")
    elif action == "detach":
        _checked(await api.detach_volume(volume_id, return_error=True), "Not detached")
    else:
        if vol.get("server"):
            _checked(await api.detach_volume(volume_id, return_error=True), "Not detached")
            await asyncio.sleep(VOLUME_DETACH_WAIT)
        _checked(await api.delete_volume(volume_id, return_error=True), "Not deleted")
    return {"ok": True}


async def change_ipv4(server_id, keep_old=False, use_ip=None, notify=None):
    """🔁 Give the server another IPv4 (a job: the server is off for about a minute)."""
    _account, api, server = await _find(server_id)
    _busy_check(server_id)
    if not ip_change.current_ipv4(server):
        raise ApiError("The server has no public IPv4")
    if use_ip is not None:
        try:
            use_ip = int(use_ip)
        except (TypeError, ValueError):
            raise ApiError("Invalid IP")
    name = server.get("name")
    j = _new_job("ip", server_id, f"IPv4 change of {name}")

    async def log(line):
        j["logs"].append(line)
        j["stage"] = line

    async def work():
        r = await ip_change.change_ipv4(api, server_id, keep_old=bool(keep_old),
                                        use_ip=use_ip, log=log)
        text = (f"{r['old_ip']} → {r['new_ip']}" if r["ok"] else f"Not changed: {r['error']}")
        if r.get("warning"):
            text += f" ({r['warning']})"
        j.update(ok=r["ok"], result={
            "text": text, "old_ip": r["old_ip"], "new_ip": r["new_ip"], "kept": r["kept"],
            "summary": (f"🔁 *IPv4 change* (Mini App) — `{name}`\n\n"
                        + (f"`{r['old_ip']}` → `{r['new_ip']}`" if r["ok"]
                           else "❌ Not changed; the old IP stays.")
                        + ("\n📍 The old IP was kept." if r["kept"] else "")),
        })
    return job(_run(j, work(), notify)["id"])


async def set_protection(server_id, on):
    _account, api, _server = await _find(server_id, fresh=False)
    _checked(await api.change_server_protection(server_id, bool(on), return_error=True),
             "Protection not changed")
    return {"ok": True, "protected": bool(on)}


def _protected_check(server, what):
    if is_protected(server):
        raise ApiError(f"Protection is on: turn it off (Manage → Security) before you {what} "
                       f"this server", 409)


async def add_node(server_id, panel, api_key):
    """🧩 Add to Panel: register a server that already runs PasarGuard Node."""
    if not node_setup.can_register() or panel not in node_setup.panel_names():
        raise ApiError(f"Panel {panel} is not configured")
    api_key = (api_key or "").strip()
    if not node_setup.is_api_key(api_key):
        raise ApiError("The API key must be a UUID (setup.sh prints it at the end)")
    _account, _api, server = await _find(server_id, fresh=False)
    name, ip = server.get("name"), _ip(server)
    cert = await node_setup.fetch_node_cert(ip)
    if not cert:
        raise ApiError(f"PasarGuard Node does not answer on {ip}:{node_setup.NODE_PORT}; "
                       f"has setup.sh finished?", 502)
    ok, info, _retryable = await node_setup.register_node(panel, name, ip, cert, api_key)
    if not ok:
        raise ApiError(f"The panel refused: {info}", 502)
    return {"ok": True, "node_id": info,
            "summary": f"🧩 *Node added* (Mini App) — `{name}` to panel `{panel}`"}


# ------------------------------------------------------------------ server: manage

async def server_manage(server_id):
    account, api, server = await _find(server_id)
    images, snaps, pricing = await asyncio.gather(
        api.list_images("system"), api.list_images(), api.get_pricing())
    arch = (server.get("server_type") or {}).get("architecture")
    list_price = _server_type_price(server.get("server_type") or {}, server_location(server))
    override = get_override(account, server_id)
    try:
        backup_pct = float(pricing.get("server_backup", {}).get("percentage", 20) or 20)
    except (TypeError, ValueError):
        backup_pct = 20.0
    per_gb = _image_price_per_gb(pricing)
    names = {server_id: server.get("name")}
    return {"id": server_id, "name": server.get("name"), "account": account,
            "status": server.get("status"), "backups": bool(server.get("backup_window")),
            "backup_price": round(effective_price(account, server_id, list_price)
                                  * backup_pct / 100, 2),
            "list_price": round(list_price, 2), "price_override": override,
            "images": [{"name": i.get("name"), "description": i.get("description") or i.get("name")}
                       for i in sorted(images, key=lambda i: i.get("name") or "")
                       if i.get("architecture") == arch and not i.get("deprecated")],
            "snapshots": [_snap_out(i, per_gb, account, names) for i in snaps
                          if (i.get("created_from") or {}).get("id") == server_id],
            "busy": is_busy(server_id), "protected": is_protected(server),
            "ip": _ip(server), "location": server_location(server)}


async def rename(server_id, name):
    name = (name or "").strip()
    if not _RENAME_RE.match(name):
        raise ApiError("Name: 1-63 letters, digits, - or . (starting and ending with a letter or digit)")
    _account, api, server = await _find(server_id, fresh=False)
    if name == server.get("name"):
        raise ApiError("That is already the name")
    _checked(await api.rename_server(server_id, name), "Rename failed")
    return {"ok": True, "name": name}


async def set_price(server_id, price):
    account, _api, _server = await _find(server_id, fresh=False)
    if price is None or price == "":
        clear_override(account, server_id)
        return {"ok": True, "price_override": None}
    try:
        amount = float(str(price).replace(",", ".").lstrip("€").strip())
    except ValueError:
        raise ApiError("The price must be a number, like 4.51")
    if amount < 0 or amount > 100000:
        raise ApiError("Invalid price")
    set_override(account, server_id, amount)
    return {"ok": True, "price_override": amount}


async def set_backups(server_id, on):
    _account, api, server = await _find(server_id, fresh=False)
    if on:
        _checked(await api.enable_backup(server_id, return_error=True), "Backups not enabled")
    else:
        _checked(await api.disable_backup(server_id, return_error=True), "Backups not disabled")
    return {"ok": True, "backups": bool(on)}


async def reset_root_password(server_id):
    _account, api, server = await _find(server_id, fresh=False)
    result = _checked(await api.reset_password(server_id, return_error=True),
                      "Password reset failed (is the server running with qemu-guest-agent?)")
    password = result.get("root_password")
    if not password:
        raise ApiError("Hetzner returned no password (is qemu-guest-agent installed?)", 502)
    return {"password": password,
            "summary": f"🔑 *Root password reset* (Mini App) — `{server.get('name')}`\n`{password}`"}


async def rebuild(server_id, image):
    _account, api, server = await _find(server_id)
    _busy_check(server_id)
    _protected_check(server, "rebuild")
    arch = (server.get("server_type") or {}).get("architecture")
    images = await api.list_images("system")
    if not any(i.get("name") == image and i.get("architecture") == arch
               and not i.get("deprecated") for i in images):
        raise ApiError("That image is not for this server's architecture")
    result = _checked(await api.rebuild_server(server_id, image, return_error=True),
                      "Rebuild failed")
    password = result.get("root_password")
    name = server.get("name")
    return {"ok": True, "password": password,
            "summary": (f"🔧 *Rebuild started* (Mini App) — `{name}` with `{image}`"
                        + (f"\n🔑 `{password}`" if password else "\nSSH keys installed."))}


async def delete_server(server_id, confirm_name, keep_ip=False):
    _account, api, server = await _find(server_id)
    name = server.get("name")
    if (confirm_name or "").strip() != name:
        raise ApiError("Type the server's exact name to delete it")
    _busy_check(server_id)
    _protected_check(server, "delete")
    kept = None
    if keep_ip:
        kept, error = await ip_change.keep_ipv4(api, server)
        if error:
            raise ApiError(f"Not deleted: the IP could not be kept ({error})", 502)
    _checked(await api.delete_server(server_id), "Not deleted")
    forget_server(server_id)
    return {"ok": True, "kept_ip": kept,
            "summary": f"🗑 *Server deleted* (Mini App) — `{name}`"
                       + (f"\n📍 `{kept}` was kept as a free primary IP." if kept else "")}


async def snapshot_create(server_id):
    _account, api, server = await _find(server_id, fresh=False)
    description = f"{server.get('name', 'Server')} {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    _checked(await api.create_snapshot(server_id, description, return_error=True),
             "Snapshot not started")
    return {"ok": True, "description": description}


# ------------------------------------------------------------------ account-wide

async def _per_account(fetch):
    """[(account, api, result)] of ``fetch(api)`` for every account; failures skipped."""
    accounts = _accounts()
    results = await asyncio.gather(*(fetch(api) for _, api in accounts), return_exceptions=True)
    return [(name, api, r) for (name, api), r in zip(accounts, results)
            if not isinstance(r, BaseException)]


async def snapshots():
    async def fetch(api):
        return await asyncio.gather(api.list_images(), api.get_pricing(), api.list_servers())
    items, servers = [], []
    for account, _api, (images, pricing, srvs) in await _per_account(fetch):
        names = {s.get("id"): s.get("name") for s in srvs}
        per_gb = _image_price_per_gb(pricing)
        items += [_snap_out(i, per_gb, account, names) for i in images]
        servers += [{"id": s.get("id"), "name": s.get("name"), "account": account} for s in srvs]
    items.sort(key=lambda i: i.get("created") or "", reverse=True)
    return {"snapshots": items, "servers": servers, "accounts": api_registry.names()}


async def snapshot_delete(account, image_id):
    api = _account_api(account)
    img = await api.get_image(image_id)
    if not img:
        raise ApiError("Snapshot not found", 404)
    if (img.get("protection") or {}).get("delete"):
        _checked(await api.change_image_protection(image_id, False, return_error=True),
                 "Could not remove the delete protection")
    _checked(await api.delete_image(image_id, return_error=True), "Not deleted")
    return {"ok": True}


async def ips(kind):
    if kind not in ("fip", "pip"):
        raise ApiError("Unknown kind", 404)

    async def fetch(api):
        listing = api.list_floating_ips() if kind == "fip" else api.list_primary_ips()
        return await asyncio.gather(listing, api.get_pricing(), api.list_servers())
    out, servers = [], []
    for account, _api, (items, pricing, srvs) in await _per_account(fetch):
        names = {s.get("id"): s.get("name") for s in srvs}
        conv = _fip_out if kind == "fip" else _pip_out
        out += [conv(i, pricing, names, account) for i in items]
        servers += [{"id": s.get("id"), "name": s.get("name"), "account": account,
                     "location": server_location(s)} for s in srvs]
    api = api_registry.default()
    locations = await api.list_locations() if api else []
    return {"kind": kind, "ips": out, "servers": servers, "accounts": api_registry.names(),
            "locations": [{"name": l.get("name"), "city": l.get("city") or l.get("name"),
                           "flag": get_location_info(l.get("name"))[1]} for l in locations],
            "max_count": MAX_IPS}


async def ip_create(kind, account, ip_type, location, count):
    if kind not in ("fip", "pip"):
        raise ApiError("Unknown kind", 404)
    if ip_type not in ("ipv4", "ipv6"):
        raise ApiError("Invalid IP type")
    try:
        count = int(count)
    except (TypeError, ValueError):
        raise ApiError("Invalid count")
    if not 1 <= count <= MAX_IPS:
        raise ApiError(f"Count must be 1 to {MAX_IPS}")
    api = _account_api(account)
    if location not in {l.get("name") for l in await api.list_locations()}:
        raise ApiError("Unknown location")
    stamp, made, errors = _stamp(), [], []
    for i in range(count):
        name = f"{kind}-{stamp}-{i + 1}"
        if kind == "fip":
            result = await api.create_floating_ip(ip_type, location, name, return_error=True)
            item = (result or {}).get("floating_ip")
        else:
            result = await api.create_primary_ip(ip_type, location, name, return_error=True)
            item = (result or {}).get("primary_ip")
        if item:
            made.append({"ip": item.get("ip"),
                         "command": floating_ip_command(item) if kind == "fip" else ""})
        else:
            errors.append(api_error_text(result))
    if not made:
        raise ApiError(f"Not created: {errors[0] if errors else '?'}", 502)
    return {"ok": not errors, "made": made, "errors": errors}


async def ip_delete(kind, account, ip_id):
    api = _account_api(account)
    if kind == "fip":
        fip = next((f for f in await api.list_floating_ips() if f.get("id") == ip_id), None)
        if not fip:
            raise ApiError("Floating IP not found", 404)
        await _delete_fip(api, fip)
    elif kind == "pip":
        _checked(await api.delete_primary_ip(ip_id, return_error=True),
                 "Not deleted (a primary IP on a server cannot be deleted)")
    else:
        raise ApiError("Unknown kind", 404)
    return {"ok": True}


async def fip_assign(account, fip_id, server_id):
    """From the floating IP list: attach to ``server_id`` (same account) or detach (None)."""
    api = _account_api(account)
    fip = next((f for f in await api.list_floating_ips() if f.get("id") == fip_id), None)
    if not fip:
        raise ApiError("Floating IP not found", 404)
    if server_id is None:
        _checked(await api.unassign_floating_ip(fip_id, return_error=True), "Not detached")
        return {"ok": True}
    try:
        server_id = int(server_id)
    except (TypeError, ValueError):
        raise ApiError("Invalid server")
    if not any(s.get("id") == server_id for s in await api.list_servers()):
        raise ApiError("That server is not in the same account")
    _checked(await api.assign_floating_ip(fip_id, server_id, return_error=True), "Not attached")
    return {"ok": True, "command": floating_ip_command(fip)}


async def costs():
    """The cost report of every account (handlers.cost.collect_costs)."""
    out = []
    for account, api in _accounts():
        c = await collect_costs(api, account, get_overage_tracker(account))
        if c is None:
            out.append({"account": account, "empty": True})
            continue
        c["history"] = [[m, round(v, 2)] for m, v in (c["history"] or [])[:6]]
        for key, value in list(c.items()):
            if isinstance(value, float):
                c[key] = round(value, 2)
        for r in c["servers"]:
            r["price"], r["overage"] = round(r["price"], 2), round(r["overage"], 2)
        out.append(dict(c, account=account))
    return {"accounts": out,
            "total": round(sum(a.get("total", 0) for a in out), 2),
            "month": datetime.now().strftime("%Y-%m")}
