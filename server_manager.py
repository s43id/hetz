import asyncio
import logging
import time
from utils import (
    overage_cost, server_location, server_disk_gb, type_available_at,
    is_capacity_error, type_family,
)

logger = logging.getLogger(__name__)

# How many placeable candidates to try before giving up on the reset.
MAX_UPGRADE_ATTEMPTS = 4

# A server that cannot get back to its plan is moved to the closest plan to
# it instead of staying on the reset's bounce target; this many are tried.
SIMILAR_ATTEMPTS = 3

# The stages a reset reports to its progress callback, as (n, label) with
# n out of RESET_STEPS, so a UI can show one status line instead of the log.
RESET_STEPS = 6

# Retries for the trip back to the original plan. Kept short: each one
# is downtime, and a stranded server is better left running.
DOWNGRADE_ATTEMPTS = 3
DOWNGRADE_RETRY_SECONDS = 20

UPGRADE_MAP = {
    # CX Intel (old naming)
    'cx11': 'cx21',
    'cx21': 'cx31',
    'cx31': 'cx41',
    'cx41': 'cx51',
    # CX Intel (new naming)
    'cx22': 'cx32',
    'cx32': 'cx42',
    'cx42': 'cx52',
    # CX Intel (third generation)
    'cx23': 'cx33',
    'cx33': 'cx43',
    'cx43': 'cx53',
    # CPX AMD shared (old naming)
    'cpx11': 'cpx21',
    'cpx21': 'cpx31',
    'cpx31': 'cpx41',
    'cpx41': 'cpx51',
    # CPX AMD shared (new naming)
    'cpx12': 'cpx22',
    'cpx22': 'cpx32',
    'cpx32': 'cpx42',
    'cpx42': 'cpx52',
    # CAX ARM Ampere
    'cax11': 'cax21',
    'cax21': 'cax31',
    'cax31': 'cax41',
    # CCX Dedicated vCPU
    'ccx13': 'ccx23',
    'ccx23': 'ccx33',
    'ccx33': 'ccx43',
    'ccx43': 'ccx53',
    'ccx53': 'ccx63',
}


async def _migration_targets(server, api):
    """Plan names this server can be bounced through to reset its traffic.

    Hard requirements, because Hetzner refuses these outright: the
    architecture must match, the plan must not be deprecated, and its disk
    must be at least the server's current disk — Hetzner never shrinks one.

    Stock (``server_types[].locations[].available`` at the server's location)
    is only a preference: the flag under-reports, so plans flagged sold out
    are kept as later fallbacks rather than dropped. Ordering puts the least
    disruptive targets first, and the reset loop tries them in turn until one
    is accepted.
    """
    cur = server.get("server_type", {})
    cur_name = cur.get("name")
    cur_arch = cur.get("architecture")
    cur_cores = cur.get("cores", 0)
    cur_mem = cur.get("memory", 0)
    cur_family = type_family(cur_name)
    cur_disk = server_disk_gb(server)

    types = await api.get_server_types()
    location = server_location(server)
    # Stock now lives on each server type's locations array; treat it as a
    # preference, since the flag has proved incomplete.
    avail = {
        t.get("id") for t in types
        if location and type_available_at(t, location)
    }

    candidates = [
        t for t in types
        if t.get("architecture") == cur_arch
        and not t.get("deprecation")
        and t.get("name") != cur_name
        and (t.get("disk") or 0) >= cur_disk
    ]

    def rank(t):
        listed = t.get("id") in avail if avail else True
        same_family = type_family(t.get("name")) == cur_family
        bigger = (t.get("cores", 0), t.get("memory", 0)) >= (cur_cores, cur_mem)
        if same_family and bigger:
            group = 0   # ideal: an upgrade within the same family
        elif same_family:
            group = 1   # same family, smaller (still resets traffic)
        else:
            group = 2   # different family, last resort
        # Plans in stock here come first; the rest stay as fallbacks.
        return (0 if listed else 1, group,
                t.get("cores", 0), t.get("memory", 0), t.get("name", ""))

    candidates.sort(key=rank)
    names = [t.get("name") for t in candidates if t.get("name")]

    # Prefer the hand-tuned upgrade target first when it qualifies.
    preferred = UPGRADE_MAP.get(cur_name)
    if preferred in names:
        names.remove(preferred)
        names.insert(0, preferred)
    return names


# Servers with a traffic reset, plan change or IPv4 change in progress
# (manual, scheduled or automatic): a second one on the same server would
# fight the first over its power state and plan.
_busy = set()

# What runs on each busy server, for ⏳ Running now: {sid: {kind, title,
# stage, step, steps, started}} (memory only).
_activity = {}


def is_busy(server_id):
    return server_id in _busy


def claim(server_id, kind, title, steps=0):
    """Mark a server busy with ``kind`` work; False when something already runs."""
    if server_id in _busy:
        return False
    _busy.add(server_id)
    _activity[server_id] = {'server_id': server_id, 'kind': kind, 'title': title,
                            'stage': "Starting", 'step': 0, 'steps': steps,
                            'started': time.time()}
    return True


def release(server_id):
    _busy.discard(server_id)
    _activity.pop(server_id, None)


def note(server_id, stage, step=None):
    """Update the running work's stage line (and step) for ⏳ Running now."""
    entry = _activity.get(server_id)
    if entry:
        entry['stage'] = stage
        if step is not None:
            entry['step'] = step


def activity():
    """The bot's own running work, oldest first."""
    return sorted((dict(a) for a in _activity.values()), key=lambda a: a['started'])


def _plan_price(stype, location):
    prices = stype.get("prices") or []
    for p in prices:
        if p.get("location") == location:
            break
    else:
        p = prices[0] if prices else {}
    try:
        return float((p.get("price_monthly") or {}).get("gross") or 0)
    except (TypeError, ValueError):
        return 0.0


def _distance(stype, ref):
    """How far a plan's hardware is from ``ref`` (0 = same cores and RAM)."""
    cores, mem = ref.get("cores") or 1, ref.get("memory") or 1
    return (abs((stype.get("cores") or 0) - cores) / cores
            + abs((stype.get("memory") or 0) - mem) / mem)


def similar_plans(types, ref_name, current_name, disk, location, exclude=()):
    """Plans whose hardware is closer to ``ref_name`` than ``current_name`` is.

    For a server stranded on a reset's bounce target (usually the next size
    up): the stand-in it should wait on until its own plan has room again.
    Same architecture, not deprecated, disk big enough; ordered by in stock
    here, hardware distance, same family, then price.
    """
    by_name = {t.get("name"): t for t in types}
    ref, current = by_name.get(ref_name), by_name.get(current_name)
    if not ref:
        return []
    limit = _distance(current, ref) if current else float("inf")
    skip = {ref_name, current_name, *exclude}
    found = [
        t for t in types
        if t.get("name") not in skip
        and t.get("architecture") == ref.get("architecture")
        and not t.get("deprecation")
        and (t.get("disk") or 0) >= disk
        and _distance(t, ref) < limit
    ]
    found.sort(key=lambda t: (
        not type_available_at(t, location), _distance(t, ref),
        type_family(t.get("name")) != type_family(ref_name),
        _plan_price(t, location), t.get("name", "")))
    return [t.get("name") for t in found]


async def reset_server_traffic(server_id, progress_callback, api, tracker, home_plan=None):
    """Zero a server's traffic by moving it to another plan and back.

    ``progress_callback(logs, stage)`` is awaited after every log line and
    stage change; ``logs`` = [(emoji, text)], ``stage`` = (n, label), n of
    RESET_STEPS.

    ``home_plan``: the plan the server belongs on when an earlier reset left
    it stranded on another one. It is tried as the first hop, and if Hetzner
    accepts it the reset ends there: the plan change zeroed the counter and
    the server is home again.
    """
    if not claim(server_id, 'reset', "Traffic reset", RESET_STEPS):
        return False, [("⚠️", "A traffic reset or plan change is already running "
                              "on this server.")], None

    async def progress(logs, stage):
        note(server_id, stage[1], stage[0])
        if progress_callback:
            await progress_callback(logs, stage)

    try:
        return await _reset_server_traffic(server_id, progress, api, tracker, home_plan)
    finally:
        release(server_id)


async def _reset_server_traffic(server_id, progress_callback, api, tracker, home_plan):
    logs = []
    stage = (1, "Checking the server")

    async def report():
        if progress_callback:
            await progress_callback(logs, stage)

    async def add_log(emoji, message):
        logs.append((emoji, message))
        await report()

    async def step(n, label):
        nonlocal stage
        stage = (n, label)
        await report()

    async def ensure_running():
        """Bring the server back up if a failure left it powered off, so a
        failed reset never breaks SSH access."""
        try:
            srv = await api.get_server(server_id, fresh=True)
            if srv and srv.get('status') != 'running':
                await add_log("🟢", "Powering server back on (reset did not complete)...")
                await api.power_on(server_id)
                if await api.wait_for_status(server_id, "running", max_attempts=40):
                    await add_log("✅", "Server is now RUNNING")
                else:
                    await add_log("⚠️", "Power-on requested but status check timed out")
        except Exception as exc:
            await add_log("⚠️", f"Could not verify power state: {exc}")

    try:
        await add_log("📥", "Fetching server information...")
        server = await api.get_server(server_id)

        if not server:
            await add_log("❌", "Failed to fetch server information")
            return False, logs, None

        current_status = server.get('status')
        current_type = server.get('server_type', {}).get('name')

        if not current_type:
            await add_log("❌", "Could not determine current server type")
            return False, logs, None

        await add_log("💾", f"Current plan: {current_type}")
        disk, location = server_disk_gb(server), server_location(server)

        candidates = await _migration_targets(server, api)
        if home_plan and home_plan != current_type:
            candidates = [home_plan] + [c for c in candidates if c != home_plan]
            await add_log("↩️", f"Trying {home_plan} first: the server belongs on it")
        if not candidates:
            await add_log("❌", f"No plan is available for migration from {current_type} in this location")
            return False, logs, None

        await add_log("🔼", f"Reset via: {', '.join(candidates[:MAX_UPGRADE_ATTEMPTS])}")

        overage = overage_cost(server)
        if overage > 0:
            tracker.update_live_overage(server_id, overage)
            tracker.commit_overage(server_id)
            await add_log("💾", f"Overage cost €{overage:.2f} saved to cost history")

        if current_status == "running":
            await step(2, "Powering off")
            await add_log("🔴", "Shutting down server...")
            await api.power_off(server_id)

            if not await api.wait_for_status(server_id, "off", max_attempts=40):
                await add_log("❌", "Server failed to shutdown")
                return False, logs, None

            await add_log("✅", "Server is now OFF")
            await asyncio.sleep(2)

        # A plan flagged in stock can still fail placement, so try candidates
        # in order until Hetzner accepts one.
        upgrade_type = None
        last_msg = "Unknown error"
        for cand in candidates[:MAX_UPGRADE_ATTEMPTS]:
            await step(3, f"Moving to {cand}")
            await add_log("🔼", f"Migrating to {cand}...")
            result = await api.change_server_type(server_id, cand, upgrade_disk=False)
            if result and not result.get('error'):
                upgrade_type = cand
                break
            last_msg = (result or {}).get('error', {}).get('message', 'Unknown error')
            await add_log("⚠️", f"{cand} not placeable: {last_msg}")

        if not upgrade_type:
            await add_log("❌", f"No plan could be placed: {last_msg}")
            await ensure_running()
            return False, logs, None

        await asyncio.sleep(5)

        await add_log("⏳", "Waiting for upgrade to complete...")
        for i in range(30):
            server = await api.get_server(server_id, fresh=True)
            if server and server.get('server_type', {}).get('name') == upgrade_type:
                await add_log("✅", "Upgrade completed successfully")
                break
            await asyncio.sleep(5)
            if (i + 1) % 6 == 0:
                await add_log("⏳", f"Still upgrading... ({(i+1)*5}s elapsed)")

        await asyncio.sleep(3)

        await step(4, f"Starting on {upgrade_type}")
        await add_log("🟢", "Starting server...")
        await api.power_on(server_id)

        if not await api.wait_for_status(server_id, "running", max_attempts=40):
            await add_log("⚠️", "Server started but status check timed out")
        else:
            await add_log("✅", "Server is now RUNNING")

        if upgrade_type == home_plan:
            # The plan change already zeroed the counter, and this is where
            # the server belongs: no trip back.
            await add_log("🎉", f"Traffic reset, and the server is back on {home_plan}!")
            return True, logs, None

        await asyncio.sleep(5)

        await step(5, f"Moving back to {current_type}")
        await add_log("🔽", f"Downgrading back to {current_type}...")
        await api.power_off(server_id)

        if not await api.wait_for_status(server_id, "off", max_attempts=40):
            await add_log("❌", "Failed to shutdown for downgrade")
            await ensure_running()
            return False, logs, None

        await asyncio.sleep(2)

        # "error during placement" is usually transient capacity, so try a few
        # times — but not for long: every retry is downtime, and the server is
        # perfectly usable on the bigger plan while it waits for a slot.
        restored = False
        last_msg = "Unknown error"
        for attempt in range(1, DOWNGRADE_ATTEMPTS + 1):
            result = await api.change_server_type(server_id, current_type, upgrade_disk=False)
            if result and not result.get('error'):
                restored = True
                break
            last_msg = (result or {}).get('error', {}).get('message', 'Unknown error')
            if attempt < DOWNGRADE_ATTEMPTS:
                await add_log("⏳", f"{current_type} not placeable ({last_msg}) — "
                                    f"retry {attempt}/{DOWNGRADE_ATTEMPTS - 1} "
                                    f"in {DOWNGRADE_RETRY_SECONDS}s")
                await asyncio.sleep(DOWNGRADE_RETRY_SECONDS)

        if not restored:
            await add_log("❌", f"Could not return to {current_type}: {last_msg}")
            # Wait on the plan closest to the server's own (similar hardware)
            # rather than the bounce target, until its plan has room again.
            # The server is off already, so trying costs no extra downtime.
            reference = home_plan or current_type
            stand_in = upgrade_type
            try:
                types = await api.get_server_types()
                similar = similar_plans(types, reference, upgrade_type, disk, location,
                                        exclude={current_type})
            except Exception as exc:
                similar = []
                await add_log("⚠️", f"Could not look for a similar plan: {exc}")
            for cand in similar[:SIMILAR_ATTEMPTS]:
                await step(5, f"Moving to {cand} (closest to {reference})")
                await add_log("🔁", f"Moving to {cand} instead: the closest hardware to "
                                    f"{reference} with room")
                result = await api.change_server_type(server_id, cand, upgrade_disk=False)
                if result and not result.get('error'):
                    for _ in range(30):
                        await asyncio.sleep(5)
                        srv = await api.get_server(server_id, fresh=True)
                        if srv and srv.get('server_type', {}).get('name') == cand:
                            break
                    stand_in = cand
                    break
                msg = (result or {}).get('error', {}).get('message', 'Unknown error')
                await add_log("⚠️", f"{cand} not placeable: {msg}")
            await add_log("⚠️", f"Server is ON {stand_in}, not {current_type}. "
                                f"Traffic was reset, but the plan did not change back.")
            await ensure_running()
            return False, logs, current_type

        await asyncio.sleep(5)

        await add_log("⏳", "Waiting for downgrade to complete...")
        for i in range(30):
            server = await api.get_server(server_id, fresh=True)
            if server and server.get('server_type', {}).get('name') == current_type:
                await add_log("✅", "Downgrade completed successfully")
                break
            await asyncio.sleep(5)
            if (i + 1) % 6 == 0:
                await add_log("⏳", f"Still downgrading... ({(i+1)*5}s elapsed)")

        await asyncio.sleep(3)

        await step(6, "Starting the server")
        await add_log("🟢", "Starting server with original plan...")
        await api.power_on(server_id)

        if not await api.wait_for_status(server_id, "running", max_attempts=40):
            await add_log("⚠️", "Server started but status check timed out")
        else:
            await add_log("✅", "Server is now RUNNING")

        await add_log("🎉", "Traffic reset process completed!")
        return True, logs, None

    except Exception as e:
        logger.error(f"Error during traffic reset: {e}")
        await add_log("❌", f"Unexpected error: {str(e)}")
        await ensure_running()
        return False, logs, None


async def change_plan(api, server_id, plan, upgrade_disk=False, log=None,
                      attempts=3, retry_seconds=15):
    """Move a server to ``plan``: power off, switch, power back on.

    Shared by the manual Change Plan button and the automatic stock-alert
    switch, so both get the same guarantee: the server is never left off. A
    refused change powers it back up unchanged. Capacity refusals are retried
    a few times since they are usually momentary; other refusals (disk,
    architecture) are not, as they would only repeat.

    ``log`` receives plain-text progress lines. Returns a dict with
    ``ok``, ``error``, ``capacity`` (the refusal may clear on its own) and
    ``was_running``.
    """
    if not claim(server_id, 'plan', f"Plan change to {plan}"):
        return {'ok': False, 'capacity': False, 'was_running': False,
                'error': 'a traffic reset or plan change is already running on this server'}

    async def logged(line):
        note(server_id, line)
        if log:
            await log(line)

    try:
        return await _change_plan(api, server_id, plan, upgrade_disk, logged,
                                  attempts, retry_seconds)
    finally:
        release(server_id)


async def _change_plan(api, server_id, plan, upgrade_disk, log, attempts, retry_seconds):
    async def say(line):
        if log:
            await log(line)

    server = await api.get_server(server_id, fresh=True)
    if not server:
        return {'ok': False, 'error': 'server not found', 'capacity': False,
                'was_running': False}
    was_running = server.get('status') == 'running'

    if was_running:
        await say("🔴 Powering off server...")
        await api.power_off(server_id)
        if not await api.wait_for_status(server_id, 'off', max_attempts=40):
            await say("❌ Server failed to power off — making sure it stays running.")
            await api.power_on(server_id)
            return {'ok': False, 'error': 'server failed to power off',
                    'capacity': False, 'was_running': was_running}
        await say("✅ Server is OFF")

    await say(f"⚖️ Switching to {plan}{' (with disk upgrade)' if upgrade_disk else ''}...")
    msg = "Unknown error"
    accepted = False
    for attempt in range(1, attempts + 1):
        result = await api.change_server_type(server_id, plan, upgrade_disk=upgrade_disk)
        if result and not result.get('error'):
            accepted = True
            break
        msg = (result or {}).get('error', {}).get('message', 'Unknown error')
        if attempt < attempts and is_capacity_error(msg):
            await say(f"⏳ Not placeable ({msg}) — retry {attempt}/{attempts - 1} "
                      f"in {retry_seconds}s")
            await asyncio.sleep(retry_seconds)
        else:
            break

    if not accepted:
        await say(f"❌ Change failed: {msg}")
        if was_running:
            await say("🟢 Powering server back on (no changes made)...")
            await api.power_on(server_id)
            await api.wait_for_status(server_id, 'running', max_attempts=40)
            await say("✅ Server is RUNNING")
        return {'ok': False, 'error': msg, 'capacity': is_capacity_error(msg),
                'was_running': was_running}

    for _ in range(30):
        await asyncio.sleep(5)
        current = await api.get_server(server_id, fresh=True)
        if current and current.get('server_type', {}).get('name') == plan:
            await say("✅ Plan changed successfully")
            break

    if was_running:
        await say("🟢 Powering server back on...")
        await api.power_on(server_id)
        await api.wait_for_status(server_id, 'running', max_attempts=40)
        await say("✅ Server is RUNNING")
    return {'ok': True, 'error': None, 'capacity': False, 'was_running': was_running}
