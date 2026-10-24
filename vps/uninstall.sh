#!/usr/bin/env bash
# Removes everything install.sh created (Node.js, nginx and git stay installed).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo"; exit 1; }
systemctl disable --now pt-epg.timer pt-epg-web.service 2>/dev/null || true
rm -f /etc/systemd/system/pt-epg.service /etc/systemd/system/pt-epg.timer /etc/systemd/system/pt-epg-web.service
systemctl daemon-reload
if [ -f /etc/nginx/conf.d/pt-epg.conf ]; then rm -f /etc/nginx/conf.d/pt-epg.conf; nginx -t && systemctl reload nginx; fi
rm -rf /opt/pt-epg /var/www/pt-epg
id epg >/dev/null 2>&1 && userdel epg || true
if grep -q swapfile-pt-epg /etc/fstab; then
  swapoff /swapfile-pt-epg || true; sed -i '/swapfile-pt-epg/d' /etc/fstab; rm -f /swapfile-pt-epg
fi
echo "PT EPG removed."
