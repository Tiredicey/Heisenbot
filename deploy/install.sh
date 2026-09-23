#!/usr/bin/env bash
set -euo pipefail
REPO="${HEISENBOT_REPO:-https://github.com/Tiredicey/Heisenbot.git}"
APP=/opt/heisenbot
USER_NAME=heisenbot
[ "$(id -u)" -eq 0 ] || exec sudo -E bash "$0" "$@"
echo "== Heisenbot server install =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y git curl ffmpeg fonts-dejavu-core python3 python3-venv python3-pip ca-certificates
ARCH=$(dpkg --print-architecture)
if ! command -v cloudflared >/dev/null; then
  curl -fsSL -o /usr/local/bin/cloudflared "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}"
  chmod +x /usr/local/bin/cloudflared
fi
id -u "$USER_NAME" >/dev/null 2>&1 || useradd -r -m -d /var/lib/heisenbot -s /usr/sbin/nologin "$USER_NAME"
if [ -d "$APP/.git" ]; then sudo -u "$USER_NAME" git -C "$APP" pull --ff-only; else git clone --depth 1 "$REPO" "$APP"; fi
chown -R "$USER_NAME":"$USER_NAME" "$APP"
sudo -u "$USER_NAME" python3 -m venv "$APP/.venv"
sudo -u "$USER_NAME" "$APP/.venv/bin/pip" install --upgrade pip -q
sudo -u "$USER_NAME" "$APP/.venv/bin/pip" install -r "$APP/requirements.txt" -q
"$APP/.venv/bin/python" -m playwright install-deps chromium
sudo -u "$USER_NAME" PLAYWRIGHT_BROWSERS_PATH=/var/lib/heisenbot/browsers "$APP/.venv/bin/python" -m playwright install chromium
mkdir -p /etc/heisenbot
if [ ! -f /etc/heisenbot/env ]; then
  PW=$(tr -dc 'a-z0-9' </dev/urandom | head -c 12)
  TOPIC="walter-$(tr -dc 'a-z0-9' </dev/urandom | head -c 10)"
  cat > /etc/heisenbot/env <<ENV
HEISENBOT_PASSWORD=$PW
HEISENBOT_SERVER=1
HEISENBOT_HOST=127.0.0.1
HEISENBOT_DATA=/var/lib/heisenbot/data
HEISENBOT_NTFY=$TOPIC
PLAYWRIGHT_BROWSERS_PATH=/var/lib/heisenbot/browsers
ENV
  chmod 600 /etc/heisenbot/env
fi
if [ ! -f /swapfile ] && [ "$(free -m | awk '/Mem:/{print $2}')" -lt 3000 ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
install -m 644 "$APP/deploy/heisenbot.service" /etc/systemd/system/heisenbot.service
install -m 644 "$APP/deploy/heisenbot-tunnel.service" /etc/systemd/system/heisenbot-tunnel.service
install -m 755 "$APP/deploy/walter" /usr/local/bin/walter
systemctl daemon-reload
systemctl enable --now heisenbot.service heisenbot-tunnel.service
systemctl restart heisenbot.service heisenbot-tunnel.service
echo "Waiting for the dashboard address..."
for i in $(seq 1 30); do
  URL=$(curl -s http://127.0.0.1:20241/quicktunnel | sed -n 's/.*"hostname":"\([^"]*\)".*/https:\/\/\1/p')
  [ -n "$URL" ] && break
  sleep 2
done
. /etc/heisenbot/env
echo
echo "=============================================="
echo " Walter is installed and will run 24/7."
echo " Dashboard : ${URL:-run 'walter url' in a minute}"
echo " Password  : $HEISENBOT_PASSWORD"
echo " Phone alerts: install the ntfy app, subscribe to topic  $HEISENBOT_NTFY"
echo " Later, type 'walter' to see these again."
echo "=============================================="
