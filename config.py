import os
import re
from dotenv import load_dotenv

load_dotenv()


class Config:
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
    DEBUG_MODE = os.getenv('DEBUG_MODE', 'false').lower() == 'true'

    HETZNER_API_BASE = 'https://api.hetzner.cloud/v1'
    TRAFFIC_LIMIT_TB = 20
    TRAFFIC_LIMIT_BYTES = TRAFFIC_LIMIT_TB * 1024 ** 4

    # Traffic alerts (monitor.py): share of the included traffic.
    WARNING_THRESHOLD = 0.75
    CRITICAL_THRESHOLD = 0.98

    # € per TB of extra traffic, used only when Hetzner's plan prices do not
    # carry price_per_tb_traffic for the server's location.
    OVERAGE_PRICE_PER_TB = 1.0

    # PasarGuard node setup (node_setup.py). NODE_SETUP_URL is the raw URL of your
    # node setup script (add a token if its repo is private); new nodes download it
    # from cloud-init. Both features stay hidden until it and a panel are set.
    NODE_SETUP_URL = os.getenv('NODE_SETUP_URL', '').strip()

    # Mini App (webapp.py): served by the bot itself over HTTPS on
    # https://WEBAPP_DOMAIN:WEBAPP_PORT/ with the domain's certificate (certbot,
    # set up by `hetzner webapp <domain>`). Off while the domain or port is unset.
    WEBAPP_DOMAIN = os.getenv('WEBAPP_DOMAIN', '').strip().lower()
    WEBAPP_PORT = int(os.getenv('WEBAPP_PORT', '0') or 0)
    WEBAPP_CERT = os.getenv('WEBAPP_CERT', '').strip() or (
        f'/etc/letsencrypt/live/{WEBAPP_DOMAIN}/fullchain.pem' if WEBAPP_DOMAIN else '')
    WEBAPP_KEY = os.getenv('WEBAPP_KEY', '').strip() or (
        f'/etc/letsencrypt/live/{WEBAPP_DOMAIN}/privkey.pem' if WEBAPP_DOMAIN else '')

    @classmethod
    def webapp_url(cls):
        """The Mini App's address, or '' while it is not configured."""
        if not (cls.WEBAPP_DOMAIN and cls.WEBAPP_PORT):
            return ''
        return f'https://{cls.WEBAPP_DOMAIN}:{cls.WEBAPP_PORT}/'

    @classmethod
    def _parse_panels(cls) -> dict:
        # PANELS=main,backup, then PANEL_<NAME>_URL / _USERNAME / _PASSWORD and an
        # optional _CORE_ID (default: the panel's first core config); a '-' in the name
        # is '_' in the keys (PANEL_MY_PANEL_URL for "my-panel").
        panels = {}
        for name in os.getenv('PANELS', '').split(','):
            name = name.strip().lower()
            if not re.fullmatch(r'[a-z0-9-]{1,20}', name):
                continue
            prefix = f'PANEL_{name.upper().replace("-", "_")}_'
            url = os.getenv(prefix + 'URL', '').strip().rstrip('/')
            username = os.getenv(prefix + 'USERNAME', '').strip()
            password = os.getenv(prefix + 'PASSWORD', '')
            if not (url and username and password):
                continue
            if url.endswith('/api'):
                url = url[:-len('/api')]
            core = os.getenv(prefix + 'CORE_ID', '').strip()
            panels[name] = {
                'url': url,
                'username': username,
                'password': password,
                'core_id': int(core) if core.isdigit() else None,
            }
        return panels

    PANELS: dict = {}

    @classmethod
    def _parse_accounts(cls) -> dict:
        # Multi-account format: HETZNER_ACCOUNTS=name1:token1,name2:token2
        raw = os.getenv('HETZNER_ACCOUNTS', '').strip()
        if raw:
            accounts = {}
            for entry in raw.split(','):
                entry = entry.strip()
                if not entry:
                    continue
                if ':' in entry:
                    name, token = entry.split(':', 1)
                    accounts[name.strip()] = token.strip()
                else:
                    accounts[f'account{len(accounts) + 1}'] = entry
            if accounts:
                return accounts
        # Backward compat: HETZNER_API_TOKEN — supports both single token
        # and comma-separated list (e.g. token1,token2,token3)
        raw_token = os.getenv('HETZNER_API_TOKEN', '').strip()
        if raw_token:
            tokens = [t.strip() for t in raw_token.split(',') if t.strip()]
            if len(tokens) == 1:
                return {'default': tokens[0]}
            return {f'account{i + 1}': t for i, t in enumerate(tokens)}
        return {}

    HETZNER_ACCOUNTS: dict = {}

    @classmethod
    def validate(cls):
        if not cls.TELEGRAM_TOKEN:
            raise ValueError("TELEGRAM_TOKEN is required in .env")
        if not cls.HETZNER_ACCOUNTS:
            raise ValueError("HETZNER_ACCOUNTS or HETZNER_API_TOKEN is required in .env")
        if not cls.ADMIN_ID:
            raise ValueError("ADMIN_ID is required in .env")


Config.HETZNER_ACCOUNTS = Config._parse_accounts()
Config.PANELS = Config._parse_panels()
Config.validate()
