"""🔁 Change a server's IPv4, and 📍 keep an IPv4 when its server is deleted.

A server's public IPv4 is a **primary IP**. Changing it (the server must be
off for that):

1. get the new address first, while nothing is touched yet: a new primary IP
   in the server's location (``auto_delete`` on, like the one a new server
   gets), or a kept one the admin picked;
2. power off, take the old IP off the server, put the new one on;
3. power on, then delete the old IP, or keep it (unassigned, billed monthly,
   ready for another server).

The new IP is made before the old one is given up, so Hetzner cannot hand the
same address back. Any failure puts the old IP back and powers the server on
(a server is never left off), and a new IP made for nothing is deleted.

Keeping an IP when a server is deleted turns its ``auto_delete`` off first;
the create wizard (chat and Mini App) then offers it for a new server in the
same location.
"""
import asyncio
import logging
from datetime import datetime

from server_manager import claim, note, release
from utils import api_error_text, server_location

logger = logging.getLogger(__name__)

ASSIGN_WAIT_SECONDS = 2
ASSIGN_POLLS = 15


def _failed(result):
    return result is None or (isinstance(result, dict) and bool(result.get('error')))


def current_ipv4(server):
    """{id, ip} of the server's primary IPv4, or None."""
    ipv4 = ((server or {}).get('public_net') or {}).get('ipv4') or {}
    return {'id': ipv4.get('id'), 'ip': ipv4.get('ip')} if ipv4.get('ip') else None


def pip_location(pip):
    return ((pip.get('location') or {}).get('name')
            or ((pip.get('datacenter') or {}).get('location') or {}).get('name', ''))


async def kept_ipv4s(api, location=None):
    """Unassigned primary IPv4s of the account (in ``location`` when given)."""
    pips = await api.list_primary_ips() or []
    return [p for p in pips if p.get('type') == 'ipv4' and not p.get('assignee_id')
            and (location is None or pip_location(p) == location)]


async def _wait_assignee(api, pip_id, server_id):
    for _ in range(ASSIGN_POLLS):
        pip = await api.get_primary_ip(pip_id)
        if pip and pip.get('assignee_id') == server_id:
            return True
        await asyncio.sleep(ASSIGN_WAIT_SECONDS)
    return False


async def change_ipv4(api, server_id, keep_old=False, use_ip=None, log=None):
    """Give the server another IPv4. Returns {ok, error, old_ip, new_ip, kept, warning}.

    ``use_ip``: id of a kept (unassigned) primary IPv4 to use instead of a new one.
    """
    if not claim(server_id, 'ip', "IPv4 change"):
        return {'ok': False, 'error': 'a reset, plan change or IP change is already running '
                                      'on this server'}
    try:
        return await _change_ipv4(api, server_id, keep_old, use_ip, log)
    finally:
        release(server_id)


async def _change_ipv4(api, server_id, keep_old, use_ip, log):
    async def say(line):
        note(server_id, line)
        if log:
            await log(line)

    out = {'ok': False, 'error': None, 'old_ip': None, 'new_ip': None, 'kept': False,
           'warning': None}
    server = await api.get_server(server_id, fresh=True)
    if not server:
        out['error'] = 'server not found'
        return out
    old = current_ipv4(server)
    if not old or not old.get('id'):
        out['error'] = 'the server has no primary IPv4'
        return out
    out['old_ip'] = old['ip']
    location = server_location(server)

    created = False
    if use_ip:
        new = next((p for p in await kept_ipv4s(api, location) if p.get('id') == use_ip), None)
        if not new:
            out['error'] = 'that IP is not a free IPv4 in this server\'s location'
            return out
        await say(f"📍 Using the kept IP {new.get('ip')}")
    else:
        await say("🆕 Getting a new IPv4...")
        name = f"{server.get('name', server_id)}-{datetime.now().strftime('%y%m%d%H%M%S')}"[-63:]
        result = await api.create_primary_ip('ipv4', location, name.lstrip('-'),
                                             return_error=True, auto_delete=True)
        new = (result or {}).get('primary_ip') if not _failed(result) else None
        if not new:
            out['error'] = f"no new IP: {api_error_text(result)}"
            return out
        created = True
        await say(f"✅ New IP: {new.get('ip')}")

    async def drop_new():
        if created:
            await api.delete_primary_ip(new['id'])

    was_running = server.get('status') == 'running'
    if was_running:
        await say("🔴 Powering off...")
        await api.power_off(server_id)
        if not await api.wait_for_status(server_id, 'off', max_attempts=40):
            await say("❌ The server did not power off; it stays as it was.")
            await api.power_on(server_id)
            await drop_new()
            out['error'] = 'the server did not power off'
            return out

    async def back_on():
        if was_running:
            await say("🟢 Powering on...")
            await api.power_on(server_id)
            await api.wait_for_status(server_id, 'running', max_attempts=40)

    await say(f"🔌 Taking {old['ip']} off the server...")
    result = await api.unassign_primary_ip(old['id'], return_error=True)
    if _failed(result):
        await say(f"❌ Could not take the old IP off: {api_error_text(result)}")
        await back_on()
        await drop_new()
        out['error'] = f"the old IP stayed: {api_error_text(result)}"
        return out
    await asyncio.sleep(ASSIGN_WAIT_SECONDS)

    await say(f"🔗 Putting {new.get('ip')} on the server...")
    result = await api.assign_primary_ip(new['id'], server_id, return_error=True)
    if _failed(result) or not await _wait_assignee(api, new['id'], server_id):
        error = api_error_text(result) if _failed(result) else 'not assigned in time'
        await say(f"❌ Could not put the new IP on: {error}; putting the old one back")
        await api.assign_primary_ip(old['id'], server_id)
        await _wait_assignee(api, old['id'], server_id)
        await back_on()
        await drop_new()
        out['error'] = f"the new IP could not be assigned ({error}); the old IP is back"
        return out
    out['new_ip'] = new.get('ip')
    if not created:
        await adopt(api, new['id'])
    await back_on()

    if keep_old:
        await api.update_primary_ip(old['id'], auto_delete=False)
        out['kept'] = True
        await say(f"📍 Kept {old['ip']} as a free primary IP")
    else:
        result = await api.delete_primary_ip(old['id'], return_error=True)
        if _failed(result):
            out['warning'] = (f"{old['ip']} could not be deleted ({api_error_text(result)}) and "
                              f"is still billed; delete it in 📍 Primary IPs")
            await say(f"⚠️ {out['warning']}")
        else:
            await say(f"🗑 Deleted the old IP {old['ip']}")
    out['ok'] = True
    await say(f"🎉 The server's IPv4 is now {out['new_ip']}")
    return out


async def adopt(api, pip_id):
    """A kept IP now on a server goes with it again when that server is deleted."""
    await api.update_primary_ip(pip_id, auto_delete=True)


async def keep_ipv4(api, server):
    """Before deleting ``server``: keep its IPv4 (auto_delete off). Returns (ip, error)."""
    old = current_ipv4(server)
    if not old or not old.get('id'):
        return None, 'the server has no primary IPv4'
    result = await api.update_primary_ip(old['id'], return_error=True, auto_delete=False)
    if _failed(result):
        return None, api_error_text(result)
    return old['ip'], None
