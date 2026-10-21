"""The Mini App's web server: the page plus a JSON API, inside the bot process.

It runs on the bot's own event loop (aiohttp, started from main.py's
post_init), over HTTPS on ``Config.WEBAPP_PORT`` with the domain's certificate
(see `hetzner webapp`). Telegram only opens Mini Apps over HTTPS.

Auth: every /api call carries Telegram's ``initData`` in the
``X-Telegram-Init-Data`` header. It is checked with the bot token's HMAC
(secret = HMAC-SHA256("WebAppData", token)), must be at most
``MAX_AGE_SECONDS`` old and must belong to ``Config.ADMIN_ID``. The page
itself holds no data and is served to anyone.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import ssl
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import MenuButtonDefault, MenuButtonWebApp, WebAppInfo

import updater
import webapp_api as api
from config import Config

logger = logging.getLogger(__name__)

PAGE = Path(__file__).with_name("webapp") / "index.html"
MAX_AGE_SECONDS = 24 * 3600
MENU_BUTTON_TEXT = "Panel"

_runner = None
_bot = None


# ------------------------------------------------------------------ auth

def verify_init_data(init_data, token, max_age=MAX_AGE_SECONDS, now=None):
    """The Telegram user dict if ``init_data`` is genuine and fresh, else None."""
    if not init_data or not token:
        return None
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received = fields.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(fields.get("auth_date", "0"))
        user = json.loads(fields.get("user", "{}"))
    except ValueError:
        return None
    if (now or time.time()) - auth_date > max_age:
        return None
    return user if isinstance(user, dict) else None


@web.middleware
async def auth_and_errors(request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    user = verify_init_data(request.headers.get("X-Telegram-Init-Data", ""),
                            Config.TELEGRAM_TOKEN)
    if user is None:
        return web.json_response({"error": "Not signed in: open the Mini App from the bot"},
                                 status=401)
    if user.get("id") != Config.ADMIN_ID:
        return web.json_response({"error": "Not allowed"}, status=403)
    try:
        return await handler(request)
    except api.ApiError as e:
        return web.json_response({"error": str(e)}, status=e.status)
    except web.HTTPException:
        raise
    except Exception as e:
        logger.exception("Mini App API error on %s", request.path)
        return web.json_response({"error": f"Server error: {e}"}, status=500)


# ------------------------------------------------------------------ routes

async def _body(request):
    try:
        data = await request.json()
    except Exception:
        raise api.ApiError("The request body is not JSON")
    if not isinstance(data, dict):
        raise api.ApiError("The request body is not JSON")
    return data


def _sid(request):
    try:
        return int(request.match_info["sid"])
    except (KeyError, ValueError):
        raise api.ApiError("Invalid server id")


async def _notify(job):
    """Report a finished reset / plan change / create in the admin's chat."""
    summary = (job.get("result") or {}).get("summary")
    if _bot is None or not summary:
        return
    await _bot.send_message(chat_id=Config.ADMIN_ID, text=summary, parse_mode="Markdown")


async def page(request):
    if not PAGE.exists():
        return web.Response(status=404, text="page missing")
    return web.FileResponse(PAGE, headers={"Cache-Control": "no-cache"})


def _json(data):
    return web.json_response(data)


async def get_dashboard(request):
    return _json(await api.dashboard())


async def get_servers(request):
    return _json(await api.servers())


async def get_server(request):
    return _json(await api.server_detail(_sid(request)))


async def post_power(request):
    body = await _body(request)
    return _json(await api.power(_sid(request), body.get("action")))


async def post_reset(request):
    return _json(await api.reset(_sid(request), notify=_notify))


async def post_plan(request):
    body = await _body(request)
    return _json(await api.change(_sid(request), str(body.get("plan") or ""),
                                  bool(body.get("upgrade_disk")), notify=_notify))


async def post_watch(request):
    body = await _body(request)
    return _json(await api.set_watch(_sid(request), str(body.get("plan") or ""),
                                     bool(body.get("on", True))))


async def post_schedule(request):
    body = await _body(request)
    return _json(await api.add_schedule(_sid(request), body.get("repeat"), body.get("day"),
                                        body.get("hour"), body.get("minute")))


async def delete_schedule(request):
    return _json(api.delete_schedule(request.match_info["id"]))


async def get_alerts(request):
    return _json(await api.alerts())


async def delete_create_alert(request):
    return _json(api.delete_create_alert(request.match_info["id"]))


async def get_create_options(request):
    names = api.api_registry.names()
    account = request.query.get("account") or (names[0] if names else "")
    return _json(await api.create_options(account))


async def post_create(request):
    b = await _body(request)
    return _json(await api.create(
        str(b.get("account") or ""), str(b.get("plan") or ""), str(b.get("location") or ""),
        str(b.get("image") or ""), bool(b.get("ipv6")), b.get("count", 1),
        b.get("name") or None, b.get("panel") or None, notify=_notify,
        ipv4_id=b.get("ipv4_id") or None))


async def post_create_alert(request):
    b = await _body(request)
    return _json(await api.create_alert(
        str(b.get("account") or ""), str(b.get("plan") or ""), b.get("locations"),
        str(b.get("image") or ""), bool(b.get("ipv6")), b.get("name") or None))


async def get_version(request):
    return _json(await api.version())


_reports = set()


async def post_update(request):
    body = await _body(request)
    result = api.start_update(body.get("action"))
    if _bot is not None:
        # Reports in the chat if the script fails before restarting the bot;
        # otherwise the restarted bot reports (main.on_startup).
        task = asyncio.create_task(updater.report_when_done(_bot))
        _reports.add(task)
        task.add_done_callback(_reports.discard)
    return _json(result)


async def get_job(request):
    return _json(api.job(request.match_info["id"]))


async def _say(text):
    """Send a Markdown note to the admin's chat (passwords, deletes: kept in the chat too)."""
    if _bot is None or not text:
        return
    try:
        await _bot.send_message(chat_id=Config.ADMIN_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Could not send a Mini App note to Telegram: {e}")


async def _reply(coro):
    data = await coro
    await _say(data.pop("summary", None))
    return _json(data)


def _int(request, key):
    try:
        return int(request.match_info[key])
    except (KeyError, ValueError):
        raise api.ApiError("Invalid id")


async def get_network(request):
    return _json(await api.server_network(_sid(request)))


async def post_fip(request):
    b = await _body(request)
    try:
        fip_id = int(b.get("fip_id"))
    except (TypeError, ValueError):
        raise api.ApiError("Invalid IP id")
    return _json(await api.fip_action(_sid(request), fip_id, b.get("action")))


async def delete_fip(request):
    return _json(await api.fip_delete(_sid(request), _int(request, "fid")))


async def post_fip_new(request):
    b = await _body(request)
    return _json(await api.fip_create_for(_sid(request), b.get("type")))


async def post_volume(request):
    b = await _body(request)
    return _json(await api.volume_create(_sid(request), b.get("size")))


async def post_volume_action(request):
    b = await _body(request)
    return _json(await api.volume_action(_sid(request), _int(request, "vid"), b.get("action")))


async def delete_volume(request):
    return _json(await api.volume_action(_sid(request), _int(request, "vid"), "delete"))


async def post_node(request):
    b = await _body(request)
    return await _reply(api.add_node(_sid(request), str(b.get("panel") or ""), b.get("api_key")))


async def get_manage(request):
    return _json(await api.server_manage(_sid(request)))


async def post_rename(request):
    b = await _body(request)
    return _json(await api.rename(_sid(request), b.get("name")))


async def post_price(request):
    b = await _body(request)
    return _json(await api.set_price(_sid(request), b.get("price")))


async def post_backups(request):
    b = await _body(request)
    return _json(await api.set_backups(_sid(request), bool(b.get("on"))))


async def post_password(request):
    return await _reply(api.reset_root_password(_sid(request)))


async def post_rebuild(request):
    b = await _body(request)
    return await _reply(api.rebuild(_sid(request), str(b.get("image") or "")))


async def post_delete(request):
    b = await _body(request)
    return await _reply(api.delete_server(_sid(request), b.get("name"), bool(b.get("keep_ip"))))


async def post_protection(request):
    b = await _body(request)
    return _json(await api.set_protection(_sid(request), bool(b.get("on"))))


async def post_ipv4(request):
    b = await _body(request)
    return _json(await api.change_ipv4(_sid(request), bool(b.get("keep_old")), b.get("use_ip"),
                                       notify=_notify))


async def get_health(request):
    return _json(api.health_data())


async def post_automation(request):
    b = await _body(request)
    return _json(api.automation_set(b.get("pause_hours"), bool(b.get("resume"))))


async def post_report(request):
    b = await _body(request)
    return _json(api.report_set(**{k: b[k] for k in ("enabled", "hour", "minute") if k in b}))


async def post_report_send(request):
    return await _reply(api.report_now())


async def get_running(request):
    return _json(await api.running())


async def post_snapshot(request):
    return _json(await api.snapshot_create(_sid(request)))


async def get_snapshots(request):
    return _json(await api.snapshots())


async def delete_snapshot(request):
    return _json(await api.snapshot_delete(request.match_info["account"], _int(request, "id")))


async def get_ips(request):
    return _json(await api.ips(request.match_info["kind"]))


async def post_ips(request):
    b = await _body(request)
    return _json(await api.ip_create(request.match_info["kind"], str(b.get("account") or ""),
                                     b.get("type"), str(b.get("location") or ""), b.get("count", 1)))


async def delete_ip(request):
    return _json(await api.ip_delete(request.match_info["kind"], request.match_info["account"],
                                     _int(request, "id")))


async def post_fip_assign(request):
    b = await _body(request)
    return _json(await api.fip_assign(request.match_info["account"], _int(request, "id"),
                                      b.get("server_id")))


async def post_auto_reset(request):
    b = await _body(request)
    return _json(api.auto_reset_set(**{k: b[k] for k in ("enabled", "hour", "minute", "threshold")
                                       if k in b}))


async def post_server_auto_reset(request):
    b = await _body(request)
    return _json(await api.auto_reset_server(_sid(request), b.get("on"), b.get("hour"),
                                             b.get("minute"), bool(b.get("default_time"))))


async def get_cost(request):
    return _json(await api.costs())


def build_app():
    app = web.Application(middlewares=[auth_and_errors], client_max_size=64 * 1024)
    app.add_routes([
        web.get("/", page),
        web.get("/index.html", page),
        web.get("/api/dashboard", get_dashboard),
        web.get("/api/servers", get_servers),
        web.get(r"/api/server/{sid:\d+}", get_server),
        web.post(r"/api/server/{sid:\d+}/power", post_power),
        web.post(r"/api/server/{sid:\d+}/reset", post_reset),
        web.post(r"/api/server/{sid:\d+}/plan", post_plan),
        web.post(r"/api/server/{sid:\d+}/watch", post_watch),
        web.post(r"/api/server/{sid:\d+}/schedule", post_schedule),
        web.delete(r"/api/schedule/{id:[0-9a-f]+}", delete_schedule),
        web.get("/api/alerts", get_alerts),
        web.delete(r"/api/create-alert/{id:[0-9a-f]+}", delete_create_alert),
        web.get("/api/create/options", get_create_options),
        web.post("/api/create", post_create),
        web.post("/api/create/alert", post_create_alert),
        web.get(r"/api/job/{id:[0-9a-f]+}", get_job),
        web.get(r"/api/server/{sid:\d+}/network", get_network),
        web.post(r"/api/server/{sid:\d+}/fip", post_fip),
        web.post(r"/api/server/{sid:\d+}/fip/new", post_fip_new),
        web.delete(r"/api/server/{sid:\d+}/fip/{fid:\d+}", delete_fip),
        web.post(r"/api/server/{sid:\d+}/volume", post_volume),
        web.post(r"/api/server/{sid:\d+}/volume/{vid:\d+}", post_volume_action),
        web.delete(r"/api/server/{sid:\d+}/volume/{vid:\d+}", delete_volume),
        web.post(r"/api/server/{sid:\d+}/node", post_node),
        web.get(r"/api/server/{sid:\d+}/manage", get_manage),
        web.post(r"/api/server/{sid:\d+}/rename", post_rename),
        web.post(r"/api/server/{sid:\d+}/price", post_price),
        web.post(r"/api/server/{sid:\d+}/backups", post_backups),
        web.post(r"/api/server/{sid:\d+}/password", post_password),
        web.post(r"/api/server/{sid:\d+}/rebuild", post_rebuild),
        web.post(r"/api/server/{sid:\d+}/delete", post_delete),
        web.post(r"/api/server/{sid:\d+}/snapshot", post_snapshot),
        web.get("/api/snapshots", get_snapshots),
        web.delete(r"/api/snapshot/{account}/{id:\d+}", delete_snapshot),
        web.get(r"/api/ips/{kind:fip|pip}", get_ips),
        web.post(r"/api/ips/{kind:fip|pip}", post_ips),
        web.delete(r"/api/ips/{kind:fip|pip}/{account}/{id:\d+}", delete_ip),
        web.post(r"/api/ips/fip/{account}/{id:\d+}/assign", post_fip_assign),
        web.get("/api/cost", get_cost),
        web.post("/api/auto-reset", post_auto_reset),
        web.post(r"/api/server/{sid:\d+}/auto-reset", post_server_auto_reset),
        web.post(r"/api/server/{sid:\d+}/protection", post_protection),
        web.post(r"/api/server/{sid:\d+}/ipv4", post_ipv4),
        web.get("/api/health", get_health),
        web.post("/api/automation", post_automation),
        web.post("/api/health/report", post_report),
        web.post("/api/health/report/send", post_report_send),
        web.get("/api/running", get_running),
        web.get("/api/version", get_version),
        web.post("/api/update", post_update),
    ])
    return app


# ------------------------------------------------------------------ lifecycle

def _ssl_context():
    if not (os.path.isfile(Config.WEBAPP_CERT) and os.path.isfile(Config.WEBAPP_KEY)):
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(Config.WEBAPP_CERT, Config.WEBAPP_KEY)
    return ctx


async def start(bot):
    """Serve the Mini App if configured and point the admin's menu button at it."""
    global _runner, _bot
    _bot = bot
    url = Config.webapp_url()
    if not url:
        await _menu_button(bot, None)
        return None
    ctx = _ssl_context()
    if ctx is None:
        logger.warning("Mini App: certificate %s / %s not found; run `hetzner webapp %s`",
                       Config.WEBAPP_CERT, Config.WEBAPP_KEY, Config.WEBAPP_DOMAIN)
        return None
    _runner = web.AppRunner(build_app(), access_log=None)
    await _runner.setup()
    await web.TCPSite(_runner, "0.0.0.0", Config.WEBAPP_PORT, ssl_context=ctx).start()
    logger.warning("Mini App listening on %s", url)
    await _menu_button(bot, url)
    return url


async def _menu_button(bot, url):
    try:
        button = (MenuButtonWebApp(MENU_BUTTON_TEXT, WebAppInfo(url)) if url
                  else MenuButtonDefault())
        await bot.set_chat_menu_button(chat_id=Config.ADMIN_ID, menu_button=button)
    except Exception as e:
        logger.info(f"Could not set the menu button: {e}")


async def stop():
    global _runner
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
