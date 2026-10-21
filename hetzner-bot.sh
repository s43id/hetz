#!/usr/bin/env bash
# Interactive management CLI for the Hetzner Server Manager Bot.
# install.sh installs this as `hetzner`, so you can run it from anywhere.
# Subcommands also work non-interactively:
#   hetzner install|update|rollback|uninstall|start|stop|restart|status|logs|env|tz|webapp

SERVICE="hetzner-bot"
UNIT="/etc/systemd/system/$SERVICE.service"
REPO_URL="https://github.com/s43id/hetz.git"
REPO_DEFAULT="/opt/hetzner"
BOT_SUBDIR=""

# Where the bot lives. install.sh copies this script to /usr/local/bin, so
# its own location says nothing; the installed service knows the real folder.
SCRIPT_PATH="$(readlink -f "$0" 2>/dev/null || echo "$0")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
UNIT_DIR=$(sed -n 's/^WorkingDirectory=//p' "$UNIT" 2>/dev/null | head -n1)
if [ -n "$UNIT_DIR" ] && [ -f "$UNIT_DIR/main.py" ]; then
    DIR="$UNIT_DIR"
elif [ -f "$SCRIPT_DIR/main.py" ]; then
    DIR="$SCRIPT_DIR"
else
    DIR="$REPO_DEFAULT"
fi

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; PLAIN='\033[0m'

need_root() {
    if [ "$(id -u)" -ne 0 ]; then
        echo -e "${RED}This action needs root. Run with sudo.${PLAIN}"
        exit 1
    fi
}

svc_state() {
    if [ ! -f "/etc/systemd/system/$SERVICE.service" ]; then
        echo "not installed"
    else
        systemctl is-active "$SERVICE" 2>/dev/null || true
    fi
}

header() {
    clear
    local ver state st
    ver=$(git -C "$DIR" log -1 --format='%h %cd' --date=short 2>/dev/null || echo "-")
    state=$(svc_state)
    case "$state" in
        active)          st="${GREEN}● running${PLAIN}" ;;
        inactive|failed) st="${RED}● $state${PLAIN}" ;;
        *)               st="${YELLOW}● $state${PLAIN}" ;;
    esac
    echo -e "${CYAN}╔════════════════════════════════════════╗${PLAIN}"
    echo -e "${CYAN}║      Hetzner Server Manager Bot        ║${PLAIN}"
    echo -e "${CYAN}╚════════════════════════════════════════╝${PLAIN}"
    echo -e "  Status:  $st"
    echo -e "  Version: ${CYAN}$(bot_version)${PLAIN} ($ver)"
    echo -e "  Path:    $DIR"
    echo
}

do_install() {
    need_root
    if [ ! -f "$DIR/main.py" ]; then
        echo "⬇️  Cloning repository to $REPO_DEFAULT..."
        apt-get update -qq
        apt-get install -y -qq git
        git clone "$REPO_URL" "$REPO_DEFAULT" || return
        DIR="$REPO_DEFAULT"
    fi
    bash "$DIR/install.sh"
}

bot_version() { cat "$DIR/VERSION" 2>/dev/null || echo "?"; }

# pip, service unit, CLI, restart; 0 when the bot is running afterwards.
apply_and_restart() {
    cd "$DIR" || return 1
    ./venv/bin/pip install --quiet -r requirements.txt || { echo -e "${RED}pip install failed.${PLAIN}"; return 1; }
    # keep the service unit in sync with the repo template,
    # preserving the timezone the user picked with `hetzner tz`
    cur_tz=$(grep -E '^Environment=TZ=' "/etc/systemd/system/$SERVICE.service" 2>/dev/null | cut -d= -f3)
    sed "s|__DIR__|$DIR|g" hetzner-bot.service > "/etc/systemd/system/$SERVICE.service"
    if [ -n "$cur_tz" ]; then
        sed -i "s|^Environment=TZ=.*|Environment=TZ=$cur_tz|" "/etc/systemd/system/$SERVICE.service"
    fi
    install -m 755 hetzner-bot.sh /usr/local/bin/hetzner
    rm -f /usr/local/bin/hetzner-bot
    systemctl daemon-reload
    systemctl restart "$SERVICE"
    sleep 3
    if [ "$(systemctl is-active "$SERVICE")" = "active" ]; then
        echo -e "${GREEN}✅ Version $(bot_version) ($(git log -1 --format=%h)) is running.${PLAIN}"
        return 0
    fi
    echo -e "${RED}❌ Service is not active — check the logs (hetzner logs).${PLAIN}"
    return 1
}

do_update() {
    need_root
    if ! git -C "$DIR" rev-parse --git-dir >/dev/null 2>&1; then
        echo -e "${RED}Bot is not installed in $DIR. Use Install first.${PLAIN}"
        return 1
    fi
    cd "$DIR" || return 1
    # Pull the branch that is checked out (main).
    local branch before
    branch=$(git rev-parse --abbrev-ref HEAD)
    before=$(git rev-parse HEAD)
    echo "⬇️  Pulling the latest version of $branch (now $(bot_version))..."
    git pull origin "$branch" || { echo -e "${RED}git pull failed.${PLAIN}"; return 1; }
    # The commit we came from is what `hetzner rollback` returns to.
    [ "$(git rev-parse HEAD)" != "$before" ] && echo "$before" > "$DIR/.update_prev"
    apply_and_restart
}

do_rollback() {
    need_root
    cd "$DIR" || return 1
    local prev
    prev=$(cat "$DIR/.update_prev" 2>/dev/null)
    if [ -z "$prev" ] || ! git cat-file -e "$prev^{commit}" 2>/dev/null; then
        echo -e "${RED}No earlier version recorded (it is kept from the next update on).${PLAIN}"
        return 1
    fi
    echo "↩️  Going back from $(bot_version) to $(git show "$prev:$(git rev-parse --show-prefix)VERSION" 2>/dev/null || echo "$prev")..."
    # Tracked files only: .env, venv and the JSON data are untracked and stay.
    # `hetzner update` later pulls main forward again.
    git reset --hard "$prev" || return 1
    apply_and_restart
}

# For the in-bot update: "<exit code> <unix time>" once the action has finished.
record_result() {
    echo "$1 $(date +%s)" > "$DIR/.update_result"
}

do_uninstall() {
    need_root
    read -r -p "Remove the bot service? [y/N] " a
    case "$a" in [Yy]*) ;; *) echo "Cancelled."; return ;; esac
    systemctl disable --now "$SERVICE" 2>/dev/null
    rm -f "/etc/systemd/system/$SERVICE.service"
    systemctl daemon-reload
    rm -f /usr/local/bin/hetzner /usr/local/bin/hetzner-bot
    echo -e "${GREEN}Service and 'hetzner' command removed.${PLAIN}"
    # The bot folder is part of a git checkout; delete only what install and
    # use created in it, never the code.
    read -r -p "Also DELETE the bot's data in $DIR (.env, venv, alerts, cost history)? [y/N] " b
    case "$b" in
        [Yy]*)
            rm -rf "$DIR/venv" "$DIR/.env" "$DIR"/plan_watches.json "$DIR"/create_watches.json \
                   "$DIR"/monitor_state.json "$DIR"/price_overrides.json "$DIR"/overage_history*.json \
                   "$DIR"/node_setups.json "$DIR"/reset_schedules.json "$DIR"/update_state.json \
                   "$DIR"/.update_prev "$DIR"/.update_result "$DIR"/.update_request "$DIR"/.update.log
            echo "Data deleted; the code is still in $DIR." ;;
        *)  echo "Data kept." ;;
    esac
}

do_start()   { need_root; systemctl start "$SERVICE";   echo -e "${GREEN}Started.${PLAIN}"; }
do_stop()    { need_root; systemctl stop "$SERVICE";    echo -e "${YELLOW}Stopped.${PLAIN}"; }
do_restart() { need_root; systemctl restart "$SERVICE"; echo -e "${GREEN}Restarted.${PLAIN}"; }

do_status() {
    systemctl --no-pager status "$SERVICE"
}

do_logs() {
    echo -e "${CYAN}Live logs — press Ctrl+C to exit.${PLAIN}"
    journalctl -u "$SERVICE" -n 50 -f
}

do_env() {
    need_root
    ${EDITOR:-nano} "$DIR/.env"
    read -r -p "Restart the bot to apply changes? [Y/n] " a
    case "$a" in [Nn]*) ;; *) systemctl restart "$SERVICE"; echo "Restarted." ;; esac
}

do_tz() {
    need_root
    local unit="/etc/systemd/system/$SERVICE.service"
    if [ ! -f "$unit" ]; then
        echo -e "${RED}Bot service is not installed yet.${PLAIN}"
        return
    fi
    local cur tz
    cur=$(grep -E '^Environment=TZ=' "$unit" | cut -d= -f3)
    echo -e "Current bot timezone: ${CYAN}${cur:-system default}${PLAIN}"
    if [ -n "$1" ]; then
        tz="$1"
    else
        read -r -p "New timezone [Asia/Tehran]: " tz
        tz="${tz:-Asia/Tehran}"
    fi
    if [ ! -f "/usr/share/zoneinfo/$tz" ]; then
        echo -e "${RED}Unknown timezone: $tz${PLAIN} (e.g. Asia/Tehran, Europe/Berlin, UTC)"
        return
    fi
    if grep -qE '^Environment=TZ=' "$unit"; then
        sed -i "s|^Environment=TZ=.*|Environment=TZ=$tz|" "$unit"
    else
        sed -i "/^\[Service\]/a Environment=TZ=$tz" "$unit"
    fi
    systemctl daemon-reload
    systemctl restart "$SERVICE"
    echo -e "${GREEN}✅ Bot timezone set to $tz — all times in the bot now use it.${PLAIN}"
}

set_env() {
    # set_env KEY VALUE: replace or append KEY=VALUE in .env
    local env="$DIR/.env"
    touch "$env"; chmod 600 "$env"
    if grep -qE "^$1=" "$env"; then
        sed -i "s|^$1=.*|$1=$2|" "$env"
    else
        echo "$1=$2" >> "$env"
    fi
}

ufw_allow() {
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
        ufw allow "$1/tcp" >/dev/null && echo "  ufw: opened $1/tcp"
    fi
}

port80_unit() {
    # The systemd unit of whatever listens on TCP 80, '' when nothing does,
    # '?' when the owner is not a plain service.
    local pid unit
    pid=$(ss -ltnpH 'sport = :80' 2>/dev/null | grep -o 'pid=[0-9]*' | head -n1 | cut -d= -f2)
    if [ -z "$pid" ]; then
        ss -ltnH 'sport = :80' 2>/dev/null | grep -q . && echo "?"
        return
    fi
    unit=$(grep -o '[^/]*\.service$' "/proc/$pid/cgroup" 2>/dev/null | head -n1)
    echo "${unit:-?}"
}

do_webapp() {
    # Mini App: certificate for the subdomain (certbot, standalone on port 80),
    # a random port kept in .env, then a restart. The bot serves the page itself.
    need_root
    local domain="$1" port
    if [ -z "$domain" ]; then
        local cur
        cur=$(sed -n 's/^WEBAPP_DOMAIN=//p' "$DIR/.env" 2>/dev/null | head -n1)
        read -r -p "Mini App subdomain${cur:+ [$cur]}: " domain
        domain="${domain:-$cur}"
    fi
    domain=$(echo "$domain" | tr 'A-Z' 'a-z' | sed 's|^https\?://||; s|[:/].*||')
    if ! echo "$domain" | grep -qE '^[a-z0-9.-]+\.[a-z]{2,}$'; then
        echo -e "${RED}Not a domain: '$domain'${PLAIN}"; return
    fi
    echo "1) The A record of $domain must point to this server (Cloudflare: DNS only, grey cloud)."
    echo "   This server's public IPv4: $(hostname -I 2>/dev/null | awk '{print $1}')"
    command -v certbot >/dev/null 2>&1 || { apt-get update -qq; apt-get install -y -qq certbot; }
    ufw_allow 80
    # certbot answers Let's Encrypt on port 80 for a few seconds. If a web
    # server already holds it, stop that service just for that moment (and at
    # each renewal, about every two months) and start it again right after.
    local hooks=() unit
    unit=$(port80_unit)
    if [ -n "$unit" ]; then
        echo -e "${YELLOW}Port 80 is in use:${PLAIN}"
        ss -ltnp 'sport = :80' 2>/dev/null | tail -n +2
        case "$unit" in
            "?"|docker.service|containerd.service|"$SERVICE.service")
                echo -e "${RED}It is not a service I can stop briefly ($unit), e.g. a Docker container.${PLAIN}"
                echo "Free port 80 for a minute (stop that program), run this again, then start it."
                return 1 ;;
        esac
        echo -e "It belongs to ${CYAN}$unit${PLAIN}. It would be stopped for a few seconds now"
        echo "and at each certificate renewal (about every 2 months), then started again."
        read -r -p "Stop $unit briefly for the certificate? [y/N] " a
        case "$a" in [Yy]*) ;; *) echo "Cancelled."; return 1 ;; esac
        hooks=(--pre-hook "systemctl stop $unit" --post-hook "systemctl start $unit")
    fi
    echo "2) Getting a certificate..."
    if ! certbot certonly --standalone -d "$domain" --non-interactive --agree-tos \
            --register-unsafely-without-email --keep-until-expiring \
            --deploy-hook "systemctl restart $SERVICE" "${hooks[@]}"; then
        echo -e "${RED}certbot failed: check the DNS record and that port 80 is open (also in the Hetzner Cloud Firewall).${PLAIN}"
        return
    fi
    port=$(sed -n 's/^WEBAPP_PORT=//p' "$DIR/.env" 2>/dev/null | head -n1)
    if [ -z "$port" ]; then
        for _ in $(seq 1 50); do
            local p
            p=$(shuf -i 20000-60999 -n 1)
            if ! ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$p\$"; then port=$p; break; fi
        done
    fi
    set_env WEBAPP_DOMAIN "$domain"
    set_env WEBAPP_PORT "$port"
    ufw_allow "$port"
    systemctl restart "$SERVICE"
    sleep 3
    echo -e "${GREEN}✅ Mini App: https://$domain:$port/${PLAIN}"
    echo "   Open the bot in Telegram: the menu button «پنل» (or 📱 Open App in /start) opens it."
    echo "   If a Hetzner Cloud Firewall is attached, allow TCP $port and 80 there."
    echo "   The certificate renews by itself (certbot timer); the bot restarts after each renewal."
}

case "$1" in
    install)          do_install;   exit 0 ;;
    update)           do_update; rc=$?; record_result $rc; exit $rc ;;
    rollback)         do_rollback; rc=$?; record_result $rc; exit $rc ;;
    uninstall|remove) do_uninstall; exit 0 ;;
    start)            do_start;     exit 0 ;;
    stop)             do_stop;      exit 0 ;;
    restart)          do_restart;   exit 0 ;;
    status)           do_status;    exit 0 ;;
    logs)             do_logs;      exit 0 ;;
    env)              do_env;       exit 0 ;;
    tz|timezone)      do_tz "$2";   exit 0 ;;
    webapp|miniapp)   do_webapp "$2"; exit 0 ;;
    "") ;;  # no argument: open the interactive menu
    *)
        echo "Usage: hetzner [install|update|rollback|uninstall|start|stop|restart|status|logs|env|tz|webapp <domain>]"
        exit 1
        ;;
esac

while true; do
    header
    echo "  1) 📥 Install              6) 🔁 Restart"
    echo "  2) ⬆️  Update               7) 📊 Status"
    echo "  3) 🗑  Uninstall            8) 📜 Logs (live)"
    echo "  4) ▶️  Start                9) ⚙️  Edit .env"
    echo "  5) ⏹  Stop                10) 🕒 Timezone"
    echo "                            11) 📱 Mini App"
    echo "                            12) ↩️  Roll back last update"
    echo "                             0) 🚪 Exit"
    echo
    read -r -p "  Select an option: " choice
    echo
    case "$choice" in
        1) do_install ;;
        2) do_update ;;
        3) do_uninstall ;;
        4) do_start ;;
        5) do_stop ;;
        6) do_restart ;;
        7) do_status ;;
        8) do_logs ;;
        9) do_env ;;
        10) do_tz ;;
        11) do_webapp ;;
        12) do_rollback ;;
        0) exit 0 ;;
        *) echo -e "${RED}Invalid option.${PLAIN}" ;;
    esac
    echo
    read -r -p "  Press Enter to continue..." _
done
