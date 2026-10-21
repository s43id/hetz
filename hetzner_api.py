import aiohttp
import asyncio
import logging
import time
from config import Config

logger = logging.getLogger(__name__)

MIN_REQUEST_INTERVAL = 0.4
DEFAULT_GET_TTL = 10
LONG_TTL_PREFIXES = {
    '/pricing': 3600,
    '/locations': 3600,
    # Stock now lives on /server_types (locations[].available), so it
    # cannot be cached for long without the markers going stale.
    '/server_types': 120,
    '/images?type=system': 3600,
}
RATE_LIMIT_SOFT_FLOOR = 200


class HetznerAPI:
    def __init__(self, token: str):
        self.base_url = Config.HETZNER_API_BASE
        self.headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        self._throttle_lock = asyncio.Lock()
        self._last_request = 0.0
        self._cache = {}
        # Paths Hetzner has removed (HTTP 410). Remembered so we stop calling
        # them on every screen and stop filling the log with the same error.
        self._gone = set()
        # The rate limit as the last response reported it (health.py shows it).
        self.rate_limit = None
        self.rate_remaining = None

    def _cache_get(self, endpoint):
        hit = self._cache.get(endpoint)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        return None

    def _cache_set(self, endpoint, result):
        ttl = DEFAULT_GET_TTL
        for prefix, long_ttl in LONG_TTL_PREFIXES.items():
            if endpoint.startswith(prefix):
                ttl = long_ttl
                break
        self._cache[endpoint] = (time.monotonic() + ttl, result)

    async def _throttle(self):
        async with self._throttle_lock:
            wait = MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    async def _request(self, method, endpoint, data=None, retry=3, fresh=False, return_error=False):
        if method == 'GET' and not fresh:
            cached = self._cache_get(endpoint)
            if cached is not None:
                return cached

        path = endpoint.split('?')[0]
        if path in self._gone:
            return None

        url = f"{self.base_url}{endpoint}"
        for attempt in range(retry):
            await self._throttle()
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.request(
                        method, url, headers=self.headers, json=data,
                        timeout=aiohttp.ClientTimeout(total=30)
                    ) as response:
                        self._note_rate(response.headers)
                        if response.status == 429:
                            wait_time = min(2 ** attempt * 5, 60)
                            logger.warning(f"Rate limited. Waiting {wait_time}s...")
                            await asyncio.sleep(wait_time)
                            continue
                        try:
                            result = await response.json()
                        except Exception:
                            result = {}
                        if response.status == 410:
                            # The endpoint is gone for good; note it once and
                            # let callers degrade instead of retrying forever.
                            if path not in self._gone:
                                self._gone.add(path)
                                logger.warning(
                                    "Hetzner has removed %s (410) — features that "
                                    "rely on it are degraded: %s", path, result)
                            return result if return_error else None
                        if response.status >= 400:
                            logger.error(f"API Error {response.status}: {result}")
                            return result if return_error else None
                        result = result if result is not None else {}
                        if method == 'GET':
                            self._cache_set(endpoint, result)
                        else:
                            self._cache.clear()
                        remaining = response.headers.get('RateLimit-Remaining')
                        if remaining and int(float(remaining)) < RATE_LIMIT_SOFT_FLOOR:
                            logger.warning(f"Rate limit low ({remaining} left), slowing down...")
                            await asyncio.sleep(3)
                        return result
            except Exception as e:
                logger.error(f"Request failed (attempt {attempt + 1}): {e}")
                if attempt < retry - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return None
        return None

    def _note_rate(self, headers):
        try:
            if headers.get('RateLimit-Limit'):
                self.rate_limit = int(float(headers['RateLimit-Limit']))
            if headers.get('RateLimit-Remaining'):
                self.rate_remaining = int(float(headers['RateLimit-Remaining']))
        except (TypeError, ValueError):
            pass

    async def _probe(self, endpoint):
        """One uncached GET with no retry: (HTTP status, JSON body or None, error text).

        Status 0 means no answer at all (network error or timeout).
        """
        await self._throttle()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.base_url}{endpoint}", headers=self.headers,
                                       timeout=aiohttp.ClientTimeout(total=20)) as response:
                    self._note_rate(response.headers)
                    try:
                        body = await response.json()
                    except Exception:
                        body = None
                    return response.status, body, ''
        except Exception as e:
            return 0, None, str(e) or type(e).__name__

    async def check_access(self):
        """Can this account's token still use the API? (status, code, message).

        One uncached ``GET /servers?per_page=1``: 200 = fine, 401 = token
        rejected, 403 = forbidden (blocked / locked), 429 = rate limited,
        0 = no answer. ``code`` / ``message`` come from Hetzner's error body.
        """
        status, body, error = await self._probe('/servers?per_page=1')
        err = (body or {}).get('error') if isinstance(body, dict) else None
        if err:
            return status, err.get('code') or '', err.get('message') or ''
        return status, '', error

    async def list_server_actions(self, status=None, server_id=None, per_page=25):
        """The newest server actions (all servers, or one), uncached; None on error."""
        query = f"sort=id:desc&per_page={per_page}" + (f"&status={status}" if status else "")
        path = f"/servers/{server_id}/actions" if server_id else "/servers/actions"
        result = await self._request('GET', f"{path}?{query}", fresh=True)
        return result.get('actions', []) if result else None

    async def _list_all(self, path, key, per_page=50, fresh=False):
        """Fetch every page of a paginated collection.

        Hetzner returns only 25 items per page by default, so a single
        un-paginated GET silently truncates larger accounts (and even the
        full server-type catalogue).
        """
        items = []
        page = 1
        while True:
            sep = '&' if '?' in path else '?'
            result = await self._request(
                'GET', f'{path}{sep}per_page={per_page}&page={page}', fresh=fresh)
            if not result:
                break
            items.extend(result.get(key, []))
            pagination = (result.get('meta') or {}).get('pagination') or {}
            next_page = pagination.get('next_page')
            if not next_page or next_page == page:
                break
            page = next_page
        return items

    async def list_servers(self, fresh=False):
        return await self._list_all('/servers', 'servers', fresh=fresh)

    async def diagnose_servers(self):
        """Probe /servers (bypassing cache) and return a short reason string
        explaining why the list may be empty: an API error code, a network
        failure, or a genuinely empty account."""
        result = await self._request('GET', '/servers', fresh=True, return_error=True)
        if result is None:
            return "no response (network/timeout)"
        if isinstance(result, dict) and result.get('error'):
            err = result['error']
            return f"{err.get('message', 'error')} (code: {err.get('code', '?')})"
        count = len(result.get('servers', [])) if isinstance(result, dict) else 0
        return f"ok — {count} server(s)"

    async def get_server(self, server_id, fresh=False):
        result = await self._request('GET', f'/servers/{server_id}', fresh=fresh)
        return result.get('server') if result else None

    async def power_off(self, server_id):
        return await self._request('POST', f'/servers/{server_id}/actions/poweroff')

    async def power_on(self, server_id):
        return await self._request('POST', f'/servers/{server_id}/actions/poweron')

    async def change_server_type(self, server_id, server_type, upgrade_disk=False):
        return await self._request('POST', f'/servers/{server_id}/actions/change_type', {
            'server_type': server_type,
            'upgrade_disk': upgrade_disk
        }, return_error=True)

    async def get_server_types(self, fresh=False):
        return await self._list_all('/server_types', 'server_types', fresh=fresh)

    async def reset_password(self, server_id, return_error=False):
        return await self._request('POST', f'/servers/{server_id}/actions/reset_password',
                                   return_error=return_error)

    async def list_images(self, image_type='snapshot'):
        return await self._list_all(f'/images?type={image_type}', 'images')

    async def get_image(self, image_id):
        result = await self._request('GET', f'/images/{image_id}')
        return result.get('image') if result else None

    async def create_snapshot(self, server_id, description, return_error=False):
        return await self._request('POST', f'/servers/{server_id}/actions/create_image', {
            'type': 'snapshot',
            'description': description,
        }, return_error=return_error)

    async def delete_image(self, image_id, return_error=False):
        return await self._request('DELETE', f'/images/{image_id}', return_error=return_error)

    async def change_image_protection(self, image_id, delete_protect, return_error=False):
        return await self._request('POST', f'/images/{image_id}/actions/change_protection', {
            'delete': delete_protect,
        }, return_error=return_error)

    async def list_floating_ips(self):
        return await self._list_all('/floating_ips', 'floating_ips')

    async def create_floating_ip(self, ip_type, home_location, name, description=None,
                                 return_error=False):
        return await self._request('POST', '/floating_ips', {
            'type': ip_type,
            'home_location': home_location,
            'name': name,
            'description': description or name,
        }, return_error=return_error)

    async def delete_floating_ip(self, fip_id, return_error=False):
        return await self._request('DELETE', f'/floating_ips/{fip_id}', return_error=return_error)

    async def list_primary_ips(self):
        return await self._list_all('/primary_ips', 'primary_ips')

    async def create_primary_ip(self, ip_type, location, name, return_error=False,
                                auto_delete=False):
        # Primary IPs belong to a location now; `datacenter` went away with
        # the /datacenters endpoint.
        return await self._request('POST', '/primary_ips', {
            'type': ip_type,
            'location': location,
            'name': name,
            'assignee_type': 'server',
            'auto_delete': auto_delete,
        }, return_error=return_error)

    async def delete_primary_ip(self, pip_id, return_error=False):
        return await self._request('DELETE', f'/primary_ips/{pip_id}', return_error=return_error)

    async def get_primary_ip(self, pip_id, fresh=True):
        result = await self._request('GET', f'/primary_ips/{pip_id}', fresh=fresh)
        return result.get('primary_ip') if result else None

    async def update_primary_ip(self, pip_id, return_error=False, **fields):
        """PUT name / auto_delete / labels of a primary IP."""
        return await self._request('PUT', f'/primary_ips/{pip_id}', fields,
                                   return_error=return_error)

    async def assign_primary_ip(self, pip_id, server_id, return_error=False):
        """Put a primary IP on a server (the server must be powered off)."""
        return await self._request('POST', f'/primary_ips/{pip_id}/actions/assign', {
            'assignee_id': server_id,
            'assignee_type': 'server',
        }, return_error=return_error)

    async def unassign_primary_ip(self, pip_id, return_error=False):
        """Take a primary IP off its server (the server must be powered off)."""
        return await self._request('POST', f'/primary_ips/{pip_id}/actions/unassign',
                                   return_error=return_error)

    async def list_locations(self):
        return await self._list_all('/locations', 'locations')

    async def rebuild_server(self, server_id, image, return_error=False):
        return await self._request('POST', f'/servers/{server_id}/actions/rebuild', {
            'image': image,
        }, return_error=return_error)

    async def change_server_protection(self, server_id, on, return_error=False):
        """Delete + rebuild protection (Hetzner wants both the same)."""
        return await self._request('POST', f'/servers/{server_id}/actions/change_protection', {
            'delete': bool(on),
            'rebuild': bool(on),
        }, return_error=return_error)

    async def enable_backup(self, server_id, return_error=False):
        return await self._request('POST', f'/servers/{server_id}/actions/enable_backup',
                                   return_error=return_error)

    async def disable_backup(self, server_id, return_error=False):
        return await self._request('POST', f'/servers/{server_id}/actions/disable_backup',
                                   return_error=return_error)

    async def assign_floating_ip(self, fip_id, server_id, return_error=False):
        return await self._request('POST', f'/floating_ips/{fip_id}/actions/assign', {
            'server': server_id,
        }, return_error=return_error)

    async def unassign_floating_ip(self, fip_id, return_error=False):
        return await self._request('POST', f'/floating_ips/{fip_id}/actions/unassign',
                                   return_error=return_error)

    async def list_volumes(self):
        return await self._list_all('/volumes', 'volumes')

    async def create_volume(self, name, size, server_id, return_error=False):
        return await self._request('POST', '/volumes', {
            'name': name,
            'size': size,
            'server': server_id,
            'automount': True,
            'format': 'ext4',
        }, return_error=return_error)

    async def attach_volume(self, volume_id, server_id, return_error=False):
        return await self._request('POST', f'/volumes/{volume_id}/actions/attach', {
            'server': server_id,
            'automount': True,
        }, return_error=return_error)

    async def detach_volume(self, volume_id, return_error=False):
        return await self._request('POST', f'/volumes/{volume_id}/actions/detach',
                                   return_error=return_error)

    async def delete_volume(self, volume_id, return_error=False):
        return await self._request('DELETE', f'/volumes/{volume_id}', return_error=return_error)

    async def get_pricing(self):
        result = await self._request('GET', '/pricing')
        return result.get('pricing', {}) if result else {}

    async def list_ssh_keys(self):
        return await self._list_all('/ssh_keys', 'ssh_keys')

    async def create_server(self, name, server_type, image, location, ssh_keys=None, enable_ipv6=True,
                            user_data=None, primary_ipv4=None):
        """``image``: a system image name or a snapshot id (as text).
        ``primary_ipv4``: id of an unassigned primary IPv4 to use (a kept one)."""
        data = {
            'name': name,
            'server_type': server_type,
            'image': image,
            'location': location,
            'public_net': {
                'enable_ipv4': True,
                'enable_ipv6': enable_ipv6,
            },
        }
        if primary_ipv4:
            data['public_net']['ipv4'] = primary_ipv4
        if ssh_keys:
            data['ssh_keys'] = ssh_keys
        if user_data:
            data['user_data'] = user_data  # cloud-init, runs once at first boot
        return await self._request('POST', '/servers', data, return_error=True)

    async def delete_server(self, server_id):
        return await self._request('DELETE', f'/servers/{server_id}', return_error=True)

    async def rename_server(self, server_id, name):
        return await self._request('PUT', f'/servers/{server_id}', {'name': name}, return_error=True)

    async def wait_for_status(self, server_id, target_status, max_attempts=40):
        for i in range(max_attempts):
            server = await self.get_server(server_id, fresh=True)
            if server and server.get('status') == target_status:
                logger.info(f"Server {server_id} reached status: {target_status}")
                return True
            await asyncio.sleep(5)
        logger.warning(f"Server {server_id} did not reach {target_status} in time")
        return False


class HetznerAPIRegistry:
    """Holds one HetznerAPI instance per named account."""

    def __init__(self, accounts: dict):
        self._apis = {name: HetznerAPI(token) for name, token in accounts.items()}
        self._names = list(accounts.keys())

    def get(self, name: str) -> 'HetznerAPI | None':
        return self._apis.get(name)

    def names(self) -> list:
        return list(self._names)

    def default(self) -> 'HetznerAPI | None':
        return self._apis[self._names[0]] if self._names else None


api_registry = HetznerAPIRegistry(Config.HETZNER_ACCOUNTS)
