import logging
import json
from datetime import datetime
from pathlib import Path
from config import Config
from hetzner_api import api_registry
from overage_tracker import get_overage_tracker
from utils import format_traffic, get_traffic_emoji, included_traffic_tb, overage_cost

logger = logging.getLogger(__name__)

STATE_FILE = Path('monitor_state.json')


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.error(f"Failed to save monitor state: {e}")


async def traffic_monitor(bot):
    logger.info("Running hourly traffic monitor check...")
    today = datetime.now().strftime('%Y-%m-%d')
    state = _load_state()
    warning_pct = Config.WARNING_THRESHOLD * 100
    critical_pct = Config.CRITICAL_THRESHOLD * 100

    for account_name in api_registry.names():
        api = api_registry.get(account_name)
        tracker = get_overage_tracker(account_name)
        account_label = f"[{account_name}] " if len(api_registry.names()) > 1 else ""

        try:
            servers = await api.list_servers()
            if not servers:
                logger.warning(f"No servers found for account '{account_name}' during monitor check")
                continue

            for server in servers:
                server_id = str(server.get('id'))
                server_name = server.get('name', 'Unnamed')
                traffic_bytes = server.get('outgoing_traffic', 0)
                traffic_tb = traffic_bytes / (1024 ** 4)
                limit_tb = included_traffic_tb(server)
                usage_pct = (traffic_tb / limit_tb) * 100 if limit_tb else 0
                usage_text = f"{format_traffic(traffic_bytes, limit_tb)} ({usage_pct:.1f}%)"
                emoji = get_traffic_emoji(traffic_tb, limit_tb)

                tracker.update_live_overage(server_id, overage_cost(server))

                state_key = f"{account_name}/{server_id}"
                if state_key not in state:
                    state[state_key] = {
                        'warned_75': False,
                        'last_critical_date': '',
                        'last_over_date': '',
                    }

                s = state[state_key]

                if usage_pct >= 100:
                    if s.get('last_over_date') != today:
                        msg = (
                            f"🔥 *TRAFFIC LIMIT EXCEEDED*\n\n"
                            f"{account_label}Server: `{server_name}`\n"
                            f"{emoji} Traffic: {usage_text}\n\n"
                            f"You are being charged for overage!\n"
                            f"Reset traffic immediately to stop charges."
                        )
                        await _send(bot, msg)
                        s['last_over_date'] = today

                elif usage_pct >= critical_pct:
                    if s.get('last_critical_date') != today:
                        msg = (
                            f"🚨 *CRITICAL TRAFFIC ALERT*\n\n"
                            f"{account_label}Server: `{server_name}`\n"
                            f"{emoji} Traffic: {usage_text}\n\n"
                            f"⚠️ Traffic limit almost exhausted!\n"
                            f"Consider resetting traffic to avoid overage charges."
                        )
                        await _send(bot, msg)
                        s['last_critical_date'] = today

                elif usage_pct >= warning_pct:
                    if not s.get('warned_75'):
                        msg = (
                            f"⚠️ *TRAFFIC WARNING*\n\n"
                            f"{account_label}Server: `{server_name}`\n"
                            f"{emoji} Traffic: {usage_text}\n\n"
                            f"Traffic usage has exceeded {warning_pct:g}% of the monthly limit."
                        )
                        await _send(bot, msg)
                        s['warned_75'] = True

                else:
                    if s.get('warned_75'):
                        s['warned_75'] = False
                        logger.info(f"Server {server_name} dropped below {warning_pct:g}%, warning reset.")

        except Exception as e:
            logger.error(f"Error in traffic monitor for account '{account_name}': {e}")

    _save_state(state)
    logger.info("Hourly traffic monitor check completed")


async def _send(bot, message: str):
    try:
        await bot.send_message(chat_id=Config.ADMIN_ID, text=message, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Failed to send alert: {e}")
