"""Offline test doubles for the Hetzner bot.

FakeHetzner replaces one account's HetznerAPI. It subclasses the real client
and overrides only ``_request``, so every real method (pagination, caching,
error handling, the 410 "gone" memo) runs unchanged against an in-memory
Hetzner Cloud. Chat drives the real handlers through ``button_handler`` with
fake Telegram objects and records every screen the bot shows.

Nothing here talks to Telegram or Hetzner. dev/run_tests.py sets up the
environment (fake .env values, a scratch working directory) before importing
the bot and these doubles.
"""
import itertools
import re
from types import SimpleNamespace

from hetzner_api import HetznerAPI

TB = 1024 ** 4

# --------------------------------------------------------------- catalogue

LOCATIONS = [
    {"id": 1, "name": "fsn1", "city": "Falkenstein", "country": "DE"},
    {"id": 2, "name": "nbg1", "city": "Nuremberg", "country": "DE"},
    {"id": 3, "name": "hel1", "city": "Helsinki", "country": "FI"},
    {"id": 4, "name": "ash", "city": "Ashburn, VA", "country": "US"},
]

# name: (id, cores, memory GB, disk GB, architecture, monthly €)
PLANS = {
    "cx23": (101, 2, 4, 40, "x86", 3.49),
    "cx33": (102, 4, 8, 80, "x86", 5.49),
    "cx43": (103, 8, 16, 160, "x86", 9.49),
    "cx53": (104, 16, 32, 320, "x86", 17.49),
    "cpx22": (111, 2, 4, 80, "x86", 6.49),
    "cpx32": (112, 4, 8, 160, "x86", 10.99),
    "cax11": (121, 2, 4, 40, "arm", 3.79),
    "cax21": (122, 4, 8, 80, "arm", 6.49),
}

IMAGES = [
    {"id": 1, "name": "ubuntu-24.04", "description": "Ubuntu 24.04", "architecture": "x86",
     "type": "system", "deprecated": None, "status": "available"},
    {"id": 2, "name": "debian-12", "description": "Debian 12", "architecture": "x86",
     "type": "system", "deprecated": None, "status": "available"},
    {"id": 3, "name": "ubuntu-24.04", "description": "Ubuntu 24.04", "architecture": "arm",
     "type": "system", "deprecated": None, "status": "available"},
]


# Shared by every fake account: real Hetzner ids are unique across accounts,
# and _resolve_server relies on that.
_IDS = itertools.count(1000)


def _error(code, message):
    return {"error": {"code": code, "message": message}}


def _server_loc(server):
    return (server.get("datacenter") or {}).get("location", server.get("location") or {})["name"]


class FakeHetzner(HetznerAPI):
    """In-memory Hetzner Cloud for one account.

    Knobs a test can turn:
      stock            {(plan, location): bool} — the `available` flag
      refuse_change    {plan: n} — refuse the next n change_type calls to plan
                       for capacity (n=None: always)
      refuse_create    {(plan, location)} — creates there are refused for capacity
      lose_create_reply  True: the next create succeeds but the reply is lost
      user_data          {server_id: cloud-init script} of servers created with one
      gone             paths answering 410 (default: /datacenters, as on the real API)
      access           (status, code, message) that check_access sees (200 = fine)
      actions          server actions /servers/actions lists ({id, command, status,
                       progress, started, finished, resources, error})

    Every server gets a primary IPv4 (``server_ips``, listed with ``primary_ips``),
    deleted with the server unless its ``auto_delete`` is off.
    """

    def __init__(self, account="main"):
        super().__init__(token=f"fake-{account}")
        self.account = account
        self.servers = {}
        self.ssh_keys = []
        self.floating_ips = []
        self.primary_ips = []
        self.volumes = []
        self.snapshots = []
        self.stock = {(p, l["name"]): True for p in PLANS for l in LOCATIONS[:3]}
        self.refuse_change = {}
        self.refuse_create = set()
        self.lose_create_reply = False
        self.user_data = {}
        self.gone_paths = {"/datacenters"}
        self.calls = []          # (method, endpoint, data) in call order
        self._ids = _IDS
        self.server_ips = []     # the primary IPv4 of each server
        self._pip_n = itertools.count(1)
        self.access = (200, "", "")
        self.refuse_pip_assign = 0   # refuse the next n primary IP assigns
        self.actions = []
        self.rate_limit, self.rate_remaining = 3600, 3500

    # -------------------------------------------------- test-data helpers

    def server_type(self, name):
        tid, cores, mem, disk, arch, price = PLANS[name]
        return {
            "id": tid, "name": name, "cores": cores, "memory": float(mem), "disk": disk,
            "architecture": arch, "deprecation": None,
            # Ashburn costs more and, like some real payloads, carries no
            # per-TB overage price (the bot then falls back to its default).
            "prices": [
                {"location": l["name"],
                 "price_monthly": {"net": f"{price / 1.19:.4f}",
                                   "gross": f"{price * (1.2 if l['name'] == 'ash' else 1):.4f}"},
                 **({} if l["name"] == "ash" else
                    {"price_per_tb_traffic": {"net": "1.0000", "gross": "1.1900"}})}
                for l in LOCATIONS
            ],
            "locations": [
                {"id": l["id"], "name": l["name"],
                 "available": self.stock.get((name, l["name"]), False),
                 "recommended": False, "deprecation": None}
                for l in LOCATIONS[:3]
            ],
        }

    def add_server(self, name, plan="cx23", location="fsn1", traffic_tb=0.0,
                   included_tb=20, status="running", nested_location=True, disk=None):
        sid = next(self._ids)
        loc = next(l for l in LOCATIONS if l["name"] == location)
        pip = {"id": next(self._ids), "ip": f"10.0.0.{sid % 250}", "type": "ipv4",
               "name": f"primary_ip-{sid}", "assignee_id": sid, "assignee_type": "server",
               "auto_delete": True, "location": {"name": location}}
        self.server_ips.append(pip)
        server = {
            "id": sid, "name": name, "status": status,
            "server_type": self.server_type(plan),
            "public_net": {"ipv4": {"id": pip["id"], "ip": pip["ip"]}, "ipv6": None},
            "outgoing_traffic": int(traffic_tb * TB),
            "included_traffic": int(included_tb * TB),
            "primary_disk_size": disk or PLANS[plan][3],
            "backup_window": None,
            "locked": False,
            "protection": {"delete": False, "rebuild": False},
        }
        if nested_location:
            server["datacenter"] = {"name": f"{location}-dc14", "location": dict(loc)}
        else:
            server["location"] = dict(loc)
        self.servers[sid] = server
        return sid

    def plan_of(self, sid):
        return self.servers[sid]["server_type"]["name"]

    def ip_of(self, sid):
        return ((self.servers[sid]["public_net"] or {}).get("ipv4") or {}).get("ip")

    def all_pips(self):
        return self.server_ips + self.primary_ips

    async def _probe(self, endpoint):
        self.calls.append(("GET", endpoint, None))
        status, code, message = self.access
        if status == 200:
            return 200, {"servers": []}, ""
        if status == 0:
            return 0, None, message or "timeout"
        return status, {"error": {"code": code, "message": message}}, ""

    # -------------------------------------------------- the fake transport

    async def _request(self, method, endpoint, data=None, retry=3, fresh=False,
                       return_error=False):
        self.calls.append((method, endpoint, data))
        path = endpoint.split("?")[0]
        if path in self._gone:
            return None
        if path in self.gone_paths:
            self._gone.add(path)
            return None
        result = self._route(method, path, endpoint, data or {})
        if isinstance(result, dict) and result.get("error") and not return_error:
            return None
        return result

    def _page(self, key, items):
        return {key: items, "meta": {"pagination": {"page": 1, "next_page": None}}}

    def _route(self, method, path, endpoint, data):
        if method == "GET" and path == "/servers/actions":
            return self._actions(endpoint)
        m = re.fullmatch(r"/servers/(\d+)/actions", path)
        if m and method == "GET":
            return self._actions(endpoint, int(m.group(1)))
        m = re.fullmatch(r"/servers/(\d+)(?:/actions/(\w+))?", path)
        if m:
            return self._server_route(method, int(m.group(1)), m.group(2), data)
        m = re.fullmatch(r"/(floating_ips|primary_ips|volumes|images)/(\d+)(?:/actions/(\w+))?", path)
        if m:
            return self._item_route(method, m.group(1), int(m.group(2)), m.group(3), data)
        if method == "GET":
            if path == "/servers":
                return self._page("servers", list(self.servers.values()))
            if path == "/server_types":
                return self._page("server_types", [self.server_type(p) for p in PLANS])
            if path == "/locations":
                return self._page("locations", LOCATIONS)
            if path == "/images":
                if "type=system" in endpoint:
                    return self._page("images", IMAGES)
                return self._page("images", self.snapshots)
            if path == "/pricing":
                return {"pricing": {
                    "image": {"price_per_gb_month": {"gross": "0.0119"}},
                    "volume": {"price_per_gb_month": {"gross": "0.0440"}},
                    "server_backup": {"percentage": "20.0"},
                    "floating_ips": [], "primary_ips": [],
                }}
            if path == "/primary_ips":
                return self._page("primary_ips", self.all_pips())
            for key in ("ssh_keys", "floating_ips", "volumes"):
                if path == f"/{key}":
                    return self._page(key, getattr(self, key))
            return _error("not_found", f"fake has no GET {path}")
        if method == "POST" and path == "/servers":
            return self._create_server(data)
        if method == "POST" and path == "/floating_ips":
            if data.get("home_location") not in [l["name"] for l in LOCATIONS]:
                return _error("invalid_input", "unknown home_location")
            n = len(self.floating_ips) + 1
            ip = {"id": next(self._ids), "name": data.get("name"), "type": data.get("type"),
                  "ip": f"10.8.0.{n}" if data.get("type") == "ipv4" else f"2a01:4f8:c0c:{n}::/64",
                  "server": None, "home_location": {"name": data["home_location"]}}
            self.floating_ips.append(ip)
            return {"floating_ip": ip}
        if method == "POST" and path == "/volumes":
            server = self.servers.get(data.get("server"))
            if server is None:
                return _error("invalid_input", "server not found")
            vid = next(self._ids)
            vol = {"id": vid, "name": data.get("name"), "size": data.get("size"),
                   "server": server["id"], "linux_device": f"/dev/disk/by-id/scsi-0HC_Volume_{vid}",
                   "location": {"name": _server_loc(server)}}
            self.volumes.append(vol)
            return {"volume": vol}
        if method == "POST" and path == "/primary_ips":
            if "location" not in data:
                return _error("invalid_input", "location is required")
            ip = {"id": next(self._ids), "ip": f"10.9.0.{next(self._pip_n)}",
                  "type": data.get("type"), "name": data.get("name"),
                  "assignee_id": None, "assignee_type": "server",
                  "auto_delete": bool(data.get("auto_delete")),
                  "location": {"name": data["location"]}}
            self.primary_ips.append(ip)
            return {"primary_ip": ip}
        return _error("not_found", f"fake has no {method} {path}")

    def _server_route(self, method, sid, action, data):
        server = self.servers.get(sid)
        if server is None:
            return _error("not_found", "server not found")
        if method == "GET" and action is None:
            return {"server": server}
        if method == "DELETE" and action is None:
            if server["protection"]["delete"]:
                return _error("protected", "server is delete protected")
            del self.servers[sid]
            for pip in list(self.all_pips()):
                if pip.get("assignee_id") == sid:
                    if pip.get("auto_delete"):
                        (self.server_ips if pip in self.server_ips else self.primary_ips).remove(pip)
                    else:
                        pip["assignee_id"] = None
            return {"action": {"id": next(self._ids), "status": "running"}}
        if method == "PUT" and action is None:
            server["name"] = data.get("name", server["name"])
            return {"server": server}
        if action == "poweroff":
            server["status"] = "off"
        elif action == "poweron":
            server["status"] = "running"
        elif action == "change_type":
            return self._change_type(server, data)
        elif action == "reset_password":
            return {"root_password": "FakePass123", "action": {"id": next(self._ids)}}
        elif action == "rebuild":
            if server["protection"]["rebuild"]:
                return _error("protected", "server is rebuild protected")
            return {"root_password": None, "action": {"id": next(self._ids)}}
        elif action == "change_protection":
            if bool(data.get("delete")) != bool(data.get("rebuild")):
                return _error("invalid_input", "delete and rebuild must be the same")
            server["protection"] = {"delete": bool(data.get("delete")),
                                    "rebuild": bool(data.get("rebuild"))}
        elif action in ("enable_backup", "disable_backup"):
            server["backup_window"] = "22-02" if action == "enable_backup" else None
        elif action == "create_image":
            self.snapshots.append({"id": next(self._ids), "type": "snapshot",
                                   "description": data.get("description"),
                                   "image_size": 1.5, "status": "available",
                                   "disk_size": server["primary_disk_size"],
                                   "architecture": server["server_type"]["architecture"],
                                   "created": "2026-10-10T12:00:00+00:00",
                                   "created_from": {"id": sid, "name": server["name"]},
                                   "protection": {"delete": False}})
        else:
            return _error("not_found", f"fake has no action {action}")
        return {"action": {"id": next(self._ids), "status": "running"}}

    def _actions(self, endpoint, server_id=None):
        status = re.search(r"status=(\w+)", endpoint)
        items = [a for a in self.actions
                 if (not status or a.get("status") == status.group(1))
                 and (server_id is None
                      or any(r.get("id") == server_id for r in a.get("resources") or []))]
        return self._page("actions", sorted(items, key=lambda a: -a["id"]))

    def _item_route(self, method, key, item_id, action, data):
        if key == "primary_ips":
            items = self.server_ips if any(i["id"] == item_id for i in self.server_ips) \
                else self.primary_ips
        else:
            items = self.snapshots if key == "images" else getattr(self, key)
        item = next((i for i in items if i["id"] == item_id), None)
        if item is None:
            return _error("not_found", f"{key[:-1]} not found")
        if method == "GET" and action is None:
            return {"image": item} if key == "images" else {key[:-1]: item}
        if method == "PUT" and action is None and key == "primary_ips":
            for field in ("name", "auto_delete"):
                if field in data:
                    item[field] = data[field]
            return {"primary_ip": item}
        if key == "primary_ips" and action in ("assign", "unassign"):
            return self._pip_action(item, action, data)
        if method == "DELETE" and action is None:
            if key == "images" and item.get("protection", {}).get("delete"):
                return _error("protected", "image is delete protected")
            if key == "primary_ips" and item.get("assignee_id"):
                return _error("must_be_unassigned", "primary IP is assigned")
            if key == "volumes" and item.get("server"):
                return _error("locked", "volume is attached")
            items.remove(item)
            return {}
        if action == "change_protection":
            item["protection"] = {"delete": bool(data.get("delete"))}
        elif action == "assign" and key == "floating_ips":
            if data.get("server") not in self.servers:
                return _error("invalid_input", "server not found")
            item["server"] = data["server"]
        elif action == "unassign" and key == "floating_ips":
            item["server"] = None
        elif action == "attach" and key == "volumes":
            server = self.servers.get(data.get("server"))
            if server is None or _server_loc(server) != item["location"]["name"]:
                return _error("invalid_input", "server not in the volume's location")
            item["server"] = server["id"]
        elif action == "detach" and key == "volumes":
            item["server"] = None
        else:
            return _error("not_found", f"fake has no {key} action {action}")
        return {"action": {"id": next(self._ids), "status": "running"}}

    def _pip_action(self, pip, action, data):
        """Primary IPv4 on / off a server; like Hetzner, the server must be off."""
        sid = pip.get("assignee_id") if action == "unassign" else data.get("assignee_id")
        server = self.servers.get(sid)
        if server is None:
            return _error("invalid_input", "server not found")
        if server["status"] != "off":
            return _error("server_not_stopped", "server must be stopped")
        if action == "unassign":
            pip["assignee_id"] = None
            server["public_net"]["ipv4"] = None
        else:
            if self.refuse_pip_assign:
                self.refuse_pip_assign -= 1
                return _error("resource_unavailable", "assign refused (test)")
            if pip.get("assignee_id"):
                return _error("conflict", "primary IP is already assigned")
            if server["public_net"].get("ipv4"):
                return _error("conflict", "server already has a primary IPv4")
            if pip["location"]["name"] != _server_loc(server):
                return _error("invalid_input", "primary IP is in another location")
            pip["assignee_id"] = sid
            server["public_net"]["ipv4"] = {"id": pip["id"], "ip": pip["ip"]}
        return {"action": {"id": next(self._ids), "status": "running"}}

    def _change_type(self, server, data):
        plan = data.get("server_type")
        if plan not in PLANS:
            return _error("invalid_input", f"unknown server type {plan}")
        if server["status"] != "off":
            return _error("server_not_stopped", "server must be stopped")
        if PLANS[plan][4] != server["server_type"]["architecture"]:
            return _error("invalid_input", "the new server type has another architecture")
        if PLANS[plan][3] < server["primary_disk_size"]:
            return _error("invalid_input", "not sufficient disk space for the new server type")
        if plan in self.refuse_change:
            left = self.refuse_change[plan]
            if left is None or left > 0:
                if left is not None:
                    self.refuse_change[plan] = left - 1
                return _error("resource_unavailable", "error during placement")
        server["server_type"] = self.server_type(plan)
        if data.get("upgrade_disk"):
            server["primary_disk_size"] = PLANS[plan][3]
        # A plan change resets the traffic counter on the real platform.
        server["outgoing_traffic"] = 0
        return {"action": {"id": next(self._ids), "status": "running"}}

    def _create_server(self, data):
        plan, location, name = data["server_type"], data["location"], data["name"]
        if any(s["name"] == name for s in self.servers.values()):
            return _error("uniqueness_error", "server name is already used")
        if (plan, location) in self.refuse_create:
            return _error("resource_unavailable", "error during placement")
        image = str(data.get("image"))
        if image.isdigit():
            snap = next((i for i in self.snapshots if str(i["id"]) == image), None)
            if snap is None:
                return _error("invalid_input", "image not found")
            if (snap.get("disk_size") or 0) > PLANS[plan][3]:
                return _error("invalid_input", "image disk is bigger than the server type's")
        elif not any(i["name"] == image and i["architecture"] == PLANS[plan][4] for i in IMAGES):
            return _error("invalid_input", "image not found for this architecture")
        kept = data.get("public_net", {}).get("ipv4")
        if kept:
            pip = next((p for p in self.all_pips() if p["id"] == kept), None)
            if pip is None or pip.get("assignee_id") or pip["location"]["name"] != location:
                return _error("invalid_input", "primary IP not usable here")
        sid = self.add_server(name, plan, location)
        self.servers[sid]["image"] = image
        if kept:
            own = self.servers[sid]["public_net"]["ipv4"]["id"]
            self.server_ips = [p for p in self.server_ips if p["id"] != own]
            pip["assignee_id"] = sid
            self.servers[sid]["public_net"]["ipv4"] = {"id": pip["id"], "ip": pip["ip"]}
        if data.get("user_data"):
            self.user_data[sid] = data["user_data"]
        if not data.get("public_net", {}).get("enable_ipv6", True):
            self.servers[sid]["public_net"]["ipv6"] = None
        if self.lose_create_reply:
            self.lose_create_reply = False
            return None
        password = None if data.get("ssh_keys") else "RootPass456"
        return {"server": self.servers[sid], "root_password": password}


# --------------------------------------------------------------- Telegram


class Markup:
    """Readable view of an InlineKeyboardMarkup."""

    def __init__(self, markup):
        rows = getattr(markup, "inline_keyboard", None) or []
        self.buttons = [(b.text, b.callback_data) for row in rows for b in row
                        if b.callback_data is not None]
        # Copy buttons carry no callback data: label -> the text they copy.
        self.copies = {b.text: b.copy_text.text for row in rows for b in row
                       if getattr(b, "copy_text", None)}

    def data(self):
        return [d for _, d in self.buttons]

    def find(self, needle):
        """callback_data of the first button whose label contains ``needle``."""
        for text, data in self.buttons:
            if needle in text:
                return data
        raise AssertionError(f"no button containing {needle!r}; have {[t for t, _ in self.buttons]}")


class Screen(SimpleNamespace):
    """One message the bot showed: text, markup and how it was shown."""


class FakeBot:
    def __init__(self, chat):
        self.chat = chat

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, **_):
        self.chat.record("send", text, reply_markup, parse_mode)
        return SimpleNamespace(message_id=next(self.chat.msg_ids), chat_id=chat_id)

    async def edit_message_text(self, text, chat_id=None, message_id=None, reply_markup=None,
                                parse_mode=None, **_):
        self.chat.record("edit", text, reply_markup, parse_mode)


class FakeMessage:
    def __init__(self, chat, text="", message_id=1):
        self.chat = chat
        self.text = text
        self.chat_id = chat.chat_id
        self.message_id = message_id

    async def reply_text(self, text, reply_markup=None, parse_mode=None, **_):
        self.chat.record("reply", text, reply_markup, parse_mode)
        return FakeMessage(self.chat, text, message_id=next(self.chat.msg_ids))

    async def edit_text(self, text, reply_markup=None, parse_mode=None, **_):
        self.chat.record("edit", text, reply_markup, parse_mode)

    async def delete(self):
        pass


class FakeQuery:
    def __init__(self, chat, data, user_id):
        self.chat = chat
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMessage(chat)
        self.answers = []

    async def answer(self, text=None, show_alert=False, **_):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None, **_):
        self.chat.record("edit", text, reply_markup, parse_mode)


class Chat:
    """The admin's chat: press buttons, send text, inspect what the bot showed."""

    def __init__(self, user_id, chat_id=None):
        self.user_id = user_id
        self.chat_id = chat_id or user_id
        self.screens = []
        self.msg_ids = itertools.count(100)
        self.bot = FakeBot(self)
        self.context = SimpleNamespace(user_data={}, bot=self.bot, error=None)

    def record(self, how, text, markup, parse_mode):
        if parse_mode == "Markdown":
            problem = markdown_problem(text)
            if problem:
                raise AssertionError(f"Telegram would reject this Markdown ({problem}):\n{text}")
        self.screens.append(Screen(how=how, text=text, markup=Markup(markup)))

    @property
    def last(self):
        return self.screens[-1]

    async def press(self, data, user_id=None):
        """Press a button through the real button_handler (auth, ack, routing)."""
        from handlers import button_handler
        query = FakeQuery(self, data, user_id or self.user_id)
        update = SimpleNamespace(callback_query=query, effective_user=query.from_user,
                                 effective_chat=SimpleNamespace(id=self.chat_id))
        before = len(self.screens)
        await button_handler(update, self.context)
        for s in self.screens[before:]:
            if s.text.startswith("⚠️ That button failed") or "not wired up" in s.text:
                raise AssertionError(s.text)
        return query

    def update_for_query(self, data):
        query = FakeQuery(self, data, self.user_id)
        return SimpleNamespace(callback_query=query, effective_user=query.from_user,
                               effective_chat=SimpleNamespace(id=self.chat_id))

    def update_for_text(self, text):
        return SimpleNamespace(message=FakeMessage(self, text),
                               effective_user=SimpleNamespace(id=self.user_id),
                               effective_chat=SimpleNamespace(id=self.chat_id),
                               callback_query=None)


def markdown_problem(text):
    """Why Telegram's legacy Markdown parser would reject ``text``, or None.

    Legacy Markdown has no nesting: inside `code` and ```pre``` nothing is
    parsed; elsewhere * and _ open an entity that must close, and a backslash
    escapes the next character.
    """
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if text.startswith("```", i):
            end = text.find("```", i + 3)
            if end < 0:
                return "unclosed ```"
            i = end + 3
            continue
        if ch in "`*_":
            end = text.find(ch, i + 1)
            if end < 0:
                return f"unclosed {ch} at {i}: {text[max(0, i - 20):i + 20]!r}"
            if ch != "`" and "`" in text[i + 1:end]:
                return f"code inside {ch} at {i} (nested entity)"
            i = end + 1
            continue
        i += 1
    return None
