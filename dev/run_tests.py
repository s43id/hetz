"""Offline tests for the Hetzner bot: real handlers, fake Hetzner, fake Telegram.

    venv/bin/python dev/run_tests.py     # on the server
    python3 dev/run_tests.py                               # anywhere with the deps

Needs only the packages in requirements.txt; no network, no tokens. It runs
in a temporary working directory, so the bot's JSON files (plan watches,
create watches, cost history...) start empty and the real ones are never
touched. Exit status 0 = every test passed.
"""
import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.dirname(HERE)
VENV_DIR = os.path.join(BOT_DIR, "venv")
VENV_PYTHON = os.path.join(VENV_DIR, "bin", "python")
# Same trick as check_availability.py: the deps live in ./venv on the server.
if (os.path.exists(VENV_PYTHON)
        and os.path.realpath(sys.prefix) != os.path.realpath(VENV_DIR)):
    os.execv(VENV_PYTHON, [VENV_PYTHON, os.path.abspath(__file__), *sys.argv[1:]])

ADMIN = 4242
# Set before config.py runs; load_dotenv() never overrides variables that are
# already set, so a real .env next to the bot cannot leak in.
os.environ.update({
    "TELEGRAM_TOKEN": "123:fake",
    "ADMIN_ID": str(ADMIN),
    "HETZNER_ACCOUNTS": "main:fake-main,second:fake-second",
    "DEBUG_MODE": "false",
    # One fake panel, so the node buttons show (tests point its URL at a local server).
    "NODE_SETUP_URL": "https://TOKEN@raw.example/setup.sh",
    "PANELS": "panel-b",
    "PANEL_PANEL_B_URL": "http://127.0.0.1:9/api",
    "PANEL_PANEL_B_USERNAME": "admin",
    "PANEL_PANEL_B_PASSWORD": "pw",
})
sys.path[:0] = [BOT_DIR, HERE]
WORK_DIR = tempfile.mkdtemp(prefix="hetzner-bot-test-")
atexit.register(shutil.rmtree, WORK_DIR, ignore_errors=True)
os.chdir(WORK_DIR)

# Every wait in the bot is asyncio.sleep (status polls, retry pauses): make
# them instant so a full traffic reset runs in milliseconds.
_real_sleep = asyncio.sleep


async def _instant_sleep(_delay, result=None):
    await _real_sleep(0)
    return result

asyncio.sleep = _instant_sleep

import warnings  # noqa: E402
from telegram.warnings import PTBUserWarning  # noqa: E402
warnings.filterwarnings("ignore", category=PTBUserWarning)  # as main.main() does

from hetzner_api import api_registry  # noqa: E402
from fake_hetzner import FakeHetzner, Chat, markdown_problem  # noqa: E402
import handlers  # noqa: E402,F401  (registers every route)
from handlers.router import router  # noqa: E402
import plan_watch  # noqa: E402
import create_watch  # noqa: E402
import node_setup  # noqa: E402
import reset_schedule  # noqa: E402
import auto_reset  # noqa: E402
import automation  # noqa: E402
import health  # noqa: E402
import server_manager  # noqa: E402
from server_manager import reset_server_traffic, change_plan  # noqa: E402
import overage_tracker  # noqa: E402
from overage_tracker import get_overage_tracker  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def fresh_world():
    """New fake accounts and empty state files for one test."""
    for f in ("plan_watches.json", "create_watches.json", "price_overrides.json",
              "monitor_state.json", "node_setups.json", "reset_schedules.json",
              "auto_reset.json", "automation.json", "health.json"):
        if os.path.exists(f):
            os.remove(f)
    for f in os.listdir("."):
        if f.startswith("overage_history"):
            os.remove(f)
    overage_tracker._tracker_registry.clear()
    node_setup._nodes_cache.clear()
    reset_schedule._last_logs.clear()
    import server_manager
    server_manager._busy.clear()
    server_manager._activity.clear()
    import webapp_api
    webapp_api._jobs.clear()
    main, second = FakeHetzner("main"), FakeHetzner("second")
    api_registry._apis["main"] = main
    api_registry._apis["second"] = second
    return main, second, Chat(ADMIN)


class Bot:
    """Background jobs get a bare bot; collect what they send."""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **_):
        if parse_mode == "Markdown":
            problem = markdown_problem(text)
            assert not problem, f"bad Markdown ({problem}):\n{text}"
        self.sent.append(text)


# ------------------------------------------------------------------- tests

@test
async def menu_and_unauthorized():
    main, _, chat = fresh_world()
    await chat.press("start_menu")
    assert "Hetzner Server Manager" in chat.last.text
    assert "watches" in chat.last.markup.data()
    q = await chat.press("start_menu", user_id=999)
    assert q.answers == [("⛔ Unauthorized", True)], q.answers


@test
async def server_list_and_detail_across_accounts():
    main, second, chat = fresh_world()
    a = main.add_server("alpha", "cx23", "fsn1", traffic_tb=5)
    b = second.add_server("beta", "cx33", "nbg1", nested_location=False)
    await chat.press("list_servers")
    data = chat.last.markup.data()
    assert f"server_{a}" in data and f"server_{b}" in data
    # Opening a server of the non-selected account switches to its owner.
    await chat.press(f"server_{b}")
    assert "beta" in chat.last.text and "Germany" in chat.last.text, chat.last.text
    assert chat.context.user_data["account"] == "second"
    await chat.press(f"server_{a}")
    assert "5.00/20 TB" in chat.last.text, chat.last.text


@test
async def every_button_on_reachable_screens_has_a_route():
    """Walk the menus breadth-first and check each button resolves somewhere."""
    main, _, chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1")
    # Entry points owned by ConversationHandlers in main.py, not the router.
    conv = ("console_", "rename_", "price_", "srvname_custom", "nodeaddp_")
    # Buttons whose handlers change things; pressing them is covered elsewhere.
    risky = ("resetgo_", "poweroff_", "poweron_", "deletego_", "resizego_", "srvgo",
             "rebuildgo_", "resetpw_confirm_", "backupgo_", "snapcreate_",
             "fipnewc_", "pipnewc_", "volnewc_", "srvalertgo", "pwadd_", "pwdel_",
             "srvnode_", "apause_", "prot_", "ipchgo_", "deletekeep_", "hltog")
    seen, queue = set(), ["start_menu", f"server_{sid}", "srvc"]
    while queue and len(seen) < 120:
        data = queue.pop(0)
        if data in seen:
            continue
        seen.add(data)
        if any(data.startswith(p) for p in conv):
            continue
        assert any(r.fullmatch(data) for r, _, _ in router._routes), f"no route for {data}"
        if any(data.startswith(p) for p in risky):
            continue
        await chat.press(data)
        for d in chat.last.markup.data():
            assert d and len(d.encode()) <= 64, f"callback_data too long: {d}"
            queue.append(d)
    assert len(seen) > 30, f"walk too short: {sorted(seen)}"


@test
async def change_plan_success_and_capacity_refusal():
    main, _, chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1")
    await chat.press(f"resizego_{sid}_cx33_0")
    assert main.plan_of(sid) == "cx33" and "Done" in chat.last.text
    main.refuse_change["cx43"] = None
    await chat.press(f"resizego_{sid}_cx43_0")
    assert main.plan_of(sid) == "cx33"
    assert main.servers[sid]["status"] == "running", "a refused change must power back on"
    assert "no capacity" in chat.last.text
    assert f"pwadd_{sid}_cx43" in chat.last.markup.data()


@test
async def smaller_disk_is_refused_before_power_off():
    main, _, chat = fresh_world()
    sid = main.add_server("alpha", "cx33", "fsn1")
    await chat.press(f"resizet_{sid}_cpx22")  # 80 GB == 80 GB: allowed
    assert "Confirm Plan Change" in chat.last.text
    main.servers[sid]["primary_disk_size"] = 160
    await chat.press(f"resizet_{sid}_cpx22")
    assert "Disk too small" in chat.last.text
    assert not any("poweroff" in c[1] for c in main.calls)


@test
async def traffic_reset_and_stranded_server():
    main, _, chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1", traffic_tb=21)
    await chat.press(f"server_{sid}")
    await chat.press(f"reset_{sid}")                      # asks first
    assert "Start the reset?" in chat.last.text and main.plan_of(sid) == "cx23"
    assert not any("poweroff" in c[1] for c in main.calls)
    await chat.press(chat.last.markup.find("Yes, reset"))
    assert "completed successfully" in chat.last.text, chat.last.text
    assert main.plan_of(sid) == "cx23" and main.servers[sid]["outgoing_traffic"] == 0
    # 1 TB over, at the location's price_per_tb_traffic (€1.19 gross).
    assert get_overage_tracker("main").get_server_month_overage(sid) == 1.19

    # The trip back is refused: the server waits, running, on the plan with
    # the closest hardware (cpx22: 2 cores / 4 GB like cx23, not the bounce
    # target cx33), and a watch is registered to move it back automatically.
    main.refuse_change["cx23"] = None
    await chat.press(f"resetgo_{sid}")
    assert main.plan_of(sid) == "cpx22" and main.servers[sid]["status"] == "running"
    assert "Still on* `cpx22`" in chat.last.text
    await chat.press(chat.last.markup.find("Details"))      # the full log
    assert "Moving to cpx22" in chat.last.text, chat.last.text
    assert [w["plan"] for w in plan_watch.list_watches()] == ["cx23"]

    # No similar plan has room either: it stays on the bounce target.
    plan_watch._save([])
    main.refuse_change["cpx22"] = None
    del main.refuse_change["cx23"]          # bring it home, then strand it again
    assert (await change_plan(main, sid, "cx23"))["ok"]
    main.refuse_change["cx23"] = None
    await chat.press(f"resetgo_{sid}")
    assert main.plan_of(sid) == "cx33" and main.servers[sid]["status"] == "running"
    await chat.press(f"resetlog_{sid}")
    assert "cpx22 not placeable" in chat.last.text
    assert [w["plan"] for w in plan_watch.list_watches()] == ["cx23"]


@test
async def plan_watch_auto_switch_and_backoff():
    main, _, _chat = fresh_world()
    bot = Bot()
    sid = main.add_server("alpha", "cx43", "fsn1")
    main.stock[("cx53", "fsn1")] = False
    plan_watch.add_watch("main", "fsn1-dc14", "cx53", sid, ADMIN)

    await plan_watch.check_plan_watches(bot)
    assert main.plan_of(sid) == "cx43" and not bot.sent  # sold out: nothing happens

    main.stock[("cx53", "fsn1")] = True
    main.refuse_change["cx53"] = None  # flag says in stock, migration refused
    for _ in range(plan_watch.AUTO_REFUSALS_BEFORE_NOTIFY):
        for w in plan_watch.list_watches():
            plan_watch._put(dict(w, retry_after=0))
        await plan_watch.check_plan_watches(bot)
    (w,) = plan_watch.list_watches()
    assert w["auto"] is False and "Auto-switch paused" in bot.sent[-1]
    assert main.servers[sid]["status"] == "running"

    plan_watch._put(dict(w, retry_after=0, auto=True))
    del main.refuse_change["cx53"]
    await plan_watch.check_plan_watches(bot)
    assert main.plan_of(sid) == "cx53" and "Switched automatically" in bot.sent[-1]
    assert plan_watch.list_watches() == []


@test
async def create_alert_wizard_stores_the_spec():
    main, _, chat = fresh_world()
    main.stock.update({("cx53", l): False for l in ("fsn1", "nbg1", "hel1")})
    await chat.press("srvven_intel")
    assert "⛔ cx53" in " ".join(t for t, _ in chat.last.markup.buttons)
    await chat.press("srvt_cx53")
    await chat.press(chat.last.markup.find("Create automatically"))
    await chat.press("srvaloc_nbg1")
    await chat.press("srvaloc_fsn1")
    assert "nbg1 → fsn1" in chat.last.text
    await chat.press("srvaok")
    await chat.press("srvi_ubuntu-24.04")
    await chat.press("srvipv6_0")          # alert mode skips the count step
    assert "Name" in chat.last.text
    await chat.press("srvname_auto")
    assert "Confirm Auto-Create Alert" in chat.last.text
    await chat.press("srvalertgo")
    (w,) = create_watch.list_create_watches()
    assert (w["plan"], w["locations"], w["image"], w["ipv6"], w["name"]) == \
        ("cx53", ["nbg1", "fsn1"], "ubuntu-24.04", False, None), w
    await chat.press("watches")
    assert f"cwx_{w['id']}" in chat.last.markup.data()


@test
async def create_watch_probe_order_and_lost_reply():
    main, _, _chat = fresh_world()
    bot = Bot()
    main.stock.update({("cx53", l): False for l in ("fsn1", "nbg1", "hel1")})
    create_watch.add_create_watch("main", "cx53", ["nbg1", "fsn1"], "ubuntu-24.04",
                                  False, None, ADMIN)
    # First check probes (no probe yet), both refuse: alert kept, backed off.
    main.refuse_create = {("cx53", "nbg1"), ("cx53", "fsn1")}
    await create_watch.check_create_watches(bot)
    (w,) = create_watch.list_create_watches()
    assert w["failures"] == 1 and w["last_probe"] > 0 and not main.servers

    # Stock flag shows fsn1; nbg1 still refuses → created in fsn1.
    main.stock[("cx53", "fsn1")] = True
    main.refuse_create = {("cx53", "nbg1")}
    create_watch._put(dict(w, retry_after=0))
    await create_watch.check_create_watches(bot)
    (server,) = main.servers.values()
    assert server["name"].startswith("cx53-fsn1-") and server["public_net"]["ipv6"] is None
    assert "Server created automatically" in bot.sent[-1]
    assert create_watch.list_create_watches() == []

    # A create whose reply is lost is found by name, not reported as failed.
    create_watch.add_create_watch("main", "cx53", ["fsn1"], "ubuntu-24.04", False, "web", ADMIN)
    main.lose_create_reply = True
    await create_watch.check_create_watches(bot)
    assert any(s["name"] == "web" for s in main.servers.values())
    assert "Server created automatically" in bot.sent[-1]


@test
async def rename_and_price_conversations():
    main, _, chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1")
    from handlers import _start_rename, recv_rename, _start_price, recv_price
    await _start_rename(chat.update_for_query(f"rename_{sid}"), chat.context)
    await recv_rename(chat.update_for_text("bravo"), chat.context)
    assert main.servers[sid]["name"] == "bravo"
    await _start_price(chat.update_for_query(f"price_{sid}"), chat.context)
    await recv_price(chat.update_for_text("2,50"), chat.context)
    await chat.press(f"server_{sid}")
    assert "€2.50/month" in chat.last.text, chat.last.text


@test
async def removed_endpoint_is_remembered():
    main, _, _chat = fresh_world()
    assert await main._request("GET", "/datacenters") is None
    assert "/datacenters" in main._gone
    assert await main._request("GET", "/datacenters?page=2") is None


@test
async def change_plan_never_leaves_server_off():
    main, _, _chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1")
    out = await change_plan(main, sid, "cax11")  # wrong architecture → refused
    assert not out["ok"] and not out["capacity"]
    assert main.servers[sid]["status"] == "running"
    ok, _logs, pending = await reset_server_traffic(sid, None, main,
                                                    get_overage_tracker("main"))
    assert ok and pending is None and main.plan_of(sid) == "cx23"


@test
async def primary_ips_are_created_per_location():
    main, _, chat = fresh_world()
    await chat.press("pip_new")
    await chat.press("pipnewt_ipv4")
    assert "pipnewl_ipv4_fsn1" in chat.last.markup.data(), chat.last.markup.data()
    await chat.press("pipnewl_ipv4_fsn1")
    await chat.press("pipnewc_ipv4_fsn1_1")
    assert "done (1/1)" in chat.last.text, chat.last.text
    await chat.press("pips")
    assert "10.9.0.1" in chat.last.text and "🇩🇪" in chat.last.text


@test
async def stale_prompt_buttons_and_leaving_a_prompt():
    main, _, chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1")
    from price_overrides import set_override, get_override
    set_override("main", sid, 3.0)
    for data in (f"rename_cancel_{sid}", f"price_cancel_{sid}", "console_cancel",
                 "auth_key", "srvname_cancel"):
        await chat.press(data)           # no "not wired up"
    await chat.press(f"price_clear_{sid}")
    assert get_override("main", sid) is None

    import main as entry
    from handlers import _start_rename
    from telegram.ext import ConversationHandler
    await entry._fresh_prompt(_start_rename)(chat.update_for_query(f"rename_{sid}"), chat.context)
    assert chat.context.user_data.get("rename_id") == sid
    state = await entry.leave_prompt(chat.update_for_query("start_menu"), chat.context)
    assert state == ConversationHandler.END and "rename_id" not in chat.context.user_data
    assert "Hetzner Server Manager" in chat.last.text
    conv = entry.build_prompt_conversation()
    assert conv.allow_reentry and conv.conversation_timeout == entry.PROMPT_TIMEOUT
    assert conv.fallbacks[-1].callback is entry.leave_prompt


@test
async def prompt_conversation_routes_real_updates():
    """Ask PTB's own ConversationHandler which callback each update reaches."""
    import datetime
    import main as entry
    from telegram import Update, CallbackQuery, Message, Chat as TgChat, User
    from handlers import recv_rename
    conv = entry.build_prompt_conversation()
    user = User(id=ADMIN, first_name="a", is_bot=False)
    tg_chat = TgChat(id=ADMIN, type="private")
    msg = Message(message_id=1, date=datetime.datetime.now(), chat=tg_chat, from_user=user)

    def press(data):
        return Update(1, callback_query=CallbackQuery("q", user, "ci", message=msg, data=data))

    def text(t):
        return Update(2, message=Message(message_id=2, date=datetime.datetime.now(),
                                         chat=tg_chat, from_user=user, text=t))

    def reached(update):
        found = conv.check_update(update)
        return found and found[2].callback

    conv._conversations[(ADMIN, ADMIN)] = entry.WAIT_RENAME
    assert reached(text("new-name")) is recv_rename
    assert reached(press("start_menu")) is entry.leave_prompt       # other button ends it
    assert reached(press("price_7")).__qualname__.startswith("_fresh_prompt")  # re-entry
    conv._conversations.pop((ADMIN, ADMIN))
    assert not reached(press("start_menu"))   # no prompt open: left to button_handler
    assert not reached(text("hello"))


@test
async def detail_shows_real_disk_and_overage_price():
    main, _, chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1", traffic_tb=22, disk=80)
    await chat.press(f"server_{sid}")
    assert "Disk: `80 GB` (plan 40 GB)" in chat.last.text, chat.last.text
    assert "`€2.38` (€1.19/TB)" in chat.last.text, chat.last.text
    us = main.add_server("usa", "cx23", "ash", traffic_tb=2, included_tb=1)
    await chat.press(f"server_{us}")
    assert "(€1.00/TB)" in chat.last.text   # no per-TB price → configured default


@test
async def account_wide_screens_name_and_switch_account():
    main, second, chat = fresh_world()
    main.add_server("alpha", "cx23", "fsn1")
    second.add_server("beta", "cx33", "nbg1")
    await chat.press("overage_cost")
    assert "Account: `main`" in chat.last.text and "alpha" in chat.last.text
    await chat.press(chat.last.markup.find("🔑 second"))
    assert "Account: `second`" in chat.last.text and "beta" in chat.last.text
    for screen in ("snapshots", "fips", "pips"):
        await chat.press(screen)
        assert "Account: `second`" in chat.last.text
        assert f"useacct_{screen}_main" in chat.last.markup.data()


@test
async def create_retry_only_makes_missing_servers():
    main, _, chat = fresh_world()
    await chat.press("srvc")
    await chat.press("srvt_cx23")
    await chat.press("srvl_fsn1")
    await chat.press("srvi_ubuntu-24.04")
    await chat.press("srvipv6_0")
    await chat.press("srvcount_2")
    chat.context.user_data["srv_name_base"] = "web"
    await chat.press("srvname_auto")
    chat.context.user_data["srv_name_base"] = "web"
    main.refuse_create = {("cx23", "fsn1")}
    orig = main._create_server
    made = []

    def first_only(data):            # web-1 succeeds, web-2 is refused
        if not made:
            main.refuse_create = set()
        made.append(data["name"])
        result = orig(data)
        main.refuse_create = {("cx23", "fsn1")}
        return result
    main._create_server = first_only
    await chat.press("srvgo")
    assert "(1/2)" in chat.last.text and "🔄 Retry" in " ".join(t for t, _ in chat.last.markup.buttons)
    main.refuse_create = set()
    main._create_server = orig
    await chat.press("srvgo")
    assert "(2/2)" in chat.last.text and "created earlier" in chat.last.text, chat.last.text
    assert sorted(s["name"] for s in main.servers.values()) == ["web-1", "web-2"]


@test
async def plan_list_prices_and_monitor_limits():
    main, _, chat = fresh_world()
    await chat.press("srvven_intel")
    labels = " ".join(t for t, _ in chat.last.markup.buttons)
    assert "€3.49/mo" in labels, labels
    main.add_server("usa", "cx23", "ash", traffic_tb=0.9, included_tb=1)
    import monitor
    bot = Bot()
    await monitor.traffic_monitor(bot)
    # The alert shows this server's own 1 TB allowance, not the 20 TB default.
    assert any("0.90/1 TB" in t and "75%" in t for t in bot.sent), bot.sent


@test
async def lost_create_reply_without_keys_points_to_reset_password():
    main, _, _chat = fresh_world()
    bot = Bot()
    create_watch.add_create_watch("main", "cx23", ["fsn1"], "ubuntu-24.04", False, "solo", ADMIN)
    main.lose_create_reply = True
    await create_watch.check_create_watches(bot)
    assert "Reset Password" in bot.sent[-1], bot.sent[-1]


@test
async def main_module_wires_up():
    import main  # noqa: F401  (imports every handler the Application registers)
    assert markdown_problem("*bold* `a_b` x\\_y") is None
    assert markdown_problem("error resource_unavailable") is not None


FAKE_PEM = "-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"


@test
async def create_as_node_registers_it_once_the_node_answers():
    main, _, chat = fresh_world()
    assert node_setup.can_create_nodes() and node_setup.panel_names() == ["panel-b"]
    await chat.press("srvt_cx23")
    await chat.press("srvl_fsn1")
    await chat.press("srvi_ubuntu-24.04")
    await chat.press("srvipv6_0")
    await chat.press("srvcount_1")
    chat.context.user_data["srv_name_base"] = "node7"
    await chat.press("srvname_auto")
    chat.context.user_data["srv_name_base"] = "node7"
    assert "srvnode_panel-b" in chat.last.markup.data(), chat.last.markup.data()
    await chat.press("srvnode_panel-b")
    assert "Node setup started" in chat.last.text, chat.last.text
    (sid,) = [s for s, v in main.servers.items() if v["name"] == "node7"]
    (job,) = node_setup.list_jobs()
    assert (job["server_id"], job["panel"], job["name"]) == (sid, "panel-b", "node7")
    script = main.user_data[sid]
    assert f"NODE_API_KEY={job['api_key']}" in script and "setup.sh --auto --reboot" in script
    assert " https://TOKEN@raw.example/setup.sh -o /root/setup.sh " in script, script

    calls = []
    orig_fetch, orig_register = node_setup.fetch_node_cert, node_setup.register_node

    async def fetch(ip, *a, **k):
        return cert_now

    async def register(*args):
        calls.append(args)
        return answer

    node_setup.fetch_node_cert, node_setup.register_node = fetch, register
    try:
        bot = Bot()
        cert_now = None                                   # still installing
        await node_setup.check_node_setups(bot)
        assert not bot.sent and len(node_setup.list_jobs()) == 1
        cert_now, answer = FAKE_PEM, (False, "panel unreachable: ClientError", True)
        await node_setup.check_node_setups(bot)           # panel down: keep trying
        (job2,) = node_setup.list_jobs()
        assert job2["last_error"].startswith("panel unreachable") and not bot.sent
        answer = (True, "12", False)
        await node_setup.check_node_setups(bot)
        assert "Node added to panel" in bot.sent[-1] and "`12`" in bot.sent[-1]
        assert node_setup.list_jobs() == []
        assert calls[-1] == ("panel-b", "node7", job["ip"], FAKE_PEM, job["api_key"])

        # Never answering: reported once after the timeout, with the API key.
        node_setup.add_job("main", sid, "node8", "10.0.0.8", "panel-b", "k-1", ADMIN)
        node_setup._put(dict(node_setup.list_jobs()[0], created_at=0))
        cert_now = None
        await node_setup.check_node_setups(bot)
        assert "timed out" in bot.sent[-1] and "k-1" in bot.sent[-1]
        assert node_setup.list_jobs() == []
    finally:
        node_setup.fetch_node_cert, node_setup.register_node = orig_fetch, orig_register


@test
async def node_buttons_hide_without_panels_or_for_other_images():
    main, _, chat = fresh_world()
    from config import Config
    sid = main.add_server("alpha", "cx23", "fsn1")
    await chat.press(f"srvnet_{sid}")
    assert f"nodeadd_{sid}" in chat.last.markup.data()
    saved = dict(Config.PANELS)
    Config.PANELS.clear()
    try:
        await chat.press(f"srvnet_{sid}")
        assert f"nodeadd_{sid}" not in chat.last.markup.data()
    finally:
        Config.PANELS.update(saved)
    chat.context.user_data.update(srv_type="cx23", srv_loc="fsn1", srv_img="debian-12",
                                  srv_ipv6=False, srv_count=1, srv_name_base="deb")
    await chat.press("srvname_auto")
    assert not any(d.startswith("srvnode_") for d in chat.last.markup.data())


@test
async def add_to_panel_prompt_registers_an_existing_server():
    main, _, chat = fresh_world()
    sid = main.add_server("old-node", "cx23", "fsn1")
    from handlers import _start_node_key, recv_node_key
    await chat.press(f"nodeadd_{sid}")
    assert f"nodeaddp_{sid}_panel-b" in chat.last.markup.data()
    state = await _start_node_key(chat.update_for_query(f"nodeaddp_{sid}_panel-b"), chat.context)
    assert state == handlers.WAIT_NODE_KEY and "API key" in chat.last.text
    assert await recv_node_key(chat.update_for_text("not-a-key"), chat.context) == state
    assert "not a UUID" in chat.last.text

    orig_fetch, orig_register = node_setup.fetch_node_cert, node_setup.register_node
    seen = []

    async def fetch(ip, *a, **k):
        return FAKE_PEM

    async def register(*args):
        seen.append(args)
        return False, "HTTP 409: Node already exists", False

    node_setup.fetch_node_cert, node_setup.register_node = fetch, register
    try:
        key = node_setup.new_api_key()
        await recv_node_key(chat.update_for_text(key), chat.context)
        assert "Could not add" in chat.last.text and key in chat.last.text, chat.last.text
        assert seen == [("panel-b", "old-node", main.servers[sid]["public_net"]["ipv4"]["ip"],
                         FAKE_PEM, key)]
        assert "node_key" not in chat.context.user_data
    finally:
        node_setup.fetch_node_cert, node_setup.register_node = orig_fetch, orig_register
    await chat.press(f"nodekey_cancel_{sid}")         # Cancel after the prompt ended
    assert "old-node" in chat.last.text


@test
async def register_node_speaks_the_panel_api():
    """Against a local HTTP server shaped like the PasarGuard API."""
    from aiohttp import web
    from config import Config
    got = {}

    async def token(request):
        form = await request.post()
        if (form.get("username"), form.get("password")) != ("admin", "pw"):
            return web.json_response({"detail": "Incorrect username or password"}, status=401)
        return web.json_response({"access_token": "T", "token_type": "bearer"})

    async def cores(request):
        assert request.headers["Authorization"] == "Bearer T"
        return web.json_response({"cores": [{"id": 3, "name": "b"}, {"id": 2, "name": "a"}],
                                  "total": 2})

    async def node(request):
        body = await request.json()
        got.update(body)
        if body["name"] == "taken":
            return web.json_response({"detail": "Node already exists"}, status=409)
        return web.json_response(dict(body, id=41), status=201)

    app = web.Application()
    app.add_routes([web.post("/api/admin/token", token), web.get("/api/cores/simple", cores),
                    web.post("/api/node", node)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    panel = Config.PANELS["panel-b"]
    saved = dict(panel)
    try:
        panel["url"] = f"http://127.0.0.1:{port}"
        ok, info, _ = await node_setup.register_node("panel-b", "n1", "1.2.3.4", FAKE_PEM, "key-1")
        assert (ok, info) == (True, "41"), info
        assert got == {"name": "n1", "address": "1.2.3.4", "port": 62050, "api_port": 62051,
                       "usage_coefficient": 1, "connection_type": "grpc", "server_ca": FAKE_PEM,
                       "keep_alive": 60, "core_config_id": 2, "api_key": "key-1"}, got
        ok, info, retry = await node_setup.register_node("panel-b", "taken", "1.2.3.4", FAKE_PEM, "k")
        assert not ok and not retry and "409" in info and "already exists" in info
        panel["password"] = "wrong"
        ok, info, retry = await node_setup.register_node("panel-b", "n2", "1.2.3.4", FAKE_PEM, "k")
        assert not ok and not retry and "Incorrect" in info, info
        panel.update(saved, url=f"http://127.0.0.1:{port}", core_id=7)
        await node_setup.register_node("panel-b", "n3", "1.2.3.4", FAKE_PEM, "k")
        assert got["core_config_id"] == 7
    finally:
        await runner.cleanup()
        panel.clear()
        panel.update(saved)
    ok, info, retry = await node_setup.register_node("panel-b", "n4", "1.2.3.4", FAKE_PEM, "k")
    assert not ok and retry and "unreachable" in info, info   # nothing listens on :9


@test
async def panel_names_with_a_dash():
    from config import Config
    env = {"PANELS": "panel-a,my-panel,Bad_Name",
           "PANEL_PANEL_A_URL": "https://a.example/api", "PANEL_PANEL_A_USERNAME": "u",
           "PANEL_PANEL_A_PASSWORD": "p",
           "PANEL_MY_PANEL_URL": "https://c.example", "PANEL_MY_PANEL_USERNAME": "u",
           "PANEL_MY_PANEL_PASSWORD": "p", "PANEL_MY_PANEL_CORE_ID": "3"}
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        panels = Config._parse_panels()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    # a trailing /api is dropped; '-' in a name is '_' in the keys; bad names are skipped
    assert sorted(panels) == ["my-panel", "panel-a"], panels
    assert panels["panel-a"]["url"] == "https://a.example" and panels["panel-a"]["core_id"] is None
    assert panels["my-panel"]["url"] == "https://c.example" and panels["my-panel"]["core_id"] == 3, panels


@test
async def fetch_node_cert_reads_what_the_node_presents():
    import shutil as _sh
    import ssl as _ssl
    import subprocess
    assert await node_setup.fetch_node_cert("127.0.0.1", port=9, timeout=2) is None
    if not _sh.which("openssl"):
        print("        (openssl missing: TLS part skipped)")
        return
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                    "ec_paramgen_curve:P-256", "-keyout", "k.pem", "-out", "c.pem",
                    "-days", "1", "-nodes", "-subj", "/CN=node"],
                   check=True, capture_output=True)
    ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain("c.pem", "k.pem")
    ctx.set_alpn_protocols(["h2"])

    async def handle(reader, writer):
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    try:
        pem = await node_setup.fetch_node_cert("127.0.0.1", port=port, timeout=5)
    finally:
        server.close()
    want = _ssl.DER_cert_to_PEM_cert(_ssl.PEM_cert_to_DER_cert(open("c.pem").read()))
    assert pem == want, pem


@test
async def schedule_times_are_iran_time():
    from datetime import datetime, timezone
    ns = reset_schedule.next_occurrence
    assert reset_schedule.IRAN.utcoffset(datetime(2026, 10, 16)).total_seconds() == 3.5 * 3600
    utc = lambda *a: datetime(*a, tzinfo=timezone.utc).timestamp()  # noqa: E731
    after = utc(2026, 10, 16, 20, 0)          # Fri 16 Oct 23:30 in Tehran
    daily = {"repeat": "d", "day": 0, "hour": 4, "minute": 0}
    assert ns(daily, after) == utc(2026, 10, 17, 0, 30)       # 04:00 Tehran
    assert ns(dict(daily, hour=23, minute=50), after) == utc(2026, 10, 16, 20, 20)
    saturday = {"repeat": "w", "day": 5, "hour": 4, "minute": 0}
    assert ns(saturday, after) == utc(2026, 10, 17, 0, 30)
    friday = {"repeat": "w", "day": 4, "hour": 9, "minute": 0}  # today, already past
    assert ns(friday, after) == utc(2026, 10, 23, 5, 30)
    day31 = {"repeat": "m", "day": 31, "hour": 0, "minute": 0}
    assert ns(day31, after) == utc(2026, 10, 30, 20, 30)        # 31 Oct 00:00 Tehran
    assert ns(day31, utc(2026, 11, 1)) == utc(2026, 11, 29, 20, 30)  # November: the 30th
    once = {"repeat": "o", "day": 20261016, "hour": 10, "minute": 0}
    assert ns(once, after) is None
    assert reset_schedule.describe(saturday) == "every Saturday at 04:00"


@test
async def scheduled_reset_wizard_run_and_missed_run():
    import time as _time
    main, _, chat = fresh_world()
    bot = Bot()
    sid = main.add_server("alpha", "cx23", "fsn1", traffic_tb=12)
    await chat.press(f"server_{sid}")
    await chat.press(chat.last.markup.find("Reset Schedule"))
    assert "No scheduled resets" in chat.last.text and "Iran time" in chat.last.text
    await chat.press(chat.last.markup.find("Add Schedule"))
    await chat.press(chat.last.markup.find("Every month"))
    await chat.press(f"rschday_{sid}_m_1")
    await chat.press(f"rschh_{sid}_m_1_4")
    await chat.press(f"rschm_{sid}_m_1_4_30")
    assert "Saved: every month on day 1 at 04:30" in chat.last.text, chat.last.text
    await chat.press(f"rschm_{sid}_m_1_4_30")
    assert "already exists" in chat.last.text
    (sched,) = reset_schedule.list_schedules(sid)
    await chat.press(f"server_{sid}")
    assert "Reset every month on day 1 at 04:30" in chat.last.text
    await chat.press("start_menu")
    assert chat.last.markup.find("Alerts & Schedules (1)")
    await chat.press("watches")
    assert "Scheduled traffic resets" in chat.last.text

    # Due a minute ago: the job resets the server and moves the schedule on.
    reset_schedule._save([dict(sched, next_run=_time.time() - 60)])
    tasks = await reset_schedule.check_reset_schedules(bot)
    await asyncio.gather(*tasks)
    assert main.servers[sid]["outgoing_traffic"] == 0 and main.plan_of(sid) == "cx23"
    assert "Scheduled traffic reset started" in bot.sent[0]
    assert "completed successfully" in bot.sent[-1] and "Next run" in bot.sent[-1]
    (moved,) = reset_schedule.list_schedules()
    assert moved["next_run"] > _time.time()

    # Two hours late (bot was down): skipped, not run at an unplanned hour.
    main.servers[sid]["outgoing_traffic"] = 5 * 1024 ** 4
    reset_schedule._save([dict(moved, next_run=_time.time() - 7200)])
    assert await reset_schedule.check_reset_schedules(bot) == []
    assert "missed" in bot.sent[-1] and main.servers[sid]["outgoing_traffic"] > 0

    # A one-off is removed once it ran; a deleted server drops its schedules.
    once, err = reset_schedule.add_schedule("main", sid, "alpha", ADMIN, "o", 20991231, 3, 0)
    assert not err
    reset_schedule._save([dict(once, next_run=_time.time() - 1)])
    await asyncio.gather(*await reset_schedule.check_reset_schedules(bot))
    assert reset_schedule.list_schedules() == []
    reset_schedule.add_schedule("main", sid, "alpha", ADMIN, "d", 0, 3, 0)
    await chat.press(f"deletego_{sid}")
    assert reset_schedule.list_schedules() == []


@test
async def stranded_server_goes_back_to_its_plan():
    main, _, chat = fresh_world()
    bot = Bot()
    sid = main.add_server("alpha", "cx23", "fsn1", traffic_tb=8)
    main.refuse_change["cx23"] = None
    await chat.press(f"resetgo_{sid}")
    assert main.plan_of(sid) == "cpx22" and "Still on" in chat.last.text
    (w,) = plan_watch.list_watches()
    assert w["plan"] == "cx23" and plan_watch.is_return(w)
    await chat.press(f"server_{sid}")
    assert "Waiting to move back to `cx23`" in chat.last.text

    # A return watch keeps switching however often Hetzner refuses.
    for _ in range(plan_watch.AUTO_REFUSALS_BEFORE_NOTIFY + 2):
        for x in plan_watch.list_watches():
            plan_watch._put(dict(x, retry_after=0))
        await plan_watch.check_plan_watches(bot)
    (w,) = plan_watch.list_watches()
    assert w["auto"] is True and not any("paused" in t for t in bot.sent)
    assert main.servers[sid]["status"] == "running"

    # The next reset tries cx23 first; once it has room the server is home
    # and the return watch is gone.
    del main.refuse_change["cx23"]
    main.servers[sid]["outgoing_traffic"] = 3 * 1024 ** 4
    await chat.press(f"resetgo_{sid}")
    assert main.plan_of(sid) == "cx23" and main.servers[sid]["outgoing_traffic"] == 0
    assert "completed successfully" in chat.last.text
    await chat.press(f"resetlog_{sid}")
    assert "back on cx23" in chat.last.text
    assert plan_watch.list_watches() == []

    # Stranded again, then the watch itself brings it back.
    main.refuse_change["cx23"] = None
    await chat.press(f"resetgo_{sid}")
    del main.refuse_change["cx23"]
    plan_watch._put(dict(plan_watch.list_watches()[0], retry_after=0))
    await plan_watch.check_plan_watches(bot)
    assert main.plan_of(sid) == "cx23" and "Back on the original plan" in bot.sent[-1]


@test
async def floating_ip_shows_the_command_to_run():
    from utils import floating_ip_command
    assert floating_ip_command({"ip": "203.0.113.139", "type": "ipv4"}) == \
        "sudo ip addr add 203.0.113.139 dev eth0"
    assert floating_ip_command({"ip": "2a01:4f8:1c1c:1234::/64", "type": "ipv6"}) == \
        "sudo ip addr add 2a01:4f8:1c1c:1234::1/64 dev eth0"
    main, _, chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1")
    main.floating_ips += [
        {"id": 901, "ip": "203.0.113.139", "type": "ipv4", "server": sid,
         "home_location": {"name": "fsn1"}},
        {"id": 902, "ip": "203.0.113.140", "type": "ipv4", "server": None,
         "home_location": {"name": "fsn1"}},
    ]
    await chat.press("fips")
    assert "ip addr add" not in chat.last.text
    await chat.press("fiptog_902")
    assert "`sudo ip addr add 203.0.113.140 dev eth0`" in chat.last.text, chat.last.text
    assert "177.139 dev" not in chat.last.text
    await chat.press(f"srvfip_{sid}")
    assert "`sudo ip addr add 203.0.113.139 dev eth0`" in chat.last.text
    assert "177.140 dev" not in chat.last.text


@test
async def similar_plans_rank_by_hardware():
    from server_manager import similar_plans
    main, _, _chat = fresh_world()
    types = await main.get_server_types()
    # cx23 (2/4) stranded on cx33 (4/8): cpx22 matches; cax11 is ARM, the
    # rest are no closer than cx33.
    assert similar_plans(types, "cx23", "cx33", 40, "fsn1") == ["cpx22"]
    main.stock[("cpx22", "fsn1")] = False   # sold-out flag: still offered
    types = await main.get_server_types(fresh=True)
    assert similar_plans(types, "cx23", "cx33", 40, "fsn1") == ["cpx22"]
    assert similar_plans(types, "cx23", "cpx22", 40, "fsn1") == []   # already closest
    assert similar_plans(types, "cx23", "cx33", 100, "fsn1") == []   # disk too big


@test
async def one_reset_or_plan_change_per_server_at_a_time():
    import server_manager
    main, _, chat = fresh_world()
    bot = Bot()
    sid = main.add_server("alpha", "cx23", "fsn1", traffic_tb=4)
    plan_watch.add_watch("main", "fsn1", "cx33", sid, ADMIN)
    server_manager._busy.add(sid)
    try:
        ok, logs, _ = await reset_server_traffic(sid, None, main, get_overage_tracker("main"))
        assert not ok and "already running" in logs[0][1]
        assert not (await change_plan(main, sid, "cx33"))["ok"]
        await plan_watch.check_plan_watches(bot)          # skipped, kept
        assert len(plan_watch.list_watches()) == 1 and not bot.sent
    finally:
        server_manager._busy.discard(sid)
    assert main.plan_of(sid) == "cx23" and main.servers[sid]["status"] == "running"
    assert not any("poweroff" in c[1] for c in main.calls)


@test
async def server_page_is_grouped_with_manage_and_network():
    main, _, chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1", traffic_tb=12)
    main.servers[sid]["public_net"]["ipv6"] = {"ip": "2a01:4f8:c0c:1::/64"}
    await chat.press(f"server_{sid}")
    data = chat.last.markup.data()
    for gone in (f"rebuild_{sid}", f"delete_{sid}", f"srvfip_{sid}", f"backup_{sid}"):
        assert gone not in data, gone                   # moved into the submenus
    assert f"srvmng_{sid}" in data and f"srvnet_{sid}" in data and f"reset_{sid}" in data
    assert chat.last.markup.copies["📋 Copy IP"] == main.servers[sid]["public_net"]["ipv4"]["ip"]
    assert "`▓▓▓▓▓▓░░░░` 60.0%" in chat.last.text and "12.00/20 TB" in chat.last.text
    await chat.press(f"srvmng_{sid}")
    data = chat.last.markup.data()
    for want in ("rebuild_", "srvsnap_", "rename_", "price_", "backup_", "resetpw_", "delete_"):
        assert f"{want}{sid}" in data, want
    await chat.press(f"srvnet_{sid}")
    assert chat.last.markup.copies["📋 Copy IPv6"] == "2a01:4f8:c0c:1::1"
    assert f"srvfip_{sid}" in chat.last.markup.data() and f"volmenu_{sid}" in chat.last.markup.data()


@test
async def server_list_sorts_filters_and_tags_panel_nodes():
    from aiohttp import web
    from config import Config
    main, second, chat = fresh_world()
    low = main.add_server("alpha", "cx23", "fsn1", traffic_tb=2)
    high = second.add_server("zulu", "cx23", "nbg1", traffic_tb=18)
    mid = main.add_server("mike", "cx23", "fsn1", traffic_tb=16)
    node_ip = main.servers[low]["public_net"]["ipv4"]["ip"]

    async def token(request):
        return web.json_response({"access_token": "T"})

    async def nodes(request):
        return web.json_response({"nodes": [{"id": 1, "name": "a", "address": node_ip}],
                                  "total": 1})

    app = web.Application()
    app.add_routes([web.post("/api/admin/token", token), web.get("/api/nodes", nodes)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    panel = Config.PANELS["panel-b"]
    saved = dict(panel)
    panel["url"] = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        await chat.press("list_servers")
        ids = [d for d in chat.last.markup.data() if d.startswith("server_")]
        assert ids == [f"server_{high}", f"server_{mid}", f"server_{low}"], ids  # by traffic
        labels = [t for t, d in chat.last.markup.buttons if d.startswith("server_")]
        assert "▓▓▓▓░ 90%" in labels[0] and "🧩panel-b" in labels[2], labels
        await chat.press(chat.last.markup.find("Sort: traffic"))
        ids = [d for d in chat.last.markup.data() if d.startswith("server_")]
        assert ids == [f"server_{low}", f"server_{mid}", f"server_{high}"], ids  # by name
        await chat.press(chat.last.markup.find("Near limit (2)"))
        ids = [d for d in chat.last.markup.data() if d.startswith("server_")]
        assert ids == [f"server_{mid}", f"server_{high}"], ids
        await chat.press(f"server_{low}")
        assert "Node of panel `panel-b`" in chat.last.text
    finally:
        await runner.cleanup()
        panel.clear()
        panel.update(saved)


@test
async def main_menu_shows_the_dashboard():
    main, second, chat = fresh_world()
    main.add_server("alpha", "cx23", "fsn1", traffic_tb=21)      # over (1 TB × €1.19)
    main.add_server("bravo", "cx33", "fsn1", traffic_tb=16, status="off")   # near
    second.add_server("charlie", "cx23", "nbg1", traffic_tb=1)
    reset_schedule.add_schedule("main", 1, "x", ADMIN, "d", 0, 4, 0)
    await chat.press("start_menu")
    text = chat.last.text
    assert "Servers: *3* (2 running, 1 other)" in text, text
    assert "main 2 · second 1" in text
    assert "Near the traffic limit: *1* · over it: *1*" in text
    assert "`€12.47`/month + overage `€1.19`" in text, text      # 3.49 + 5.49 + 3.49
    assert "Alerts & schedules: *1*" in text


@test
async def reset_shows_one_status_line_then_a_summary():
    main, _, chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1", traffic_tb=12)
    before = len(chat.screens)
    await chat.press(f"resetgo_{sid}")
    progress = [s.text for s in chat.screens[before:-1]]
    assert any("Step 3/6: Moving to cx33" in t for t in progress), progress
    assert any("Step 6/6" in t for t in progress)
    assert all("Fetching server information" not in t or t.count("\n") < 8 for t in progress)
    final = chat.last.text
    assert "`12.00/20 TB` → `0.00/20 TB`" in final and "completed successfully" in final, final
    assert "Shutting down" not in final                      # the log is behind Details
    await chat.press(chat.last.markup.find("Details"))
    assert "Shutting down server" in chat.last.text and "Reset log" in chat.last.text


@test
async def copy_buttons_for_passwords_and_commands():
    main, _, chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1")
    await chat.press(f"resetpw_confirm_{sid}")
    assert chat.last.markup.copies["📋 Copy password"] == "FakePass123"
    main.floating_ips.append({"id": 905, "ip": "203.0.113.9", "type": "ipv4", "server": sid,
                              "home_location": {"name": "fsn1"}})
    await chat.press(f"srvfip_{sid}")
    assert chat.last.markup.copies["📋 Copy command"] == "sudo ip addr add 203.0.113.9 dev eth0"
    await chat.press("fiptog_905")
    assert chat.last.markup.copies["📋 Copy command"] == "sudo ip addr add 203.0.113.9 dev eth0"


@test
async def slash_commands_open_the_screens():
    from types import SimpleNamespace
    from fake_hetzner import FakeMessage
    from handlers import COMMAND_HANDLERS
    main, second, chat = fresh_world()
    a = main.add_server("web-1", "cx23", "fsn1")
    b = second.add_server("web-2", "cx23", "nbg1")
    c = main.add_server("db", "cx23", "fsn1")

    async def run(command, *args, user=ADMIN):
        chat.context.args = list(args)
        update = SimpleNamespace(message=FakeMessage(chat), effective_user=SimpleNamespace(id=user),
                                 effective_chat=SimpleNamespace(id=chat.chat_id))
        before = len(chat.screens)
        await COMMAND_HANDLERS[command](update, chat.context)
        return chat.screens[before:]

    shown = await run("find", "web")
    assert shown[0].how == "reply" and "2 servers match" in shown[0].text
    assert {f"server_{a}", f"server_{b}"} <= set(shown[0].markup.data())
    shown = await run("find", "db")                      # one match → its page, then edits
    assert "🖥️ *db*" in shown[-1].text, [s.text for s in shown]
    ip = second.servers[b]["public_net"]["ipv4"]["ip"]
    shown = await run("find", ip)
    assert "web-2" in shown[-1].text and chat.context.user_data["account"] == "second"
    assert "No server matches" in (await run("find", "nope"))[-1].text
    assert "Usage" in (await run("find"))[-1].text
    assert "All Servers" in (await run("servers"))[-1].text
    assert "Alerts & Schedules" in (await run("alerts"))[-1].text
    assert "Cost" in (await run("cost"))[-1].text or "cost" in (await run("cost"))[-1].text
    assert await run("servers", user=999) == []
    _ = c


@test
async def auto_reset_rule_and_settings():
    import time as _time
    from datetime import datetime
    fresh_world()
    s = auto_reset.settings()
    assert s["enabled"] and (s["hour"], s["minute"], s["threshold"]) == (5, 0, 90)
    run = datetime.fromtimestamp(s["next_run"], reset_schedule.IRAN)
    assert (run.hour, run.minute) == (5, 0) and s["next_run"] > _time.time()
    assert s["next_run"] - _time.time() <= 86400
    auto_reset.set_time(3, 30)
    run = datetime.fromtimestamp(auto_reset.settings()["next_run"], reset_schedule.IRAN)
    assert (run.hour, run.minute) == (3, 30)
    for bad in ((24, 0), (3, 60)):
        try:
            auto_reset.set_time(*bad)
            raise AssertionError("bad time accepted")
        except ValueError:
            pass

    tb = 1024 ** 4
    now = 1_000_000.0
    srv = lambda used: {"outgoing_traffic": int(used * tb), "included_traffic": 20 * tb}  # noqa: E731
    reason = auto_reset.reason
    assert reason(srv(18), None, 90, now) == "threshold"           # 90%
    assert reason(srv(17.9), None, 90, now) is None                # no pace known yet
    # 6 TB in the last day: 15 + 6 > 20 → on pace to pass the limit.
    assert reason(srv(15), {"bytes": 9 * tb, "at": now - 86400}, 90, now) == "pace"
    assert reason(srv(15), {"bytes": 13 * tb, "at": now - 86400}, 90, now) is None
    # The counter went down since (a reset): no pace.
    assert reason(srv(5), {"bytes": 19 * tb, "at": now - 86400}, 90, now) is None
    assert reason(srv(17), None, 85, now) == "threshold"


@test
async def auto_reset_runs_at_night_and_reports():
    import time as _time
    main, second, chat = fresh_world()
    bot = Bot()
    full = main.add_server("full", "cx23", "fsn1", traffic_tb=19)        # 95%
    fast = second.add_server("fast", "cx23", "fsn1", traffic_tb=15)      # pace
    left = main.add_server("left-out", "cx23", "fsn1", traffic_tb=19)    # excluded
    off = main.add_server("off", "cx23", "fsn1", traffic_tb=19, status="off")
    calm = main.add_server("calm", "cx23", "fsn1", traffic_tb=2)
    auto_reset.set_included(left, False)
    tb = 1024 ** 4
    auto_reset._update(samples={str(fast): {"bytes": 9 * tb, "at": _time.time() - 86400},
                                str(calm): {"bytes": 1 * tb, "at": _time.time() - 86400}})

    assert await auto_reset.check_auto_reset(bot) is None    # not due yet
    auto_reset._update(next_run=_time.time() - 60)
    done = await auto_reset.check_auto_reset(bot)
    entries = await done
    assert sorted((e["name"], e["reason"]) for e in entries) == [("fast", "pace"), ("full", "threshold")]
    assert main.servers[full]["outgoing_traffic"] == 0 and second.servers[fast]["outgoing_traffic"] == 0
    for sid in (left, off):
        assert main.servers[sid]["outgoing_traffic"] == 19 * tb
    assert main.servers[off]["status"] == "off"               # never powered on by it
    assert "Auto reset started" in bot.sent[0] and "`full`" in bot.sent[0]
    assert "✅ `full` (95%): reset" in bot.sent[-1] and "on pace" in bot.sent[-1], bot.sent[-1]
    s = auto_reset.settings()
    assert s["next_run"] > _time.time() + 3600
    samples = auto_reset._load()["samples"]
    assert samples[str(full)]["bytes"] == 0 and samples[str(calm)]["bytes"] == 2 * tb

    # A night with nothing to do stays silent.
    sent = len(bot.sent)
    auto_reset._update(next_run=_time.time() - 60)
    assert await (await auto_reset.check_auto_reset(bot)) == []
    assert len(bot.sent) == sent

    # Two hours late (bot was down): skipped and reported.
    main.servers[full]["outgoing_traffic"] = 19 * tb
    auto_reset._update(next_run=_time.time() - 7200)
    assert await auto_reset.check_auto_reset(bot) is None
    assert "missed" in bot.sent[-1] and main.servers[full]["outgoing_traffic"] == 19 * tb

    # Switched off: nothing runs; switching on starts from the next run.
    auto_reset.set_enabled(False)
    auto_reset._update(next_run=_time.time() - 60)
    assert await auto_reset.check_auto_reset(bot) is None
    auto_reset.set_enabled(True)
    assert auto_reset.settings()["next_run"] > _time.time()

    # A busy server is skipped and named in the report.
    import server_manager
    server_manager._busy.add(full)
    try:
        auto_reset._update(next_run=_time.time() - 60)
        entries = await (await auto_reset.check_auto_reset(bot))
        assert entries[0]["skipped"] and "skipped" in bot.sent[-1]
    finally:
        server_manager._busy.discard(full)


@test
async def auto_reset_chat_screens():
    main, _, chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1", traffic_tb=4)
    await chat.press(f"server_{sid}")
    assert "Auto reset at 90%+ — nightly 05:00" in chat.last.text, chat.last.text
    await chat.press("watches")
    await chat.press(chat.last.markup.find("Auto Reset: 05:00"))
    assert "Auto Reset* — ✅ On" in chat.last.text and "`05:00`" in chat.last.text
    assert "1 of 1 take part" in chat.last.text
    await chat.press("art_85")
    assert "Threshold: 85%" in chat.last.text and chat.last.markup.find("✅ 85%")
    await chat.press("arh")
    await chat.press("arhh_4")
    await chat.press("arm_4_30")
    assert "Time: 04:30" in chat.last.text and auto_reset.time_text() == "04:30"
    await chat.press("arsv")
    await chat.press(chat.last.markup.find("✅ alpha"))
    assert chat.last.markup.find("⛔ alpha") and not auto_reset.is_included(sid)
    await chat.press(f"server_{sid}")
    assert "Auto reset" not in chat.last.text
    await chat.press(f"rsch_{sid}")
    assert "Auto reset: off for this server" in chat.last.text
    await chat.press(f"arsrv_{sid}")
    assert "Auto reset on for this server" in chat.last.text and auto_reset.is_included(sid)
    await chat.press("artog")
    assert "Turned off" in chat.last.text and not auto_reset.settings()["enabled"]
    await chat.press(f"rsch_{sid}")
    assert "Auto reset is off for every server" in chat.last.text

@test
async def pause_stops_every_automation_until_resumed():
    import time as _time
    main, _, chat = fresh_world()
    bot = Bot()
    sid = main.add_server("alpha", "cx23", "fsn1", traffic_tb=19)
    plan_watch.add_watch("main", "fsn1-dc14", "cx33", sid, ADMIN)
    create_watch.add_create_watch("main", "cx23", ["fsn1"], "ubuntu-24.04", False, None, ADMIN)
    sched, _ = reset_schedule.add_schedule("main", sid, "alpha", ADMIN, "d", 0, 4, 0)

    await chat.press("hl")
    assert "Automations run" in chat.last.text and chat.last.markup.find("Pause automations")
    await chat.press("apause")
    assert "For how long?" in chat.last.text
    await chat.press("apause_6")
    assert automation.is_paused() and "paused until" in chat.last.text, chat.last.text
    assert chat.last.markup.find("Resume automations")
    await chat.press("start_menu")
    assert "Automations paused until" in chat.last.text and chat.last.markup.find("🩺 Health ⏸")
    await chat.press("watches")
    assert "nothing here acts" in chat.last.text and "aresume" in chat.last.markup.data()

    await plan_watch.check_plan_watches(bot)
    await create_watch.check_create_watches(bot)
    assert main.plan_of(sid) == "cx23" and len(main.servers) == 1 and not bot.sent
    assert len(plan_watch.list_watches()) == 1 and len(create_watch.list_create_watches()) == 1
    reset_schedule._save([dict(sched, next_run=_time.time() - 60)])
    assert await reset_schedule.check_reset_schedules(bot) == []
    assert "skipped" in bot.sent[-1] and "paused" in bot.sent[-1]
    assert reset_schedule.list_schedules()[0]["next_run"] > _time.time()
    auto_reset._update(next_run=_time.time() - 60)
    assert await auto_reset.check_auto_reset(bot) is None
    assert "Auto reset skipped" in bot.sent[-1] and main.servers[sid]["outgoing_traffic"] > 0

    await chat.press("aresume")
    assert not automation.is_paused() and "resumed" in chat.last.text
    await plan_watch.check_plan_watches(bot)
    assert main.plan_of(sid) == "cx33"                    # the watch acted

    # A timed pause ends by itself, with a message.
    automation.pause(1)
    automation._save(dict(automation._load(), until=int(_time.time()) - 5))
    assert not automation.is_paused()
    assert await automation.check_pause_end(bot) and "Automations resumed" in bot.sent[-1]
    assert not await automation.check_pause_end(bot)
    automation.pause(None)
    assert automation.is_paused() and automation.state()["until"] is None
    assert not await automation.check_pause_end(bot)


@test
async def health_watches_accounts_locks_and_failed_actions():
    import time as _time
    main, second, chat = fresh_world()
    bot = Bot()
    sid = main.add_server("alpha", "cx23", "fsn1")
    main.actions.append({"id": 5, "command": "poweron", "status": "error", "progress": 100,
                         "started": None, "finished": None, "resources": [], "error": None})

    await health.check_health(bot)
    assert not bot.sent and health._load().get("failed", []) == []
    main.actions.clear()
    second.access = (401, "unauthorized", "unable to authenticate")
    await health.check_health(bot)
    assert "`second` *rejects its token*" in bot.sent[-1], bot.sent
    await health.check_health(bot)
    assert len(bot.sent) == 1                                  # said once
    await chat.press("start_menu")
    assert "token rejected" in chat.last.text and chat.last.markup.find("🩺 Health ⚠️")
    await chat.press("hl")
    assert "`second` — token rejected (401)" in chat.last.text, chat.last.text
    assert "`main` — API ok · 3,500 of 3,600" in chat.last.text
    second.access = (200, "", "")
    await health.check_health(bot)
    assert "answers again" in bot.sent[-1]

    # No answer: only after two checks in a row.
    main.access = (0, "", "timeout")
    await health.check_health(bot)
    assert len(bot.sent) == 2
    await health.check_health(bot)
    assert "does not answer" in bot.sent[-1]
    main.access = (200, "", "")
    await health.check_health(bot)
    assert "answers again" in bot.sent[-1]

    def again():
        d = health._load()
        d["actions_at"] = {}
        health._save(d)

    # A long lock is reported once (not while the bot itself works on it).
    main.servers[sid]["locked"] = True
    again()
    await health.check_health(bot)
    d = health._load()
    d["locked"][str(sid)]["since"] -= 3600
    health._save(d)
    sent = len(bot.sent)
    server_manager._busy.add(sid)
    again()
    await health.check_health(bot)
    server_manager._busy.discard(sid)
    assert len(bot.sent) == sent
    again()
    await health.check_health(bot)
    assert "*Server locked* — `alpha`" in bot.sent[-1]
    again()
    await health.check_health(bot)
    assert len(bot.sent) == sent + 1
    main.servers[sid]["locked"] = False
    again()
    await health.check_health(bot)
    assert "unlocked again" in bot.sent[-1]

    # Failed actions: the ones there at the first look are only noted; then 3 new
    # ones within the hour → one alert, and quiet for the rest of the hour.
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).isoformat()

    def failed(n):
        main.actions.append({"id": 900 + n, "command": "change_type", "status": "error",
                             "progress": 100, "started": stamp, "finished": stamp,
                             "resources": [{"id": sid, "type": "server"}],
                             "error": {"code": "placement_error", "message": "no capacity"}})
    assert health._load()["failed_seen"] == {"main": 5, "second": 0}
    failed(0)
    failed(1)
    sent = len(bot.sent)
    again()
    await health.check_health(bot)
    assert len(health._load()["failed"]) == 2 and len(bot.sent) == sent
    failed(2)
    again()
    await health.check_health(bot)
    assert "3 failed Hetzner actions in the last hour" in bot.sent[-1], bot.sent[-1]
    assert "`alpha`: plan change — no capacity" in bot.sent[-1]
    failed(3)
    again()
    await health.check_health(bot)
    assert len(bot.sent) == sent + 1 and len(health.failed_since(3600, "main")) == 4
    await chat.press("hl")
    assert "Failed actions, 24 h*: " in chat.last.text and "no capacity" in chat.last.text


@test
async def daily_check_report_and_settings():
    import time as _time
    from datetime import datetime
    main, second, chat = fresh_world()
    bot = Bot()
    main.add_server("alpha", "cx23", "fsn1", traffic_tb=19)
    main.add_server("sleepy", "cx23", "fsn1", status="off")
    r = health.report_settings()
    run = datetime.fromtimestamp(r["next_run"], reset_schedule.IRAN)
    assert r["enabled"] and (run.hour, run.minute) == (9, 0)
    assert not await health.check_daily_report(bot)
    health._set_report(next_run=_time.time() - 30)
    assert await health.check_daily_report(bot)
    text = bot.sent[-1]
    assert "Daily check" in text and "2 servers: 1 running, 1 not running (`sleepy`)" in text, text
    assert "1 near the traffic limit" in text and "No failed actions" in text
    assert "Automations: running" in text and "Auto reset: no run yet" in text
    assert health.report_settings()["next_run"] > _time.time()

    await chat.press("hl")
    await chat.press("hlh")
    await chat.press("hlhh_8")
    await chat.press("hlm_8_30")
    assert "Daily check at 08:30" in chat.last.text and health.report_time_text() == "08:30"
    await chat.press("hltog")
    assert "Daily check off" in chat.last.text and not health.report_settings()["enabled"]
    health._set_report(next_run=_time.time() - 30)
    assert not await health.check_daily_report(bot)
    await chat.press("hlrep")
    assert "Daily check" in chat.last.text and "`second`" in chat.last.text


@test
async def running_now_shows_the_bots_work_and_hetzner_actions():
    main, _, chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1")
    other = main.add_server("beta", "cx23", "fsn1")
    await chat.press("running")
    assert "Nothing is running" in chat.last.text
    assert server_manager.claim(sid, "reset", "Traffic reset", 6)
    server_manager.note(sid, "Moving to cx33", 3)
    main.actions.append({"id": 77, "command": "create_image", "status": "running", "progress": 40,
                         "started": "2026-10-20T10:00:00+00:00", "finished": None,
                         "resources": [{"id": other, "type": "server"}], "error": None})
    try:
        await chat.press("running")
        assert "`alpha` — Traffic reset" in chat.last.text and "step 3/6: Moving to cx33" in chat.last.text
        assert "`beta` — snapshot ▰▰▰▰▱▱▱▱▱▱ 40%" in chat.last.text, chat.last.text
        assert f"server_{other}" in chat.last.markup.data()
        await chat.press(f"server_{sid}")
        assert "⏳ Traffic reset (step 3/6): Moving to cx33" in chat.last.text
        main.servers[other]["locked"] = True
        await chat.press(f"server_{other}")
        assert "⏳ Hetzner: snapshot 40%" in chat.last.text, chat.last.text
        await chat.press("start_menu")
        assert "Running now: *1*" in chat.last.text
    finally:
        server_manager.release(sid)
    assert server_manager.activity() == []
    # A real reset registers itself while it runs.
    seen = []

    async def watch(logs, stage):
        seen.append([a["stage"] for a in server_manager.activity()])
    await reset_server_traffic(sid, watch, main, get_overage_tracker("main"))
    assert any("Powering off" in st for row in seen for st in row) and not server_manager.activity()


@test
async def auto_reset_own_time_per_server():
    import time as _time
    main, _, chat = fresh_world()
    bot = Bot()
    early = main.add_server("early", "cx23", "fsn1", traffic_tb=19)
    late = main.add_server("late", "cx23", "fsn1", traffic_tb=19)
    await chat.press(f"rsch_{early}")
    await chat.press(chat.last.markup.find("Auto reset time: 05:00 (default)"))
    await chat.press(f"arsth_{early}_3")
    await chat.press(f"arstm_{early}_3_30")
    assert "time of this server: 03:30" in chat.last.text
    assert chat.last.markup.find("Auto reset time: 03:30")
    s = auto_reset.settings()
    assert auto_reset.slot_of(early) == "03:30" and auto_reset.slot_of(late) == "05:00"
    assert set(s["next_runs"]) == {"03:30"}
    await chat.press(f"server_{early}")
    assert "nightly 03:30 (its own time)" in chat.last.text
    await chat.press("ar")
    assert "1 of them at their own time (03:30)" in chat.last.text
    await chat.press("arsv")
    assert chat.last.markup.find("early · 95% · 03:30")

    # The 03:30 run looks only at its server.
    auto_reset._update(next_runs={"03:30": _time.time() - 60})
    entries = await (await auto_reset.check_auto_reset(bot))
    assert [e["name"] for e in entries] == ["early"] and "(03:30)" in bot.sent[0]
    assert main.servers[late]["outgoing_traffic"] > 0
    assert auto_reset.settings()["next_runs"]["03:30"] > _time.time()
    # The default run skips it.
    main.servers[early]["outgoing_traffic"] = 19 * 1024 ** 4
    auto_reset._update(next_run=_time.time() - 60)
    entries = await (await auto_reset.check_auto_reset(bot))
    assert [e["name"] for e in entries] == ["late"]
    assert "reset" in auto_reset.settings()["last_summary"]
    # Both due at once: one task, slot after slot.
    for sid in (early, late):
        main.servers[sid]["outgoing_traffic"] = 19 * 1024 ** 4
    auto_reset._update(next_run=_time.time() - 60, next_runs={"03:30": _time.time() - 60})
    entries = await (await auto_reset.check_auto_reset(bot))
    assert sorted(e["name"] for e in entries) == ["early", "late"]

    await chat.press(f"arst_{early}")
    await chat.press(f"arstd_{early}")
    assert "uses the default time (05:00)" in chat.last.text
    assert auto_reset.settings()["next_runs"] == {} and auto_reset.slot_of(early) == "05:00"
    # A server time equal to a new default joins the default slot.
    auto_reset.set_server_time(late, 4, 0)
    auto_reset.set_time(4, 0)
    assert auto_reset.settings()["next_runs"] == {} and not auto_reset.has_own_time(late)


@test
async def protection_blocks_delete_and_rebuild():
    main, _, chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1")
    await chat.press(f"srvmng_{sid}")
    assert "Protection is off" in chat.last.text
    await chat.press(chat.last.markup.find("Protection: OFF"))
    assert main.servers[sid]["protection"] == {"delete": True, "rebuild": True}
    assert "Protection on." in chat.last.text and chat.last.markup.find("Protection: ON")
    await chat.press(f"server_{sid}")
    assert "🛡 Protected" in chat.last.text
    await chat.press(f"delete_{sid}")
    assert "*Protected*" in chat.last.text and f"prot_{sid}_0" in chat.last.markup.data()
    await chat.press(f"deletego_{sid}")                     # an old button: still refused
    assert sid in main.servers
    await chat.press(f"rebuild_{sid}")
    assert "refuses to rebuild" in chat.last.text
    await chat.press(f"rebuildgo_{sid}_debian-12")
    assert not any("rebuild" in c[1] for c in main.calls)
    await chat.press(f"prot_{sid}_0")
    assert main.servers[sid]["protection"]["delete"] is False
    await chat.press(f"deletego_{sid}")
    assert sid not in main.servers and "Server Deleted" in chat.last.text


@test
async def change_ipv4_new_kept_and_refused():
    main, _, chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1")
    old = main.ip_of(sid)
    await chat.press(f"srvnet_{sid}")
    await chat.press(chat.last.markup.find("Change IPv4"))
    assert f"Now: `{old}`" in chat.last.text and "powered off for about a minute" in chat.last.text
    await chat.press(f"ipchgo_{sid}_n_0")
    new = main.ip_of(sid)
    assert new != old and f"`{old}` → `{new}`" in chat.last.text, chat.last.text
    assert chat.last.markup.copies["📋 Copy new IP"] == new
    assert main.servers[sid]["status"] == "running"
    assert old not in [p["ip"] for p in main.all_pips()]          # deleted
    pip = next(p for p in main.all_pips() if p["ip"] == new)
    assert pip["auto_delete"] is True

    # Keep the old one: it stays, unassigned, and is offered next time.
    await chat.press(f"ipchgo_{sid}_n_1")
    kept = next(p for p in main.all_pips() if p["ip"] == new)
    assert kept["assignee_id"] is None and kept["auto_delete"] is False
    assert "was kept" in chat.last.text
    await chat.press(f"ipchg_{sid}")
    assert chat.last.markup.find(f"Use kept {new}")
    await chat.press(f"ipchgo_{sid}_{kept['id']}_0")
    assert main.ip_of(sid) == new and kept["auto_delete"] is True

    # Hetzner refuses the new IP: the old one goes back, the new one is deleted.
    before = main.ip_of(sid)
    count = len(main.all_pips())
    main.refuse_pip_assign = 1
    await chat.press(f"ipchgo_{sid}_n_0")
    assert "IPv4 not changed" in chat.last.text and main.ip_of(sid) == before
    assert main.servers[sid]["status"] == "running" and len(main.all_pips()) == count
    assert not server_manager.is_busy(sid)


@test
async def delete_keeping_the_ip_and_using_it_again():
    main, _, chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "nbg1")
    main.add_server("bystander", "cx23", "fsn1")
    ip = main.ip_of(sid)
    auto_reset.set_server_time(sid, 3, 0)
    await chat.press(f"delete_{sid}")
    await chat.press(chat.last.markup.find("keep the IP"))
    assert sid not in main.servers and f"`{ip}` was kept" in chat.last.text
    kept = next(p for p in main.all_pips() if p["ip"] == ip)
    assert kept["assignee_id"] is None and kept["auto_delete"] is False
    assert auto_reset.settings()["times"] == {}                  # forgotten with the server

    # The wizard offers it in nbg1 (not in fsn1), for one server.
    await chat.press("srvc")
    await chat.press("srvcat_regular")
    await chat.press("srvt_cpx22")
    await chat.press("srvl_fsn1")
    await chat.press("srvi_ubuntu-24.04")
    await chat.press("srvipv6_0")
    await chat.press("srvcount_1")
    await chat.press("srvname_auto")
    assert "Use kept IP" not in str(chat.last.markup.buttons)
    await chat.press("srvt_cpx22")
    await chat.press("srvl_nbg1")
    await chat.press("srvi_ubuntu-24.04")
    await chat.press("srvipv6_0")
    await chat.press("srvcount_1")
    await chat.press("srvname_auto")
    assert "IPv4: new" in chat.last.text
    await chat.press(chat.last.markup.find(f"Use kept IP {ip}"))
    assert f"IPv4: `{ip}` (kept)" in chat.last.text and chat.last.markup.find("New IPv4 instead")
    await chat.press("srvgo")
    new = next(s for s in main.servers.values() if s["name"].startswith("cpx22-nbg1"))
    assert main.ip_of(new["id"]) == ip and f"`{ip}`" in chat.last.text
    assert next(p for p in main.all_pips() if p["ip"] == ip)["auto_delete"] is True
    assert "srv_pip" not in chat.context.user_data


@test
async def new_server_from_a_snapshot():
    main, _, chat = fresh_world()
    sid = main.add_server("web", "cx33", "fsn1")                 # 80 GB disk
    await chat.press(f"snapcreate_{sid}")
    snap = main.snapshots[-1]
    await chat.press(f"snap_{snap['id']}")
    assert "Disk of the source: `80 GB`" in chat.last.text
    await chat.press(chat.last.markup.find("New server from this snapshot"))
    assert "New server from a snapshot" in chat.last.text and "srvcat_general" in chat.last.markup.data()
    await chat.press("srvcat_cost")                              # x86: straight to Intel plans
    plans = [d for d in chat.last.markup.data() if d.startswith("srvt_")]
    assert plans == ["srvt_cx33", "srvt_cx43", "srvt_cx53"], plans   # cx23's 40 GB is too small
    assert chat.last.markup.data()[-1] == "srvcs"
    await chat.press("srvt_cx43")
    await chat.press("srvl_nbg1")                                # image step skipped
    assert f"📸 {snap['description']}" in chat.last.text and "srvt_cx43" in chat.last.markup.data()
    await chat.press("srvipv6_0")
    await chat.press("srvcount_1")
    assert "web-clone-" in chat.last.text
    await chat.press("srvname_auto")
    assert "Image: `📸 " in chat.last.text and "srvnode_" not in str(chat.last.markup.data())
    await chat.press("srvgo")
    clone = next(s for s in main.servers.values() if s["name"].startswith("web-clone-"))
    assert clone["image"] == str(snap["id"]) and clone["server_type"]["name"] == "cx43"
    await chat.press("srvc")                                     # a normal wizard again
    assert "srv_snap" not in chat.context.user_data

    # The normal image step offers the account's snapshots that fit.
    await chat.press("srvcat_cost")
    await chat.press("srvven_intel")
    await chat.press("srvt_cx23")
    await chat.press("srvl_fsn1")
    assert not any(d == f"srvi_{snap['id']}" for d in chat.last.markup.data())   # 40 < 80 GB
    await chat.press("srvt_cx33")
    await chat.press("srvl_fsn1")
    await chat.press(f"srvi_{snap['id']}")
    assert f"OS: `📸 {snap['description']}`" in chat.last.text


@test
async def startup_stops_only_when_every_token_is_rejected():
    import urllib.error
    import urllib.request
    import main as main_module
    fresh_world()
    saved = urllib.request.urlopen
    refuse = set()

    def fake_urlopen(req, timeout=None):
        token = req.headers.get("Authorization", "").split()[-1]
        if token in refuse:
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)
        return None
    urllib.request.urlopen = fake_urlopen
    try:
        refuse.add("fake-second")
        main_module.check_hetzner_tokens()                       # one bad account: keep going
        refuse.add("fake-main")
        try:
            main_module.check_hetzner_tokens()
            raise AssertionError("did not stop")
        except SystemExit as e:
            assert "Every HETZNER token" in str(e)
    finally:
        urllib.request.urlopen = saved


def sign_init_data(user_id, token="123:fake", auth_date=None):
    """initData the way Telegram signs it for a Mini App."""
    import hashlib
    import hmac
    import json
    import time as _time
    from urllib.parse import urlencode
    fields = {"auth_date": str(int(auth_date or _time.time())), "query_id": "AAE",
              "user": json.dumps({"id": user_id, "first_name": "Owner"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class WebClient:
    """The Mini App's HTTP API on a local test server, signed as the admin."""

    def __init__(self):
        from aiohttp.test_utils import TestClient, TestServer
        import webapp
        self.webapp = webapp
        self.client = TestClient(TestServer(webapp.build_app()))
        self.headers = {"X-Telegram-Init-Data": sign_init_data(ADMIN)}

    async def __aenter__(self):
        await self.client.start_server()
        return self

    async def __aexit__(self, *exc):
        await self.client.close()

    async def call(self, method, path, body=None, headers=None, status=200):
        resp = await self.client.request(method, path, json=body,
                                         headers=self.headers if headers is None else headers)
        data = await resp.json() if resp.content_type == "application/json" else await resp.text()
        assert resp.status == status, (method, path, resp.status, data)
        return data

    async def finish(self, job):
        import webapp_api
        await webapp_api._jobs[job["id"]]["task"]
        return await self.call("GET", f"/api/job/{job['id']}")


@test
async def webapp_checks_telegram_init_data():
    import time as _time
    import webapp
    fresh_world()
    assert webapp.verify_init_data(sign_init_data(ADMIN), "123:fake")["id"] == ADMIN
    assert webapp.verify_init_data(sign_init_data(ADMIN), "999:other") is None
    assert webapp.verify_init_data(sign_init_data(ADMIN).replace("Owner", "Evil"), "123:fake") is None
    assert webapp.verify_init_data(sign_init_data(ADMIN, auth_date=_time.time() - 90000),
                                   "123:fake") is None
    assert webapp.verify_init_data("", "123:fake") is None
    async with WebClient() as web:
        page = await web.call("GET", "/", headers={})
        assert 'lang="en"' in page and "telegram-web-app.js" in page
        await web.call("GET", "/api/dashboard", headers={}, status=401)
        await web.call("GET", "/api/dashboard",
                       headers={"X-Telegram-Init-Data": sign_init_data(999)}, status=403)
        await web.call("GET", "/api/dashboard",
                       headers={"X-Telegram-Init-Data": sign_init_data(ADMIN, token="1:x")},
                       status=401)
        assert (await web.call("GET", "/api/dashboard"))["servers"] == 0


@test
async def webapp_reads_dashboard_servers_and_a_server():
    main, second, _chat = fresh_world()
    a = main.add_server("alpha", "cx23", "fsn1", traffic_tb=16)
    b = second.add_server("beta", "cx33", "nbg1", traffic_tb=21, status="off")
    reset_schedule.add_schedule("main", a, "alpha", ADMIN, "w", 5, 4, 0)
    async with WebClient() as web:
        d = await web.call("GET", "/api/dashboard")
        assert (d["servers"], d["running"], d["near"], d["over"], d["alerts"]) == (2, 1, 1, 1, 1)
        assert d["cost"] == 8.98 and d["overage"] == 1.19 and d["top"][0]["id"] == b
        servers = (await web.call("GET", "/api/servers"))["servers"]
        alpha = next(x for x in servers if x["id"] == a)
        assert alpha["account"] == "main" and alpha["fraction"] == 0.8 and alpha["next_reset"]
        s = await web.call("GET", f"/api/server/{a}")
        assert s["nav"] == {"prev": None, "next": "cx33"}, s["nav"]
        names = {p["name"]: p for p in s["plans"]}
        assert "cax11" not in names and names["cx33"]["disk_ok"] and names["cx33"]["bigger"]
        assert s["schedules"][0]["repeat"] == "w" and s["schedules"][0]["day"] == 5
        await web.call("GET", "/api/server/424242", status=404)


@test
async def webapp_runs_reset_plan_power_and_schedules():
    main, _second, _chat = fresh_world()
    bot = Bot()
    sid = main.add_server("alpha", "cx23", "fsn1", traffic_tb=12)
    async with WebClient() as web:
        web.webapp._bot = bot
        try:
            job = await web.call("POST", f"/api/server/{sid}/reset")
            assert job["kind"] == "reset" and not job["done"]
            job = await web.finish(job)
            assert job["done"] and job["ok"] and job["result"]["after"] == 0
            assert job["result"]["before"] == 12 * 1024 ** 4 and job["step"] == 6
            assert main.servers[sid]["outgoing_traffic"] == 0 and main.plan_of(sid) == "cx23"
            assert "Traffic Reset* (Mini App)" in bot.sent[-1]

            main.servers[sid]["primary_disk_size"] = 80
            await web.call("POST", f"/api/server/{sid}/plan", {"plan": "cx23"}, status=400)
            err = await web.call("POST", f"/api/server/{sid}/plan", {"plan": "cax11"}, status=400)
            assert "error" in err
            main.servers[sid]["primary_disk_size"] = 40
            job = await web.finish(await web.call("POST", f"/api/server/{sid}/plan",
                                                  {"plan": "cx33", "upgrade_disk": False}))
            assert job["ok"] and main.plan_of(sid) == "cx33"
            assert main.servers[sid]["primary_disk_size"] == 40     # CPU/RAM only

            await web.call("POST", f"/api/server/{sid}/watch", {"plan": "cx43", "on": True})
            assert [w["plan"] for w in plan_watch.list_watches()] == ["cx43"]
            await web.call("POST", f"/api/server/{sid}/watch", {"plan": "cx43", "on": False})
            assert plan_watch.list_watches() == []

            job = await web.finish(await web.call("POST", f"/api/server/{sid}/power",
                                                  {"action": "off"}))
            assert job["ok"] and main.servers[sid]["status"] == "off"

            x = await web.call("POST", f"/api/server/{sid}/schedule",
                               {"repeat": "m", "day": "1", "hour": "4", "minute": "30"})
            assert x["repeat"] == "m" and x["next_run"] > 0
            await web.call("POST", f"/api/server/{sid}/schedule",
                           {"repeat": "m", "day": 1, "hour": 4, "minute": 30}, status=400)
            await web.call("POST", f"/api/server/{sid}/schedule",
                           {"repeat": "o", "day": 20200101, "hour": 4, "minute": 0}, status=400)
            await web.call("POST", f"/api/server/{sid}/schedule",
                           {"repeat": "w", "day": 9, "hour": 4, "minute": 0}, status=400)
            alerts = await web.call("GET", "/api/alerts")
            assert [s["id"] for s in alerts["schedules"]] == [x["id"]]
            await web.call("DELETE", f"/api/schedule/{x['id']}")
            await web.call("DELETE", f"/api/schedule/{x['id']}", status=404)

            import server_manager
            server_manager._busy.add(sid)
            try:
                await web.call("POST", f"/api/server/{sid}/reset", status=409)
            finally:
                server_manager._busy.discard(sid)
        finally:
            web.webapp._bot = None


@test
async def webapp_creates_servers_nodes_and_alerts():
    main, _second, _chat = fresh_world()
    bot = Bot()
    main.stock[("cx53", "fsn1")] = main.stock[("cx53", "nbg1")] = main.stock[("cx53", "hel1")] = False
    async with WebClient() as web:
        web.webapp._bot = bot
        try:
            o = await web.call("GET", "/api/create/options?account=main")
            plans = {p["name"]: p for p in o["plans"]}
            assert plans["cx23"]["category"] == "cx" and plans["cax11"]["category"] == "cax"
            assert plans["cx53"]["in_stock"] == [] and "fsn1" in plans["cx53"]["offered"]
            assert o["panels"] == ["panel-b"] and any(i["arch"] == "arm" for i in o["images"])

            job = await web.finish(await web.call("POST", "/api/create", {
                "account": "main", "plan": "cx23", "location": "fsn1",
                "image": "ubuntu-24.04", "count": 2, "name": "web"}))
            assert job["ok"] and [x["name"] for x in job["result"]["servers"]] == ["web-1", "web-2"]
            assert all(x["password"] for x in job["result"]["servers"])   # no SSH keys
            assert "Servers created* (Mini App) — 2/2" in bot.sent[-1]
            await web.call("POST", "/api/create", {"account": "main", "plan": "cx23",
                           "location": "fsn1", "image": "ubuntu-24.04", "name": "web-1"},
                           status=400)                                      # name taken
            await web.call("POST", "/api/create", {"account": "main", "plan": "cx23",
                           "location": "fsn1", "image": "ubuntu-24.04", "name": "Bad_Name"},
                           status=400)
            await web.call("POST", "/api/create", {"account": "main", "plan": "cax11",
                           "location": "fsn1", "image": "debian-12"}, status=400)  # x86 image
            await web.call("POST", "/api/create", {"account": "main", "plan": "cx23",
                           "location": "fsn1", "image": "debian-12", "panel": "panel-b"},
                           status=400)                                      # node: Ubuntu only

            job = await web.finish(await web.call("POST", "/api/create", {
                "account": "main", "plan": "cx23", "location": "nbg1",
                "image": "ubuntu-24.04", "name": "node1", "panel": "panel-b"}))
            assert job["ok"] and [j["name"] for j in node_setup.list_jobs()] == ["node1"]
            sid = job["result"]["servers"][0]["id"]
            assert "NODE_API_KEY" in main.user_data[sid]

            a = await web.call("POST", "/api/create/alert", {
                "account": "main", "plan": "cx53", "locations": ["nbg1", "fsn1"],
                "image": "ubuntu-24.04", "ipv6": False})
            (c,) = create_watch.list_create_watches()
            assert c["id"] == a["id"] and c["locations"] == ["nbg1", "fsn1"] and c["name"] is None
            await web.call("POST", "/api/create/alert", {"account": "main", "plan": "cx53",
                           "locations": [], "image": "ubuntu-24.04"}, status=400)
            alerts = await web.call("GET", "/api/alerts")
            assert [x["id"] for x in alerts["creates"]] == [a["id"]]
            await web.call("DELETE", f"/api/create-alert/{a['id']}")
            assert create_watch.list_create_watches() == []
        finally:
            web.webapp._bot = None


@test
async def webapp_serves_https_and_sets_the_menu_button():
    import shutil as _sh
    import ssl
    import subprocess
    import aiohttp
    import webapp
    from config import Config
    from handlers.ui import _main_menu_keyboard
    fresh_world()

    class MenuBot:
        def __init__(self):
            self.buttons = []

        async def set_chat_menu_button(self, chat_id, menu_button):
            self.buttons.append((chat_id, menu_button))

    bot = MenuBot()
    assert await webapp.start(bot) is None                      # not configured
    assert bot.buttons[-1][1].type == "default"
    assert not any(b.web_app for row in _main_menu_keyboard("main") for b in row)
    if not _sh.which("openssl"):
        print("        (openssl missing: TLS part skipped)")
        return
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                    "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", "w.key", "-out", "w.pem",
                    "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)
    saved = (Config.WEBAPP_DOMAIN, Config.WEBAPP_PORT, Config.WEBAPP_CERT, Config.WEBAPP_KEY)
    Config.WEBAPP_DOMAIN, Config.WEBAPP_CERT, Config.WEBAPP_KEY = "localhost", "w.pem", "w.key"
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        Config.WEBAPP_PORT = sock.getsockname()[1]
    try:
        url = await webapp.start(bot)
        assert url == f"https://localhost:{Config.WEBAPP_PORT}/"
        chat, button = bot.buttons[-1]
        assert chat == ADMIN and button.web_app.url == url and button.text == "Panel"
        (row,) = [r for r in _main_menu_keyboard("main") if r[0].web_app]
        assert row[0].web_app.url == url
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
        async with aiohttp.ClientSession() as session:
            async with session.get(url, ssl=ctx) as resp:
                assert resp.status == 200 and 'lang="en"' in await resp.text()
            async with session.get(url + "api/servers", ssl=ctx) as resp:
                assert resp.status == 401
    finally:
        await webapp.stop()
        Config.WEBAPP_DOMAIN, Config.WEBAPP_PORT, Config.WEBAPP_CERT, Config.WEBAPP_KEY = saved
        webapp._bot = None


@test
async def webapp_manages_floating_ips_and_volumes():
    main, second, _chat = fresh_world()
    a = main.add_server("alpha", "cx23", "fsn1")
    b = main.add_server("beta", "cx23", "fsn1")
    other = second.add_server("gamma", "cx23", "fsn1")
    async with WebClient() as web:
        r = await web.call("POST", "/api/ips/fip", {"account": "main", "type": "ipv4",
                                                     "location": "fsn1", "count": 2})
        assert r["ok"] and [x["command"] for x in r["made"]] == [
            "sudo ip addr add 10.8.0.1 dev eth0", "sudo ip addr add 10.8.0.2 dev eth0"], r
        await web.call("POST", "/api/ips/fip", {"account": "main", "type": "ipv4",
                                                 "location": "fsn1", "count": 9}, status=400)
        await web.call("POST", "/api/ips/fip", {"account": "main", "type": "ipv4",
                                                 "location": "mars", "count": 1}, status=400)
        await web.call("POST", "/api/ips/fip", {"account": "nope", "type": "ipv4",
                                                 "location": "fsn1"}, status=400)
        d = await web.call("GET", "/api/ips/fip")
        assert [(i["ip"], i["account"], i["server_id"]) for i in d["ips"]] == [
            ("10.8.0.1", "main", None), ("10.8.0.2", "main", None)]
        assert {s["name"] for s in d["servers"]} == {"alpha", "beta", "gamma"}
        f1, f2 = (i["id"] for i in d["ips"])

        n = await web.call("GET", f"/api/server/{a}/network")
        assert len(n["floating"]) == 2 and n["volumes"] == [] and n["panels"] == ["panel-b"]
        r = await web.call("POST", f"/api/server/{a}/fip", {"fip_id": f1, "action": "assign"})
        assert r["command"] == "sudo ip addr add 10.8.0.1 dev eth0"
        assert main.floating_ips[0]["server"] == a
        await web.call("POST", f"/api/server/{b}/fip", {"fip_id": f1, "action": "unassign"},
                       status=400)                                  # not on this server
        await web.call("POST", f"/api/server/{other}/fip", {"fip_id": f1, "action": "assign"},
                       status=404)                                  # other account's IP
        await web.call("POST", f"/api/ips/fip/main/{f1}/assign", {"server_id": b})
        assert main.floating_ips[0]["server"] == b                  # moved
        await web.call("POST", f"/api/ips/fip/main/{f1}/assign", {"server_id": other},
                       status=400)
        await web.call("POST", f"/api/ips/fip/main/{f1}/assign", {"server_id": None})
        assert main.floating_ips[0]["server"] is None

        r = await web.call("POST", f"/api/server/{a}/fip/new", {"type": "ipv6"})
        assert r["command"].endswith("::1/64 dev eth0"), r
        assert main.floating_ips[-1]["server"] == a
        assert main.floating_ips[-1]["home_location"]["name"] == "fsn1"
        await web.call("DELETE", f"/api/ips/fip/main/{f2}")
        assert f2 not in [f["id"] for f in main.floating_ips]
        await web.call("DELETE", f"/api/ips/fip/main/{f2}", status=404)
        new = main.floating_ips[-1]                                 # the IPv6 on alpha
        await web.call("DELETE", f"/api/server/{other}/fip/{new['id']}", status=404)
        await web.call("DELETE", f"/api/server/{a}/fip/{new['id']}")
        assert new not in main.floating_ips
        assert ("POST", f"/floating_ips/{new['id']}/actions/unassign", None) in main.calls

        r = await web.call("POST", "/api/ips/pip", {"account": "main", "type": "ipv4",
                                                     "location": "nbg1", "count": 1})
        pip = main.primary_ips[0]
        assert r["made"][0]["ip"] == pip["ip"] and pip["location"]["name"] == "nbg1"
        pip["assignee_id"] = a
        err = await web.call("DELETE", f"/api/ips/pip/main/{pip['id']}", status=502)
        assert "assigned" in err["error"], err
        pip["assignee_id"] = None
        await web.call("DELETE", f"/api/ips/pip/main/{pip['id']}")
        assert main.primary_ips == []

        await web.call("POST", f"/api/server/{a}/volume", {"size": 5}, status=400)
        r = await web.call("POST", f"/api/server/{a}/volume", {"size": 20})
        (vol,) = main.volumes
        assert vol["server"] == a and vol["size"] == 20 and r["device"] == vol["linux_device"]
        n = await web.call("GET", f"/api/server/{b}/network")
        assert n["volumes"] == []                                   # attached elsewhere
        await web.call("POST", f"/api/server/{a}/volume/{vol['id']}", {"action": "detach"})
        n = await web.call("GET", f"/api/server/{b}/network")
        assert [v["attached"] for v in n["volumes"]] == [False]     # free in fsn1
        await web.call("POST", f"/api/server/{b}/volume/{vol['id']}", {"action": "attach"})
        assert vol["server"] == b
        await web.call("POST", f"/api/server/{a}/volume/{vol['id']}", {"action": "attach"},
                       status=400)                                  # attached to beta
        await web.call("DELETE", f"/api/server/{b}/volume/{vol['id']}")
        assert main.volumes == []                                   # detached, then deleted


@test
async def webapp_manages_a_server():
    main, _second, _chat = fresh_world()
    bot = Bot()
    sid = main.add_server("alpha", "cx23", "fsn1")
    async with WebClient() as web:
        web.webapp._bot = bot
        try:
            m = await web.call("GET", f"/api/server/{sid}/manage")
            assert [i["name"] for i in m["images"]] == ["debian-12", "ubuntu-24.04"]   # x86 only
            assert (m["backups"], m["list_price"], m["price_override"]) == (False, 3.49, None)

            await web.call("POST", f"/api/server/{sid}/rename", {"name": "-bad"}, status=400)
            await web.call("POST", f"/api/server/{sid}/rename", {"name": "alpha2"})
            assert main.servers[sid]["name"] == "alpha2"

            await web.call("POST", f"/api/server/{sid}/price", {"price": "4,51"})
            assert (await web.call("GET", f"/api/server/{sid}/manage"))["price_override"] == 4.51
            assert (await web.call("GET", f"/api/server/{sid}"))["price"] == 4.51
            await web.call("POST", f"/api/server/{sid}/price", {"price": "abc"}, status=400)
            await web.call("POST", f"/api/server/{sid}/price", {"price": None})
            assert (await web.call("GET", f"/api/server/{sid}/manage"))["price_override"] is None

            await web.call("POST", f"/api/server/{sid}/backups", {"on": True})
            assert main.servers[sid]["backup_window"]
            await web.call("POST", f"/api/server/{sid}/backups", {"on": False})
            assert not main.servers[sid]["backup_window"]

            r = await web.call("POST", f"/api/server/{sid}/password")
            assert r["password"] == "FakePass123" and "summary" not in r
            assert "FakePass123" in bot.sent[-1] and "Root password reset" in bot.sent[-1]

            await web.call("POST", f"/api/server/{sid}/snapshot")
            (snap,) = main.snapshots
            assert snap["created_from"]["id"] == sid
            d = await web.call("GET", "/api/snapshots")
            assert [(x["id"], x["account"], x["server_exists"]) for x in d["snapshots"]] == [
                (snap["id"], "main", True)]
            assert [x["id"] for x in (await web.call("GET", f"/api/server/{sid}/manage"))["snapshots"]] == [snap["id"]]
            snap["protection"] = {"delete": True}
            await web.call("DELETE", f"/api/snapshot/main/{snap['id']}")
            assert main.snapshots == []                                     # unprotected first
            await web.call("DELETE", f"/api/snapshot/main/{snap['id']}", status=404)

            await web.call("POST", f"/api/server/{sid}/rebuild", {"image": "nope"}, status=400)
            r = await web.call("POST", f"/api/server/{sid}/rebuild", {"image": "debian-12"})
            assert r["ok"] and r["password"] is None and "SSH keys installed" in bot.sent[-1]

            orig = node_setup.fetch_node_cert, node_setup.register_node
            seen = []

            async def no_cert(ip, *a, **k):
                return None

            async def cert(ip, *a, **k):
                return FAKE_PEM

            async def register(*args):
                seen.append(args)
                return True, "77", False
            try:
                key = node_setup.new_api_key()
                await web.call("POST", f"/api/server/{sid}/node", {"panel": "panel-b", "api_key": "x"},
                               status=400)
                await web.call("POST", f"/api/server/{sid}/node", {"panel": "unknown-panel", "api_key": key},
                               status=400)
                node_setup.fetch_node_cert = no_cert
                await web.call("POST", f"/api/server/{sid}/node", {"panel": "panel-b", "api_key": key},
                               status=502)
                node_setup.fetch_node_cert, node_setup.register_node = cert, register
                r = await web.call("POST", f"/api/server/{sid}/node", {"panel": "panel-b", "api_key": key})
                assert r["node_id"] == "77" and seen[0][0:2] == ("panel-b", "alpha2") and seen[0][4] == key
                assert "Node added" in bot.sent[-1]
            finally:
                node_setup.fetch_node_cert, node_setup.register_node = orig

            reset_schedule.add_schedule("main", sid, "alpha2", ADMIN, "d", 0, 4, 0)
            plan_watch.add_watch("main", "fsn1", "cx33", sid, ADMIN)
            await web.call("POST", f"/api/server/{sid}/delete", {"name": "alpha"}, status=400)
            import server_manager
            server_manager._busy.add(sid)
            try:
                await web.call("POST", f"/api/server/{sid}/delete", {"name": "alpha2"}, status=409)
                await web.call("POST", f"/api/server/{sid}/rebuild", {"image": "debian-12"}, status=409)
            finally:
                server_manager._busy.discard(sid)
            await web.call("POST", f"/api/server/{sid}/delete", {"name": "alpha2"})
            assert sid not in main.servers and "Server deleted" in bot.sent[-1]
            assert reset_schedule.list_schedules() == [] and plan_watch.list_watches() == []
        finally:
            web.webapp._bot = None


@test
async def webapp_cost_report_matches_the_bot():
    main, _second, chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1", traffic_tb=21)
    main.servers[sid]["backup_window"] = "22-02"
    main.add_server("beta", "cx33", "nbg1")
    await main.create_snapshot(sid, "s")
    await main.create_volume("v", 10, sid)
    await main.create_floating_ip("ipv4", "fsn1", "f")
    async with WebClient() as web:
        c = await web.call("GET", "/api/cost")
    acc = {a["account"]: a for a in c["accounts"]}
    assert acc["second"]["empty"]
    m = acc["main"]
    assert (m["server_cost"], m["backup_count"], m["backup_cost"]) == (8.98, 1, 0.7), m
    assert (m["snapshot_cost"], m["volume_cost"], m["floating_count"]) == (0.02, 0.44, 1), m
    assert m["overage"] == 1.19 and m["total"] == c["total"], m
    assert [r["name"] for r in m["servers"]] == ["alpha", "beta"] and m["servers"][0]["backups"]
    await chat.press("useacct_overage_cost_main")
    assert f"💰 Total: €{m['total']:.2f}" in chat.last.text, chat.last.text


@test
async def webapp_auto_reset_settings():
    main, _, _chat = fresh_world()
    sid = main.add_server("alpha", "cx23", "fsn1", traffic_tb=10)
    async with WebClient() as web:
        a = (await web.call("GET", "/api/alerts"))["auto_reset"]
        assert a["enabled"] and a["time"] == "05:00" and a["threshold"] == 90
        assert a["servers"] == [{"id": sid, "name": "alpha", "account": "main",
                                 "included": True, "status": "running", "fraction": 0.5,
                                 "time": "05:00", "own_time": False}]
        r = await web.call("POST", "/api/auto-reset", {"hour": "4", "minute": "30", "threshold": 85})
        assert r["time"] == "04:30" and r["threshold"] == 85 and r["next_text"]
        await web.call("POST", "/api/auto-reset", {"threshold": 20}, status=400)
        await web.call("POST", "/api/auto-reset", {"hour": 25}, status=400)
        r = await web.call("POST", "/api/auto-reset", {"enabled": False})
        assert not r["enabled"] and r["next_text"] is None
        r = await web.call("POST", f"/api/server/{sid}/auto-reset", {"on": False})
        assert r["included"] is False
        d = await web.call("GET", f"/api/server/{sid}")
        assert d["auto_reset"]["included"] is False and d["auto_reset"]["time"] == "04:30"
        await web.call("POST", "/api/server/999999/auto-reset", {"on": True}, status=404)
        page = await web.call("GET", "/")
        assert "autoResetSection" in page


@test
async def webapp_health_pause_running_and_report():
    main, second, _chat = fresh_world()
    import webapp
    sent = []

    class TgBot:
        async def send_message(self, chat_id, text, parse_mode=None, **_):
            assert not markdown_problem(text), text
            sent.append(text)
    webapp._bot = TgBot()
    sid = main.add_server("alpha", "cx23", "fsn1")
    second.access = (403, "forbidden", "project locked")
    await health.check_health(Bot())
    try:
        async with WebClient() as web:
            d = await web.call("GET", "/api/dashboard")
            assert d["problems"] == [{"account": "second", "status": "forbidden",
                                      "label": "access refused (403)"}] and d["paused"] is None
            h = await web.call("GET", "/api/health")
            assert [a["status"] for a in h["accounts"]] == ["ok", "forbidden"]
            assert h["accounts"][1]["message"] == "project locked" and h["accounts"][0]["rate_remaining"] == 3500
            assert h["report"]["time"] == "09:00" and h["pause_hours"] == [1, 6, 24]
            r = await web.call("POST", "/api/automation", {"pause_hours": 6})
            assert r["paused"] and "paused until" in r["pause_text"] and automation.is_paused()
            assert (await web.call("GET", "/api/dashboard"))["paused"]
            assert (await web.call("GET", "/api/alerts"))["paused"]
            r = await web.call("POST", "/api/automation", {"pause_hours": 0})
            assert automation.state()["until"] is None
            await web.call("POST", "/api/automation", {"pause_hours": -1}, status=400)
            r = await web.call("POST", "/api/automation", {"resume": True})
            assert not r["paused"] and not automation.is_paused()
            r = await web.call("POST", "/api/health/report", {"hour": 7, "minute": 30, "enabled": False})
            assert r["report"]["time"] == "07:30" and not r["report"]["enabled"]
            await web.call("POST", "/api/health/report", {"hour": 30}, status=400)
            r = await web.call("POST", "/api/health/report/send")
            assert r == {"ok": True} and "Daily check" in sent[-1] and "access refused" in sent[-1]

            assert server_manager.claim(sid, "plan", "Plan change to cx33")
            main.actions.append({"id": 5, "command": "change_type", "status": "running",
                                 "progress": 60, "started": None, "finished": None,
                                 "resources": [{"id": sid, "type": "server"}], "error": None})
            r = await web.call("GET", "/api/running")
            assert r["bot"][0]["name"] == "alpha" and r["bot"][0]["title"] == "Plan change to cx33"
            assert r["hetzner"] == [{"account": "main", "id": 5, "server_id": sid, "name": "alpha",
                                     "command": "change_type", "label": "plan change",
                                     "progress": 60, "minutes": None}]
            assert (await web.call("GET", "/api/dashboard"))["activity"] == 1
            server_manager.release(sid)
            page = await web.call("GET", "/")
            assert "renderHealth" in page and "renderRunning" in page and "changeIpSheet" in page
    finally:
        webapp._bot = None


@test
async def webapp_protection_ipv4_keep_and_clone():
    main, _, _chat = fresh_world()
    import webapp
    sent = []

    class TgBot:
        async def send_message(self, chat_id, text, parse_mode=None, **_):
            assert not markdown_problem(text), text
            sent.append(text)
    webapp._bot = TgBot()
    sid = main.add_server("web", "cx33", "fsn1")
    old = main.ip_of(sid)
    try:
        async with WebClient() as web:
            # Protection: shown, and delete / rebuild refused while it is on.
            await web.call("POST", f"/api/server/{sid}/protection", {"on": True})
            assert (await web.call("GET", f"/api/server/{sid}"))["protected"] is True
            assert (await web.call("GET", f"/api/server/{sid}/manage"))["protected"] is True
            err = await web.call("POST", f"/api/server/{sid}/delete", {"name": "web"}, status=409)
            assert "Protection is on" in err["error"]
            await web.call("POST", f"/api/server/{sid}/rebuild", {"image": "debian-12"}, status=409)
            await web.call("POST", f"/api/server/{sid}/protection", {"on": False})

            # A new IPv4, keeping the old one; then it is offered for a new server.
            n = await web.call("GET", f"/api/server/{sid}/network")
            assert n["kept_ips"] == [] and n["busy"] is False
            j = await web.call("POST", f"/api/server/{sid}/ipv4", {"keep_old": True})
            j = await web.finish(j)
            assert j["ok"] and j["result"]["old_ip"] == old and j["result"]["kept"], j
            assert main.ip_of(sid) == j["result"]["new_ip"] != old
            assert "IPv4 change" in sent[-1]
            n = await web.call("GET", f"/api/server/{sid}/network")
            (kept,) = n["kept_ips"]
            assert kept["ip"] == old
            # Back to the kept one, deleting the other.
            j = await web.finish(await web.call("POST", f"/api/server/{sid}/ipv4", {"use_ip": kept["id"]}))
            assert j["ok"] and main.ip_of(sid) == old
            await web.call("POST", f"/api/server/{sid}/ipv4", {"use_ip": "x"}, status=400)

            # Snapshot → new server (plan too small refused), on a kept IP.
            await web.call("POST", f"/api/server/{sid}/snapshot")
            snap = main.snapshots[-1]
            o = await web.call("GET", "/api/create/options?account=main")
            assert o["snapshots"][0]["id"] == snap["id"] and o["snapshots"][0]["disk"] == 80
            r = await web.call("POST", f"/api/server/{sid}/delete", {"name": "web", "keep_ip": True})
            assert r["kept_ip"] == old and sid not in main.servers
            assert "was kept" in sent[-1]
            o = await web.call("GET", "/api/create/options?account=main")
            assert [k["ip"] for k in o["kept_ips"]] == [old] and o["kept_ips"][0]["location"] == "fsn1"
            err = await web.call("POST", "/api/create", {"account": "main", "plan": "cx23",
                                 "location": "fsn1", "image": str(snap["id"])}, status=400)
            assert "smaller than the snapshot" in err["error"]
            await web.call("POST", "/api/create", {"account": "main", "plan": "cx33", "location": "nbg1",
                           "image": str(snap["id"]), "ipv4_id": o["kept_ips"][0]["id"]}, status=400)
            await web.call("POST", "/api/create", {"account": "main", "plan": "cx33", "location": "fsn1",
                           "image": str(snap["id"]), "count": 2, "ipv4_id": o["kept_ips"][0]["id"]},
                           status=400)
            j = await web.finish(await web.call("POST", "/api/create", {
                "account": "main", "plan": "cx33", "location": "fsn1", "image": str(snap["id"]),
                "ipv4_id": o["kept_ips"][0]["id"]}))
            (made,) = j["result"]["servers"]
            assert made["ok"] and made["name"].startswith("web-clone-") and made["ip"] == old
            assert main.servers[made["id"]]["image"] == str(snap["id"])

            # Own auto reset time from the server page.
            r = await web.call("POST", f"/api/server/{made['id']}/auto-reset", {"hour": 2, "minute": 10})
            assert r["server_time"] == "02:10" and r["own_time"]
            a = await web.call("GET", "/api/alerts")
            assert a["auto_reset"]["servers"][0]["time"] == "02:10"
            r = await web.call("POST", f"/api/server/{made['id']}/auto-reset", {"default_time": True})
            assert r["server_time"] == "05:00" and not r["own_time"]
            await web.call("POST", f"/api/server/{made['id']}/auto-reset", {"hour": 30, "minute": 0},
                           status=400)
    finally:
        webapp._bot = None


def _git_repo_with_versions():
    """origin (bare) + a clone like the server's, bot at bot/hetzner (any folder works); origin is one
    version ahead. Returns (clone's bot dir, old commit)."""
    import subprocess
    import tempfile as _tf
    root = _tf.mkdtemp(prefix="upd-", dir=".")
    git = lambda *a, cwd=root: subprocess.run(  # noqa: E731
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *a],
        cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "--bare", "origin.git")
    git("clone", "-q", "origin.git", "work")
    work = os.path.join(root, "work")
    bot = os.path.join(work, "bot", "hetzner")
    os.makedirs(bot)

    def release(version, note):
        open(os.path.join(bot, "VERSION"), "w").write(version + "\n")
        old = open(os.path.join(bot, "CHANGELOG.md")).read() if os.path.exists(
            os.path.join(bot, "CHANGELOG.md")) else "# changes\n"
        head, _, rest = old.partition("\n")
        open(os.path.join(bot, "CHANGELOG.md"), "w").write(f"{head}\n\n## {version}\n- {note}\n{rest}")
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", version, cwd=work)
        git("push", "-q", "origin", "HEAD:main", cwd=work)
        return git("rev-parse", "HEAD", cwd=work)

    old = release("1.0.0", "first")
    release("1.1.0", "second")
    git("reset", "-q", "--hard", old, cwd=work)       # the server still runs 1.0.0
    return bot, old


@test
async def version_and_changelog_files_agree():
    import updater
    here = os.path.dirname(os.path.abspath(updater.__file__))
    version = open(os.path.join(here, "VERSION")).read().strip()
    assert updater.parse_version(version) > (0,), version
    changelog = open(os.path.join(here, "CHANGELOG.md")).read()
    sections = [l[3:].split()[0] for l in changelog.splitlines() if l.startswith("## ")]
    assert sections and sections[0] == version, (sections[:1], version)   # bump both together
    assert sections == sorted(sections, key=updater.parse_version, reverse=True)
    assert updater.is_newer("1.0.10", "1.0.9") and not updater.is_newer("1.0.0", "1.0.0")
    log = "# x\n\n## 1.2.0\n- c\n\n## 1.1.0\n- b\n\n## 1.0.0\n- a\n"
    assert updater.changes_since(log, "1.0.0") == "## 1.2.0\n- c\n\n## 1.1.0\n- b"


@test
async def updater_fetches_starts_and_reports():
    import shutil as _sh
    import time as _time
    import updater
    import webapp
    if not _sh.which("git"):
        print("        (git missing: skipped)")
        return
    main, _, chat = fresh_world()
    bot = Bot()
    saved_dir, saved_run = updater.BOT_DIR, updater._systemd_run
    started = []
    try:
        updater.BOT_DIR = updater.Path(os.path.abspath(_git_repo_with_versions()[0]))
        bot_dir = updater.BOT_DIR
        open(bot_dir / "hetzner-bot.sh", "w").write("#!/bin/bash\n")
        updater._systemd_run = started.append
        assert updater.current_version() == "1.0.0"
        latest, changelog, error = updater.fetch_latest()
        assert (latest, error) == ("1.1.0", "") and "- second" in updater.changes_since(changelog, "1.0.0")
        assert updater.rollback_version() is None
        assert "No earlier version" in updater.start("rollback", ADMIN)

        # The bot's panel: new version, changes, update → confirm → started.
        await chat.press("start_menu")
        assert chat.last.markup.find("Update · v1.0.0") == "upd"
        await chat.press("upd")
        assert "New: `1.1.0`" in chat.last.text and "second" in chat.last.text
        await chat.press(chat.last.markup.find("Update to 1.1.0"))
        await chat.press(chat.last.markup.find("Yes"))
        assert "Update started" in chat.last.text, chat.last.text
        assert len(started) == 1 and started[0].endswith("update > '%s' 2>&1" % (bot_dir / ".update.log"))
        assert "already running" in updater.start("update", ADMIN)

        # The script finished (exit 0) and the bot came back on 1.1.0: one report.
        open(bot_dir / "VERSION", "w").write("1.1.0\n")
        open(bot_dir / ".update_result", "w").write(f"0 {int(_time.time()) + 1}")
        import handlers.update as upd
        await asyncio.gather(*upd._tasks)
        await updater.report_when_done(bot)                # already reported: nothing
        assert not bot.sent
        assert any("Update done" in s.text and "1.0.0 → 1.1.0" in s.text for s in chat.screens)

        # A failed rollback reports the log tail.
        open(bot_dir / ".update_prev", "w").write(updater._git("rev-parse", "HEAD").stdout)
        assert updater.rollback_version() == "1.0.0"
        assert updater.start("rollback", ADMIN) == ""
        open(bot_dir / ".update.log", "w").write("\x1b[31mgit reset failed\x1b[0m")
        open(bot_dir / ".update_result", "w").write(f"1 {int(_time.time()) + 1}")
        await updater.report_when_done(bot)
        assert "Rollback failed" in bot.sent[-1] and "git reset failed" in bot.sent[-1]

        # New-version notice: once per version (the server runs 1.0.0 again).
        # Checked every minute; only a commit not checked yet is fetched.
        open(bot_dir / "VERSION", "w").write("1.0.0\n")
        assert updater.CHECK_INTERVAL_SECONDS == 60
        origin_head = updater._git("ls-remote", "origin", "refs/heads/main").stdout.split()[0]
        assert updater.remote_head() == origin_head
        updater._last_check.update(head=None, full=0.0)
        (bot_dir / ".update_request").write_text(updater.json.dumps({"chat_id": ADMIN, "time": _time.time()}))
        await updater.check_for_update(bot, ADMIN)          # an update is running: wait
        assert updater._last_check["head"] is None
        (bot_dir / ".update_request").unlink()
        fetches = []
        real_fetch = updater.fetch_latest
        updater.fetch_latest = lambda: fetches.append(1) or real_fetch()
        try:
            await updater.check_for_update(bot, ADMIN)
            await updater.check_for_update(bot, ADMIN)
            assert len(fetches) == 1, fetches
            saved_head = updater.remote_head
            updater.remote_head = lambda: None              # ls-remote refused: one full check
            updater._last_check.update(head=None, full=0.0)
            await updater.check_for_update(bot, ADMIN)
            await updater.check_for_update(bot, ADMIN)
            updater.remote_head = saved_head
            assert len(fetches) == 2, fetches
        finally:
            updater.fetch_latest = real_fetch
        notices = [t for t in bot.sent if "New version" in t]
        assert len(notices) == 1 and "1.0.0 → 1.1.0" in notices[0], bot.sent

        # Mini App: version card data and start.
        async with WebClient() as web:
            web.webapp._bot = None
            v = await web.call("GET", "/api/version")
            assert v["newer"] and v["latest"] == "1.1.0" and v["rollback"] == "1.0.0"
            await web.call("POST", "/api/update", {"action": "nope"}, status=400)
            (bot_dir / ".update_request").unlink(missing_ok=True)
            assert await web.call("POST", "/api/update", {"action": "update"}) == {"ok": True}
            await web.call("POST", "/api/update", {"action": "update"}, status=409)
            assert (await web.call("GET", "/api/dashboard"))["version"] == "1.0.0"
    finally:
        updater.BOT_DIR, updater._systemd_run = saved_dir, saved_run
        webapp._bot = None


async def run():
    failed = 0
    for fn in TESTS:
        try:
            await fn()
            print(f"  ok    {fn.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run()) else 0)
