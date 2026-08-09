#!/usr/bin/env bash
#
# One-command server setup for the Angel One intraday engine.
#
#   curl -fsSL https://raw.githubusercontent.com/vijaysonavane20-hash/AutomateX-Live/claude/angle-broking-intraday-script-csqgae/deploy/setup-vps.sh | bash
#
# or, if the repo is already cloned:
#
#   bash deploy/setup-vps.sh
#
# What it does: sets the clock to IST, installs Python and git, clones or
# updates the repo, builds a virtualenv, copies the config templates, and
# installs a systemd service + timer that starts the engine each weekday
# morning.
#
# What it deliberately does NOT do:
#   * write credentials — you fill in .env yourself
#   * enable live trading — the unit runs in paper mode; going live needs a
#     separate, explicit edit
#   * start the timer — you enable it once you have seen a paper run work
#
# Safe to re-run. Existing .env and config.yaml are never overwritten.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/vijaysonavane20-hash/AutomateX-Live.git}"
BRANCH="${BRANCH:-claude/angle-broking-intraday-script-csqgae}"
APP_DIR="${APP_DIR:-$HOME/AutomateX-Live}"
SERVICE_NAME="angel-trading"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; BLUE=$'\033[0;34m'; NC=$'\033[0m'

step()  { echo -e "\n${BLUE}==>${NC} ${1}"; }
ok()    { echo -e "  ${GREEN}OK${NC}  ${1}"; }
warn()  { echo -e "  ${YELLOW}!!${NC}  ${1}"; }
die()   { echo -e "\n${RED}FAILED${NC}: ${1}\n" >&2; exit 1; }

# ---------------------------------------------------------------- pre-flight

step "Checking the environment"

[ "$(id -u)" -eq 0 ] && die "Do not run this as root. Use a normal user with sudo —
       the trading service should not run with root privileges."

command -v sudo >/dev/null 2>&1 || die "sudo is required but not installed."

if ! command -v apt-get >/dev/null 2>&1; then
    die "This script targets Debian/Ubuntu. On another distro, follow
       docs/DEPLOY.md manually."
fi
ok "Debian/Ubuntu detected, running as '$(whoami)'"

# ------------------------------------------------------------------ timezone

step "Setting the clock to IST"
# TOTP login fails on a drifting clock, and the systemd timer fires on the
# server's local time — both depend on this being right.
current_tz="$(timedatectl show --property=Timezone --value 2>/dev/null || echo unknown)"
if [ "$current_tz" != "Asia/Kolkata" ]; then
    sudo timedatectl set-timezone Asia/Kolkata
    ok "Timezone changed from ${current_tz} to Asia/Kolkata"
else
    ok "Already Asia/Kolkata"
fi
sudo timedatectl set-ntp true 2>/dev/null || warn "Could not enable NTP sync — check the clock manually."
echo "      $(date '+%Y-%m-%d %H:%M:%S %Z')"

# ------------------------------------------------------------------ packages

step "Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip git curl >/dev/null
ok "python3 $(python3 --version 2>&1 | cut -d' ' -f2), git $(git --version | cut -d' ' -f3)"

py_minor="$(python3 -c 'import sys; print(sys.version_info.minor)')"
[ "$py_minor" -lt 10 ] && die "Python 3.10+ is required, found 3.${py_minor}."

# ---------------------------------------------------------------------- code

step "Fetching the code"
if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch origin "$BRANCH" --quiet
    git -C "$APP_DIR" checkout "$BRANCH" --quiet
    git -C "$APP_DIR" pull origin "$BRANCH" --quiet
    ok "Updated existing checkout at $APP_DIR"
else
    git clone --branch "$BRANCH" --quiet "$REPO_URL" "$APP_DIR"
    ok "Cloned to $APP_DIR"
fi
cd "$APP_DIR"

# ----------------------------------------------------------------- virtualenv

step "Building the Python environment"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements-trading.txt
ok "Dependencies installed into $APP_DIR/.venv"

# --------------------------------------------------------------------- config

step "Preparing config files"
if [ -f .env ]; then
    ok ".env already exists — left untouched"
else
    cp .env.example .env
    ok ".env created from template (still empty)"
fi
chmod 600 .env

if [ -f config.yaml ]; then
    ok "config.yaml already exists — left untouched"
else
    cp config.example.yaml config.yaml
    ok "config.yaml created from template"
fi

mkdir -p state logs

# -------------------------------------------------------------------- systemd

step "Installing the systemd service and timer"

sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<UNIT
[Unit]
Description=Angel One intraday trading engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(whoami)
Group=$(id -gn)
WorkingDirectory=${APP_DIR}

# PAPER MODE. To go live, append --live here AND set confirm_live: true in
# config.yaml, then run: sudo systemctl daemon-reload
ExecStart=${APP_DIR}/.venv/bin/python run_trading.py --config config.yaml

# Only restart on abnormal exit — a clean exit means the session finished at
# square-off, and restarting then would relaunch the engine after hours.
Restart=on-failure
RestartSec=30s
StartLimitBurst=3
StartLimitIntervalSec=600

# SIGTERM triggers the engine's square-off path; give it time to finish.
KillSignal=SIGTERM
TimeoutStopSec=120

StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT

sudo tee "/etc/systemd/system/${SERVICE_NAME}.timer" >/dev/null <<'UNIT'
[Unit]
Description=Start the Angel One intraday engine on trading mornings

[Timer]
# 09:10 IST — before the 09:15 open, leaving time to log in, download the
# instrument master, screen the universe and warm up indicators.
OnCalendar=Mon-Fri 09:10
Persistent=false
Unit=angel-trading.service

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
ok "Service and timer installed (paper mode, timer not yet enabled)"

# --------------------------------------------------------------------- report

public_ip="$(curl -fsS --max-time 10 ifconfig.me 2>/dev/null || echo 'could not detect')"

cat <<SUMMARY

$(echo -e "${GREEN}")=======================================================================
 SERVER READY
=======================================================================$(echo -e "${NC}")

  Location      : ${APP_DIR}
  Python        : ${APP_DIR}/.venv/bin/python
  Public IP     : ${public_ip}

$(echo -e "${YELLOW}")  THIS IP MUST BE REGISTERED WITH ANGEL$(echo -e "${NC}")
  Go to https://smartapi.angelone.in/new/apps , click the pencil icon,
  and put the IP above into "Secondary Static IP".
  Remember: Angel allows an IP change only once per week.

  NEXT STEPS
  ----------
  1. Fill in your credentials:
         nano ${APP_DIR}/.env

  2. Set your capital (the cash you will actually deploy):
         nano ${APP_DIR}/config.yaml

  3. Test the connection (places no orders):
         cd ${APP_DIR} && ./.venv/bin/python run_trading.py --check

  4. Backtest on real history:
         cd ${APP_DIR} && ./.venv/bin/python -m trading.backtest --days 60

  5. Once --check passes, enable the daily timer:
         sudo systemctl enable --now ${SERVICE_NAME}.timer
         systemctl list-timers ${SERVICE_NAME}.timer

  USEFUL COMMANDS
  ---------------
  journalctl -u ${SERVICE_NAME} -f        watch it live
  systemctl status ${SERVICE_NAME}        is it running
  sudo systemctl stop ${SERVICE_NAME}     STOP (squares off first)

  It is in PAPER MODE. Run it that way for a month before going live.

SUMMARY
