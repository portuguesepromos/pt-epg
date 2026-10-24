#!/usr/bin/env bash
# Builds the EPG and publishes it into the web folder.
# Run by the systemd timer (pt-epg.timer). Run it by hand with:
#   sudo systemctl start pt-epg      (or: sudo -u epg /opt/pt-epg/update.sh)
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/pt-epg}"
# defaults - override them in /opt/pt-epg/config.env
WEB_DIR="/var/www/pt-epg"
DAYS=4
MIN_CHANNELS=150
MAX_CONNECTIONS=10
PUBLISH_PLAIN_XML=yes
[ -f "$APP_DIR/config.env" ] && . "$APP_DIR/config.env"

log() { echo "[$(date '+%F %T')] $*"; }

cd "$APP_DIR"

# only one run at a time
exec 9>"$APP_DIR/.lock"
if ! flock -n 9; then log "another update is already running - exiting"; exit 0; fi

# ---------------------------------------------------------------- 1. update the grabber
# Updating every run means fixes for MEO/NOS/Vodafone site changes arrive automatically.
# If the new version fails to install, go back to the version that worked.
if [ ! -d grabber/.git ]; then
  log "cloning iptv-org/epg"
  git clone --depth 1 -b master https://github.com/iptv-org/epg.git grabber
  (cd grabber && npm install --no-audit --no-fund --loglevel=error)
else
  old_head=$(git -C grabber rev-parse HEAD)
  old_lock=$(sha1sum grabber/package-lock.json | cut -d' ' -f1)
  if git -C grabber fetch -q --depth 1 origin master && git -C grabber reset -q --hard FETCH_HEAD; then
    new_lock=$(sha1sum grabber/package-lock.json | cut -d' ' -f1)
    if [ "$old_lock" != "$new_lock" ] || [ ! -d grabber/node_modules ]; then
      log "grabber dependencies changed - npm install"
      if ! (cd grabber && npm install --no-audit --no-fund --loglevel=error); then
        log "WARNING: npm install failed - going back to $old_head"
        git -C grabber fetch -q --depth 1 origin "$old_head" || true
        git -C grabber reset -q --hard "$old_head"
        (cd grabber && npm install --no-audit --no-fund --loglevel=error)
      fi
    fi
    log "grabber at $(git -C grabber rev-parse --short HEAD)"
  else
    log "WARNING: could not update grabber - using the current copy"
  fi
fi

# ---------------------------------------------------------------- 2. grab
WORK=$(mktemp -d "$APP_DIR/.work.XXXXXX")
trap 'rm -rf "$WORK"' EXIT

# One site at a time keeps memory low (~400 MB peak instead of ~1 GB) - fine for a 1 GB VPS.
mkdir -p "$WORK/channels" "$WORK/guides"
for site in $(grep -o 'site="[^"]*"' channels.xml | cut -d'"' -f2 | awk '!seen[$0]++'); do
  { echo '<?xml version="1.0" encoding="UTF-8"?>'; echo '<channels>'
    grep -F "site=\"$site\"" channels.xml; echo '</channels>'; } > "$WORK/channels/$site.xml"
  log "grabbing $site ($(grep -c '<channel ' "$WORK/channels/$site.xml") channels, $DAYS days)"
  (cd grabber && npm run --silent grab --- \
      --channels="$WORK/channels/$site.xml" \
      --output="$WORK/guides/{site}.xml" \
      --days="$DAYS" \
      --maxConnections="$MAX_CONNECTIONS" \
      --timeout=30000 \
      --delay=100) >> "$WORK/grab.log" 2>&1 || log "WARNING: $site failed (other sources will cover it)"
done
log "grab finished: $(grep -c 'programs)' "$WORK/grab.log" || true) requests, $(grep -c '(0 programs)' "$WORK/grab.log" || true) empty"

# ---------------------------------------------------------------- 3. merge (+ safety check)
python3 scripts/merge.py \
  --input "$WORK/guides" \
  --aliases aliases.json \
  --output "$WORK/public/epg.xml" \
  --min-channels "$MIN_CHANNELS"

# ---------------------------------------------------------------- 4. publish (atomic: never a half-written file)
mkdir -p "$WEB_DIR"
[ "$PUBLISH_PLAIN_XML" = "yes" ] || rm -f "$WORK/public/epg.xml"
cat > "$WORK/public/index.html" <<EOF
<!doctype html><meta charset="utf-8"><title>PT EPG</title>
<h3>PT EPG</h3><p>Updated $(date -u '+%F %H:%M UTC')</p>
<p><a href="epg.xml.gz">epg.xml.gz</a> | <a href="epg.xml">epg.xml</a> | <a href="report.txt">report.txt</a></p>
EOF
for f in "$WORK"/public/*; do
  name=$(basename "$f")
  cp "$f" "$WEB_DIR/.$name.new"
  chmod 644 "$WEB_DIR/.$name.new"
  mv -f "$WEB_DIR/.$name.new" "$WEB_DIR/$name"
done
[ "$PUBLISH_PLAIN_XML" = "yes" ] || rm -f "$WEB_DIR/epg.xml"
cp "$WORK/grab.log" "$APP_DIR/last-grab.log"

log "published to $WEB_DIR ($(du -h "$WEB_DIR/epg.xml.gz" | cut -f1) gz)"
