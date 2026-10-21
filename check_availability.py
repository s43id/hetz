"""Verify the availability logic against the live Hetzner API.

Runs the bot's own functions — not a copy of them — so what this prints is
exactly what the bot decides.

    python3 check_availability.py              # ARM64 (cax) plans
    python3 check_availability.py cx           # Intel plans
    python3 check_availability.py "" 1234567   # every plan, plus Change Plan
                                               # for that server

The bot's dependencies live in ./venv, so this re-executes itself there
when started with a plain python3.

Sections:
  1. raw stock per plan, straight from server_types[].locations
  2. creation availability, as the create-server wizard computes it
  3. consistency checks that would indicate the fields are being misread
  4. optionally, what Change Plan offers for one server
"""
import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENV_DIR = os.path.join(_HERE, 'venv')
_VENV_PYTHON = os.path.join(_VENV_DIR, 'bin', 'python')
# The bot's dependencies (telegram, paramiko, aiohttp) live only in the venv,
# so a plain `python3 check_availability.py` dies on the import. Detect "am I
# inside that venv" via sys.prefix — comparing interpreter paths does not
# work, because venv/bin/python is a symlink to the very interpreter we would
# be comparing against.
if (os.path.exists(_VENV_PYTHON)
        and os.path.realpath(sys.prefix) != os.path.realpath(_VENV_DIR)):
    os.execv(_VENV_PYTHON, [_VENV_PYTHON, os.path.abspath(__file__), *sys.argv[1:]])

from hetzner_api import api_registry  # noqa: E402
from handlers.create_server import _availability_by_location  # noqa: E402
from handlers.resize import _resize_options  # noqa: E402
from utils import server_location, server_datacenter, type_family as _type_family  # noqa: E402


async def report(prefix, server_id=None):
    for account in api_registry.names():
        api = api_registry.get(account)
        types = await api.get_server_types()
        wanted = sorted(
            (t for t in types
             if t.get('name', '').lower().startswith(prefix)
             and not t.get('deprecation')),
            key=lambda t: t.get('name', ''),
        )

        print(f"\n{'=' * 60}\naccount: {account}")
        print(f"{len(types)} server types, {len(wanted)} matching '{prefix or '*'}'")
        if not wanted:
            continue

        # 1. what the API says, verbatim
        print("\n--- raw stock per plan (server_types[].locations) ---")
        for t in wanted:
            entries = t.get('locations') or []
            if not entries:
                print(f"  {t['name']:<8} <no locations array in the payload>")
                continue
            parts = [f"{e.get('name')}={'in stock' if e.get('available') else 'SOLD OUT'}"
                     for e in entries]
            print(f"  {t['name']:<8} {', '.join(parts)}")

        # 2. what the create wizard concludes
        available, supported = await _availability_by_location(api)
        print("\n--- creation: what the wizard offers ---")
        for t in wanted:
            in_stock = sorted(l for l, ids in available.items() if t['id'] in ids)
            offered = sorted(l for l, ids in supported.items() if t['id'] in ids)
            never = sorted(set(offered) - set(in_stock))
            if in_stock:
                print(f"    ✅ {t['name']:<8} orderable in: {', '.join(in_stock)}"
                      + (f"   (sold out in: {', '.join(never)})" if never else ""))
            elif offered:
                print(f"    ⛔ {t['name']:<8} sold out everywhere; offered in: "
                      f"{', '.join(offered)}")
            else:
                print(f"    🚫 {t['name']:<8} not offered in any location")

        # 3. sanity checks
        print("\n--- consistency checks ---")
        problems = []
        for t in wanted:
            if not t.get('locations'):
                problems.append(f"{t['name']}: no locations array — stock cannot be read")
        for loc, ids in supported.items():
            if not ids:
                problems.append(f"{loc}: offers no plan at all")
        print("    " + ("\n    ".join(problems) if problems
                        else "every plan reports its locations; stock is readable"))

        # 4. Change Plan for one server
        if server_id is not None:
            server = await api.get_server(server_id)
            if not server:
                print(f"\n--- server {server_id}: not in this account ---")
                continue
            cur = server.get('server_type', {})
            plans, offlist = await _resize_options(server, api)
            same = [t for t in plans
                    if _type_family(t.get('name', '')) == _type_family(cur.get('name', ''))]
            print(f"\n--- Change Plan for '{server.get('name')}' "
                  f"({cur.get('name')}) ---")
            print(f"    location={server_location(server) or '?'} "
                  f"datacenter={server_datacenter(server) or '(none in payload)'}")
            print(f"    {len(plans)} compatible plan(s); {len(same)} in the same family")
            for t in sorted(same, key=lambda t: (t.get('cores', 0), t.get('memory', 0))):
                mark = '🔓 off-list' if t['id'] in offlist else '   listed  '
                smaller = ' 💾 smaller disk' if t.get('disk', 0) < cur.get('disk', 0) else ''
                print(f"    {mark} {t['name']:<8} "
                      f"{t.get('cores')}C/{t.get('memory'):.0f}GB/{t.get('disk')}GB{smaller}")


if __name__ == '__main__':
    args = sys.argv[1:]
    prefix = (args[0] if args else 'cax').lower()
    sid = int(args[1]) if len(args) > 1 else None
    asyncio.run(report(prefix, sid))
