"""PasarGuard nodes: create a server that sets itself up, then add it to a panel.

Flow for "Create as node" (handlers/create_server.py):
  1. The bot picks a random API key and creates the server with a cloud-init
     script that runs the repo's ``setup.sh --auto --reboot`` with that key (optimizer
     included, then one reboot).
  2. A job is saved in ``node_setups.json``. Every minute ``check_node_setups``
     opens a TLS connection to the node port: once PasarGuard Node answers, the
     certificate it presents is the one the panel must trust.
  3. The bot logs into the panel and creates the node there (POST /api/node).

"Add to Panel" on an existing server (handlers/nodes.py) does steps 2-3 at once
with an API key the admin pastes. Panel logins live only in the bot's .env;
nodes never see them.
"""
import asyncio
import json
import logging
import secrets
import shlex
import ssl
import time
import uuid
from pathlib import Path

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import Config
from utils import escape_markdown, copy_button

logger = logging.getLogger(__name__)

JOBS_FILE = Path('node_setups.json')
CHECK_INTERVAL_MINUTES = 1
# setup.sh usually needs 5-10 minutes; give up (and say so) after this.
SETUP_TIMEOUT_MINUTES = 40

NODE_PORT = 62050
NODE_API_PORT = 62051
NODE_KEEP_ALIVE = 60


# ------------------------------------------------------------------ settings

def panel_names():
    return list(Config.PANELS)


def can_register():
    """True when "Add to Panel" can work: it needs only a panel login."""
    return bool(Config.PANELS)


def can_create_nodes():
    """True when "Create as node" can work: it also needs the setup.sh URL."""
    return bool(Config.PANELS and Config.NODE_SETUP_URL)


# Which panel each server address belongs to, for the server list's 🧩 tags.
# Read with one login + GET /api/nodes per panel, cached so a list view does
# not log in every time; a panel that fails is retried after a shorter wait.
NODES_CACHE_SECONDS = 600
NODES_RETRY_SECONDS = 120
_nodes_cache = {}   # panel -> (expires_at, {address, ...})


async def _fetch_node_addresses(panel_name):
    panel = Config.PANELS[panel_name]
    url = panel["url"]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
        async with session.post(f"{url}/api/admin/token", data={
            "username": panel["username"], "password": panel["password"],
        }) as resp:
            body = await _json(resp)
            token = body.get("access_token") if resp.status == 200 else None
        if not token:
            raise RuntimeError(f"login failed: HTTP {resp.status}")
        async with session.get(f"{url}/api/nodes", params={"limit": 1000},
                               headers={"Authorization": f"Bearer {token}"}) as resp:
            body = await _json(resp)
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
    return {n.get("address") for n in body.get("nodes") or [] if n.get("address")}


async def node_panels():
    """{address: panel name} of every node in the configured panels (cached)."""
    now = time.time()

    async def one(name):
        cached = _nodes_cache.get(name)
        if cached and cached[0] > now:
            return cached[1]
        try:
            found = await _fetch_node_addresses(name)
            _nodes_cache[name] = (now + NODES_CACHE_SECONDS, found)
        except Exception as e:
            logger.info("Could not list the nodes of panel %s: %s", name, e)
            found = cached[1] if cached else set()
            _nodes_cache[name] = (now + NODES_RETRY_SECONDS, found)
        return found

    names = panel_names()
    result = {}
    for name, addresses in zip(names, await asyncio.gather(*(one(n) for n in names))):
        for address in addresses:
            result.setdefault(address, name)
    return result


def new_api_key():
    return str(uuid.uuid4())


def is_api_key(text):
    try:
        uuid.UUID(text.strip())
        return True
    except (ValueError, AttributeError):
        return False


def cloud_init_script(api_key):
    """user_data for a new server: fetch setup.sh and run it unattended."""
    url = shlex.quote(Config.NODE_SETUP_URL)
    return (
        "#!/bin/bash\n"
        f"export NODE_API_KEY={shlex.quote(api_key)}\n"
        f"curl -fsSL --retry 5 --retry-delay 5 {url} -o /root/setup.sh"
        " && bash /root/setup.sh --auto --reboot\n"
    )


# ------------------------------------------------------------- node and panel

async def fetch_node_cert(ip, port=NODE_PORT, timeout=10):
    """PEM of the certificate the node presents, or None while it is not up.

    PasarGuard Node serves its self-signed certificate on the service port;
    that same certificate is what the panel needs as ``server_ca``.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_alpn_protocols(["h2"])
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port, ssl=ctx), timeout)
    except (OSError, asyncio.TimeoutError, ssl.SSLError):
        return None
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return ssl.DER_cert_to_PEM_cert(der) if der else None


async def _json(resp):
    try:
        return await resp.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError):
        return {}


def _detail(body):
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, (dict, list)):
            return json.dumps(detail)[:200]
        if detail:
            return str(detail)[:200]
    return ""


async def register_node(panel_name, name, address, server_ca, api_key):
    """Create the node in the panel → (ok, info, retryable).

    ``info`` is the new node id on success, else the reason. Network trouble
    and HTTP 5xx are retryable; a refusal (bad login, name taken...) is not.
    """
    panel = Config.PANELS.get(panel_name)
    if not panel:
        return False, f"panel '{panel_name}' is not configured", False
    url = panel["url"]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(f"{url}/api/admin/token", data={
                "username": panel["username"], "password": panel["password"],
            }) as resp:
                body = await _json(resp)
                if resp.status != 200 or not body.get("access_token"):
                    reason = _detail(body) or f"HTTP {resp.status}"
                    return False, f"panel login failed: {reason}", resp.status >= 500
            headers = {"Authorization": f"Bearer {body['access_token']}"}

            core_id = panel["core_id"]
            if core_id is None:
                async with session.get(f"{url}/api/cores/simple", headers=headers) as resp:
                    body = await _json(resp)
                    if resp.status != 200:
                        return False, f"reading core configs failed: HTTP {resp.status}", \
                            resp.status >= 500
                cores = [c for c in body.get("cores") or [] if isinstance(c.get("id"), int)]
                if not cores:
                    return False, "the panel has no core config", False
                core_id = min(c["id"] for c in cores)

            payload = {
                "name": name,
                "address": address,
                "port": NODE_PORT,
                "api_port": NODE_API_PORT,
                "usage_coefficient": 1,
                "connection_type": "grpc",
                "server_ca": server_ca,
                "keep_alive": NODE_KEEP_ALIVE,
                "core_config_id": core_id,
                "api_key": api_key,
            }
            async with session.post(f"{url}/api/node", json=payload, headers=headers) as resp:
                body = await _json(resp)
                if resp.status in (200, 201):
                    return True, str(body.get("id", "?")), False
                reason = _detail(body) or "no details"
                return False, f"HTTP {resp.status}: {reason}", resp.status >= 500
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return False, f"panel unreachable: {type(e).__name__}", True


# --------------------------------------------------------------------- jobs

def _load():
    if not JOBS_FILE.exists():
        return []
    try:
        data = json.loads(JOBS_FILE.read_text())
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"Could not read {JOBS_FILE}: {e}")
        return []


def _save(jobs):
    try:
        JOBS_FILE.write_text(json.dumps(jobs, indent=2))
    except Exception as e:
        logger.error(f"Could not write {JOBS_FILE}: {e}")


def list_jobs():
    return _load()


def add_job(account, server_id, name, ip, panel, api_key, chat_id):
    jobs = _load()
    job = {
        "id": secrets.token_hex(4),
        "account": account,
        "server_id": server_id,
        "name": name,
        "ip": ip,
        "panel": panel,
        "api_key": api_key,
        "chat_id": chat_id,
        "created_at": int(time.time()),
    }
    jobs.append(job)
    _save(jobs)
    return job["id"]


def _drop(job):
    _save([j for j in _load() if j.get("id") != job.get("id")])


def _put(job):
    jobs = [j for j in _load() if j.get("id") != job.get("id")]
    jobs.append(job)
    _save(jobs)


def remove_job(job_id):
    _save([j for j in _load() if j.get("id") != job_id])


def _server_button(job, api_key=None):
    rows = [[InlineKeyboardButton("🖥 Open Server", callback_data=f"server_{job['server_id']}")]]
    if api_key:
        rows.insert(0, [copy_button("📋 Copy API key", api_key)])
    return InlineKeyboardMarkup(rows)


async def _send(bot, job, text, api_key=None):
    try:
        await bot.send_message(chat_id=job["chat_id"], text=text, parse_mode="Markdown",
                               reply_markup=_server_button(job, api_key))
    except Exception as e:
        logger.error(f"Could not send node setup message: {e}")


def added_text(name, ip, panel, node_id):
    return (
        f"✅ *Node added to panel*\n\n"
        f"🖥 `{name}` — `{ip}`\n"
        f"🧩 Panel: `{panel}` (node id `{node_id}`)\n\n"
        f"Check its status in the panel; set inbounds/hosts there as usual."
    )


def failed_text(name, ip, panel, api_key, reason):
    return (
        f"❌ *Could not add the node to panel* `{panel}`\n\n"
        f"🖥 `{name}` — `{ip}`\n"
        f"Reason: {escape_markdown(reason)}\n\n"
        f"API key: `{api_key}`\n"
        f"Fix the cause, then use 🧩 Add to Panel on the server with this key."
    )


async def check_node_setups(bot):
    """Scheduled: register every node whose PasarGuard Node now answers."""
    now = time.time()
    for job in list_jobs():
        name, ip, panel = job.get("name"), job.get("ip"), job.get("panel")
        if now - job.get("created_at", now) > SETUP_TIMEOUT_MINUTES * 60:
            _drop(job)
            if job.get("last_error"):
                reason = (f"PasarGuard Node is up, but the panel kept failing: "
                          f"{escape_markdown(job['last_error'])}")
            else:
                reason = (f"PasarGuard Node did not answer on port {NODE_PORT}. On the "
                          f"server, see `/root/setup.log` and "
                          f"`/var/log/cloud-init-output.log`.")
            await _send(bot, job, (
                f"⌛ *Node setup timed out* after {SETUP_TIMEOUT_MINUTES} minutes\n\n"
                f"🖥 `{name}` — `{ip}`\n"
                f"{reason}\n\n"
                f"API key: `{job.get('api_key')}` (for 🧩 Add to Panel once it runs)"
            ), api_key=job.get("api_key"))
            continue
        cert = await fetch_node_cert(ip)
        if not cert:
            continue
        _drop(job)  # claim before the slow panel calls
        ok, info, retryable = await register_node(panel, name, ip, cert, job.get("api_key"))
        if ok:
            await _send(bot, job, added_text(name, ip, panel, info))
        elif retryable:
            _put(dict(job, last_error=info))
            logger.warning(f"Node {name}: panel {panel} not reachable yet ({info}); retrying")
        else:
            await _send(bot, job, failed_text(name, ip, panel, job.get("api_key"), info),
                        api_key=job.get("api_key"))
