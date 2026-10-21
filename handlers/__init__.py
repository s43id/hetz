"""Telegram callback handlers, organised by feature.

Importing this package registers every callback route on the shared
``router`` (each feature module calls ``router.add(...)`` at import time).
``main.py`` wires ``button_handler`` into python-telegram-bot, which then
dispatches every button press through the router.

Public entry points (start command, conversation entry points, and the
server-detail view reused by the SSH console) are re-exported here so the
rest of the project can keep importing them from ``handlers``.
"""
from .router import router, button_handler

# Importing the feature modules registers their routes as a side effect.
from . import menu           # noqa: F401
from . import servers        # noqa: F401
from . import cost           # noqa: F401
from . import snapshots      # noqa: F401
from . import rebuild        # noqa: F401
from . import resize         # noqa: F401
from . import volumes        # noqa: F401
from . import ips            # noqa: F401
from . import create_server  # noqa: F401
from . import watches        # noqa: F401
from . import nodes          # noqa: F401
from . import schedules      # noqa: F401
from . import auto_reset     # noqa: F401
from . import health         # noqa: F401
from . import ip_change      # noqa: F401
from . import commands       # noqa: F401
from . import update         # noqa: F401

from .menu import start_handler
from .commands import COMMAND_HANDLERS, set_bot_commands
from .servers import (
    _start_console,
    show_server_detail,
    _start_rename,
    _start_price,
    recv_price,
    clear_price,
    cancel_price,
    WAIT_PRICE,
    recv_rename,
    cancel_rename,
    WAIT_RENAME,
)
from .nodes import (
    _start_node_key,
    recv_node_key,
    cancel_node_key,
    WAIT_NODE_KEY,
)
from .create_server import (
    _start_server_name,
    recv_server_name,
    cancel_server_name,
    WAIT_SERVER_NAME,
)

__all__ = [
    "router",
    "button_handler",
    "start_handler",
    "COMMAND_HANDLERS",
    "set_bot_commands",
    "_start_console",
    "show_server_detail",
    "_start_server_name",
    "recv_server_name",
    "cancel_server_name",
    "WAIT_SERVER_NAME",
    "_start_rename",
    "recv_rename",
    "cancel_rename",
    "WAIT_RENAME",
    "_start_price",
    "recv_price",
    "clear_price",
    "cancel_price",
    "WAIT_PRICE",
    "_start_node_key",
    "recv_node_key",
    "cancel_node_key",
    "WAIT_NODE_KEY",
]
