"""Find where Hetzner reports per-location plan availability now.

/datacenters was removed on 2026-06-02, taking server_types.available with
it. This dumps the shapes that could carry the replacement so the bot can be
pointed at the right field.

    python3 probe_api.py            # uses the first account
    python3 probe_api.py cx43       # focus on one plan
"""
import asyncio
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENV_DIR = os.path.join(_HERE, 'venv')
_VENV_PYTHON = os.path.join(_VENV_DIR, 'bin', 'python')
if (os.path.exists(_VENV_PYTHON)
        and os.path.realpath(sys.prefix) != os.path.realpath(_VENV_DIR)):
    os.execv(_VENV_PYTHON, [_VENV_PYTHON, os.path.abspath(__file__), *sys.argv[1:]])

from hetzner_api import api_registry  # noqa: E402

# Endpoints that could plausibly carry availability now.
CANDIDATES = [
    '/locations',
    '/server_types',
    '/availability',
    '/server_types/availability',
    '/locations/availability',
    '/datacenters',
]


def show(label, obj, limit=2600):
    body = json.dumps(obj, indent=2, ensure_ascii=False)
    if len(body) > limit:
        body = body[:limit] + f"\n... [{len(body) - limit} more chars]"
    print(f"\n--- {label} ---\n{body}")


async def main(plan=None):
    account = api_registry.names()[0]
    api = api_registry.get(account)
    print(f"account: {account}")

    print("\n=== which endpoints still answer ===")
    for path in CANDIDATES:
        result = await api._request('GET', path, return_error=True, fresh=True)
        if result is None:
            state = 'no response'
        elif isinstance(result, dict) and result.get('error'):
            state = f"ERROR {result['error'].get('code')}"
        else:
            keys = sorted(k for k in result if k != 'meta')
            state = f"ok, keys={keys}"
        print(f"  {path:<28} {state}")

    # A whole server type, so any new availability field is visible.
    types = await api.get_server_types()
    chosen = next((t for t in types if t.get('name') == plan), None) or (types[0] if types else None)
    if chosen:
        print(f"\n=== server_type '{chosen.get('name')}' — every field ===")
        print("top-level keys:", sorted(chosen))
        show(f"server_type {chosen.get('name')}", chosen)

    # And a whole location, in case availability moved there.
    locations = await api.list_locations()
    if locations:
        print(f"\n=== location '{locations[0].get('name')}' — every field ===")
        print("top-level keys:", sorted(locations[0]))
        show(f"location {locations[0].get('name')}", locations[0])

    # One real server, to confirm what it says about where it lives.
    servers = await api.list_servers()
    if servers:
        s = servers[0]
        print(f"\n=== server '{s.get('name')}' placement fields ===")
        print("top-level keys:", sorted(s))
        show('location', s.get('location'))
        show('datacenter', s.get('datacenter'))


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
