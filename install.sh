#!/usr/bin/env bash
# Installs the self-updating PT EPG on a Linux VPS.
#
#   sudo bash vps/install.sh                 # serve on port 8080
#   sudo bash vps/install.sh --port 80 --domain epg.example.com
#   sudo bash vps/install.sh --no-web        # just build files into /var/www/pt-epg (you serve them)
#
# Works on Debian/Ubuntu (apt) and RHEL/Alma/Rocky/Fedora (dnf). Safe to run again (it upgrades).
set -euo pipefail

PORT=8080
DOMAIN="_"
WEB=auto            # auto | nginx | python | none
APP_DIR=/opt/pt-epg
WEB_DIR=/var/www/pt-epg
RUN_USER=epg

while [ $# -gt 0 ]; do
  case "$1" in
    --port)    PORT="$2"; shift 2 ;;
    --domain)  DOMAIN="$2"; shift 2 ;;
    --no-web)  WEB=none; shift ;;
    --web)     WEB="$2"; shift 2 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Please run with sudo"; exit 1; }
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$SRC_DIR/channels.xml" ] || { echo "Run this from the extracted pt-epg folder"; exit 1; }

say() { echo -e "\n\033[1;32m==> $*\033[0m"; }

# ------------------------------------------------------------------ packages
if command -v apt-get >/dev/null; then PM=apt; elif command -v dnf >/dev/null; then PM=dnf; else
  echo "Unsupported distro (need apt or dnf). Install git, curl, python3, nodejs>=18 manually and re-run."; exit 1; fi

pkg_install() {
  if [ $PM = apt ]; then DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null
  else dnf install -y -q "$@" >/dev/null; fi
}

say "Installing base packages"
[ $PM = apt ] && apt-get update -qq
pkg_install git curl ca-certificates python3 util-linux

node_major() { command -v node >/dev/null && node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0; }
if [ "$(node_major)" -lt 18 ]; then
  say "Installing Node.js 22 (NodeSource)"
  if [ $PM = apt ]; then curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
  else curl -fsSL https://rpm.nodesource.com/setup_22.x | bash - >/dev/null; fi
  pkg_install nodejs
fi
command -v npm >/dev/null || pkg_install npm
echo "node $(node -v), npm $(npm -v), $(python3 -V)"

# ------------------------------------------------------------------ memory
# The grabber peaks at ~800 MB while running. On small VPSs add a swapfile so it can't get OOM-killed.
mem_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
swap_mb=$(awk '/SwapTotal/{print int($2/1024)}' /proc/meminfo)
if [ "$mem_mb" -lt 1800 ] && [ "$swap_mb" -lt 512 ] && [ ! -e /swapfile-pt-epg ]; then
  say "Only ${mem_mb} MB RAM and no swap - adding a 1 GB swapfile (/swapfile-pt-epg)"
  if fallocate -l 1G /swapfile-pt-epg 2>/dev/null || dd if=/dev/zero of=/swapfile-pt-epg bs=1M count=1024 status=none; then
    chmod 600 /swapfile-pt-epg && mkswap /swapfile-pt-epg >/dev/null && swapon /swapfile-pt-epg \
      && echo '/swapfile-pt-epg none swap sw 0 0' >> /etc/fstab \
      || { echo "could not enable swap (container VPS?) - continuing"; rm -f /swapfile-pt-epg; }
  fi
fi

# ------------------------------------------------------------------ user + files
say "Installing app into $APP_DIR"
id "$RUN_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$RUN_USER"
mkdir -p "$APP_DIR/scripts" "$WEB_DIR"
install -m 644 "$SRC_DIR/channels.xml" "$SRC_DIR/aliases.json" "$APP_DIR/"
install -m 644 "$SRC_DIR"/scripts/*.py "$APP_DIR/scripts/"
install -m 755 "$SRC_DIR/vps/update.sh" "$APP_DIR/update.sh"
if [ ! -f "$APP_DIR/config.env" ]; then
  cat > "$APP_DIR/config.env" <<EOF
# Settings for update.sh - edit and they apply on the next run
WEB_DIR=$WEB_DIR
DAYS=4                 # days of guide to download
MIN_CHANNELS=150       # don't publish if fewer channels than this have data
MAX_CONNECTIONS=10     # parallel requests to the TV sites
PUBLISH_PLAIN_XML=yes  # also publish the uncompressed epg.xml (~36 MB)
EOF
fi
chown -R "$RUN_USER:$RUN_USER" "$APP_DIR" "$WEB_DIR"

# ------------------------------------------------------------------ systemd service + timer
say "Creating systemd timer (runs 03:15 and 15:15 daily)"
cat > /etc/systemd/system/pt-epg.service <<EOF
[Unit]
Description=Build Portuguese EPG
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=$RUN_USER
Group=$RUN_USER
WorkingDirectory=$APP_DIR
Environment=HOME=$APP_DIR
Environment=APP_DIR=$APP_DIR
ExecStart=$APP_DIR/update.sh
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=1h
EOF

cat > /etc/systemd/system/pt-epg.timer <<'EOF'
[Unit]
Description=Build Portuguese EPG twice a day

[Timer]
OnCalendar=*-*-* 03,15:15:00
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ------------------------------------------------------------------ web server
port_busy() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q . ; }

if [ "$WEB" = auto ]; then
  if command -v nginx >/dev/null; then WEB=nginx
  elif ! port_busy 80; then WEB=nginx
  else WEB=python; fi   # something else owns port 80 (apache/caddy...) - don't fight it
fi

if [ "$WEB" = nginx ]; then
  say "Configuring nginx on port $PORT"
  if ! command -v nginx >/dev/null; then
    pkg_install nginx
    # the stock "default" site listens on 80; drop it if we're not using 80 for anything
    [ "$PORT" != 80 ] && rm -f /etc/nginx/sites-enabled/default
  fi
  [ "$PORT" = 80 ] && rm -f /etc/nginx/sites-enabled/default
  cat > /etc/nginx/conf.d/pt-epg.conf <<EOF
server {
    listen $PORT;
    listen [::]:$PORT;
    server_name $DOMAIN;
    root $WEB_DIR;
    index index.html;
    access_log /var/log/nginx/pt-epg.access.log;

    location / {
        types {
            text/html        html;
            application/xml  xml;
            application/gzip gz;
            text/plain       txt;
        }
        gzip on;                       # compresses epg.xml on the fly for apps that accept it
        gzip_types application/xml text/plain;
        add_header Cache-Control "public, max-age=1800";
        add_header Access-Control-Allow-Origin "*";
        try_files \$uri =404;
    }
}
EOF
  nginx -t
  systemctl enable --now nginx >/dev/null 2>&1 || true
  systemctl reload nginx
elif [ "$WEB" = python ]; then
  say "Port 80 is used by another web server - serving with a small Python server on port $PORT"
  cat > /etc/systemd/system/pt-epg-web.service <<EOF
[Unit]
Description=Serve Portuguese EPG files
After=network.target

[Service]
User=$RUN_USER
ExecStart=/usr/bin/env python3 -m http.server $PORT --directory $WEB_DIR
Restart=always

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now pt-epg-web
  systemctl restart pt-epg-web
fi

# ------------------------------------------------------------------ firewall (only if one is active)
if [ "$WEB" != none ]; then
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow "$PORT/tcp" >/dev/null && echo "ufw: opened $PORT/tcp"
  elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd -q --permanent --add-port="$PORT/tcp" && firewall-cmd -q --reload && echo "firewalld: opened $PORT/tcp"
  fi
fi

# ------------------------------------------------------------------ first run
systemctl daemon-reload
systemctl enable --now pt-epg.timer >/dev/null
say "First build (takes ~3-5 minutes; the first run also installs the grabber)..."
systemctl start pt-epg.service || { echo "First run failed - see: journalctl -u pt-epg -n 100"; exit 1; }
journalctl -u pt-epg -n 6 --no-pager -o cat || true

IP=$(curl -fs4 --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
HOST=$([ "$DOMAIN" != "_" ] && echo "$DOMAIN" || echo "$IP")
URL="http://$HOST$([ "$PORT" != 80 ] && echo ":$PORT")"
say "Done!"
if [ "$WEB" = none ]; then
  echo "Files are built into $WEB_DIR - point your own web server at it."
else
  echo "EPG URL:     $URL/epg.xml.gz"
  echo "Plain XML:   $URL/epg.xml"
  echo "Report:      $URL/report.txt"
fi
echo
echo "Next runs:   systemctl list-timers pt-epg.timer"
echo "Run now:     sudo systemctl start pt-epg"
echo "Logs:        journalctl -u pt-epg -n 50"
echo "Settings:    $APP_DIR/config.env"
