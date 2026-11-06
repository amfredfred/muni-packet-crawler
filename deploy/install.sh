#!/usr/bin/env bash
#
# Linux/systemd equivalent of install.ps1 — sets up the webapp as a
# persistent systemd service (restarts on crash, survives reboots) and
# crawl/review as daily systemd timers, mirroring the Windows Task
# Scheduler setup this project also supports.
#
# NOT YET RUN AGAINST A REAL SERVER as of writing this — prepared ahead of
# actual VPS access being available. Review before running; in particular
# the OS package manager commands assume Debian/Ubuntu (apt). Adjust for
# other distros (dnf/yum for RHEL-family, pacman for Arch, etc.) if needed.
#
# Usage (run from the project root, e.g. after `git clone` into place):
#   sudo bash deploy/install.sh install [webapp|crawl|review|all]
#   sudo bash deploy/install.sh uninstall [webapp|crawl|review|all]
#   sudo bash deploy/install.sh update [webapp|crawl|review|all]
#
# "update" re-copies the unit files (picks up any edits to them) and
# restarts the webapp service / re-enables the timers — run it after a
# `git pull` that changed deploy/*.service or deploy/*.timer.

set -euo pipefail

ACTION="${1:-install}"
COMPONENT="${2:-all}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_USER="${SUDO_USER:-$(whoami)}"
UNIT_DIR="/etc/systemd/system"

if [[ $EUID -ne 0 ]]; then
    echo "Run this with sudo (systemd unit files and package installs need root)." >&2
    exit 1
fi

components_for() {
    case "$1" in
        all) echo "webapp crawl review" ;;
        webapp|crawl|review) echo "$1" ;;
        *) echo "Unknown component: $1 (expected webapp, crawl, review, or all)" >&2; exit 1 ;;
    esac
}

install_system_deps() {
    echo "=== Installing system packages (python3-venv) ==="
    apt-get update -qq
    apt-get install -y python3-venv python3-pip
}

setup_venv() {
    if [[ ! -d "$PROJECT_DIR/venv" ]]; then
        echo "=== Creating venv and installing this project into it ==="
        sudo -u "$DEPLOY_USER" python3 -m venv "$PROJECT_DIR/venv"
        sudo -u "$DEPLOY_USER" "$PROJECT_DIR/venv/bin/pip" install -e "$PROJECT_DIR"
        echo "=== Installing Playwright's browser + OS deps (for JS-rendered scrapers) ==="
        sudo -u "$DEPLOY_USER" "$PROJECT_DIR/venv/bin/python" -m playwright install --with-deps chromium
    else
        echo "=== venv already exists, skipping (delete $PROJECT_DIR/venv to force a rebuild) ==="
    fi
}

install_unit() {
    local name="$1"
    local src="$PROJECT_DIR/deploy/muni-$name.service"
    local dest="$UNIT_DIR/muni-$name.service"
    sed -e "s#__PROJECT_DIR__#$PROJECT_DIR#g" -e "s#__DEPLOY_USER__#$DEPLOY_USER#g" "$src" > "$dest"
    echo "  wrote $dest"

    local timer_src="$PROJECT_DIR/deploy/muni-$name.timer"
    if [[ -f "$timer_src" ]]; then
        cp "$timer_src" "$UNIT_DIR/muni-$name.timer"
        echo "  wrote $UNIT_DIR/muni-$name.timer"
    fi
}

do_install() {
    for name in $(components_for "$COMPONENT"); do
        echo ""
        echo "=== $ACTION $name ==="
        install_unit "$name"
        systemctl daemon-reload

        if [[ "$name" == "webapp" ]]; then
            systemctl enable --now "muni-$name.service"
            echo "  Control panel: http://$(hostname -I | awk '{print $1}'):5000"
            echo "  First visit registers your authenticator app (TOTP)."
            echo "  SECURITY: the Flask dev server isn't meant for direct internet exposure."
            echo "  Put it behind a firewall (allow only your own IP to port 5000) or a"
            echo "  reverse proxy with TLS (nginx/Caddy) before relying on this long-term."
        else
            systemctl enable --now "muni-$name.timer"
            echo "  Scheduled — runs daily. Trigger a one-off run now with:"
            echo "    systemctl start muni-$name.service"
        fi
    done
}

do_uninstall() {
    for name in $(components_for "$COMPONENT"); do
        echo ""
        echo "=== uninstall $name ==="
        systemctl disable --now "muni-$name.service" 2>/dev/null || true
        systemctl disable --now "muni-$name.timer" 2>/dev/null || true
        rm -f "$UNIT_DIR/muni-$name.service" "$UNIT_DIR/muni-$name.timer"
    done
    systemctl daemon-reload
    echo "Uninstalled: $COMPONENT"
}

case "$ACTION" in
    install)
        install_system_deps
        setup_venv
        do_install
        ;;
    update)
        do_install
        for name in $(components_for "$COMPONENT"); do
            if [[ "$name" == "webapp" ]]; then
                systemctl restart "muni-$name.service"
            fi
        done
        ;;
    uninstall)
        do_uninstall
        ;;
    *)
        echo "Usage: $0 {install|uninstall|update} [webapp|crawl|review|all]" >&2
        exit 1
        ;;
esac

echo ""
echo "Done. Check status with:"
echo "  systemctl status muni-webapp"
echo "  journalctl -u muni-webapp -f"
