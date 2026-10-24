# My PT EPG – self-updating Portuguese TV guide

This builds its own XMLTV guide **twice a day**. It pulls the listings straight from **MEO, NOS and Vodafone**,
plus rtp.pt, sporttv.pt, tvi.iol.pt, opto.sic.pt and meuguia.tv. For each channel it keeps the best source,
so if one source breaks another takes over. Each channel is published under all the IDs your old EPG used,
so your playlist doesn't need any changes.

---

## Option A – your own Linux VPS (recommended)

Works on Debian/Ubuntu and on RHEL/Alma/Rocky/Fedora. You need about 1 GB RAM (the installer adds swap on
small machines) and about 300 MB of disk.

```sh
# copy pt-epg.zip to the VPS, then:
unzip pt-epg.zip && cd pt-epg
sudo bash vps/install.sh
```

It installs everything, runs the first build (about 2 minutes) and then prints your URL:

```
EPG URL:     http://YOUR-VPS-IP:8080/epg.xml.gz
```

Put that URL in your IPTV app instead of the JohnPulse one.

### Installer options
| Command | What it does |
|---|---|
| `sudo bash vps/install.sh` | Serves on **port 8080** with nginx. |
| `sudo bash vps/install.sh --port 80 --domain epg.mydomain.com` | Uses your own domain on port 80. |
| `sudo bash vps/install.sh --no-web` | Only builds the files into `/var/www/pt-epg`, for when you already run a web server and want to serve that folder yourself. |
| `sudo bash vps/install.sh --web python` | Uses a tiny built-in server instead of nginx. This happens automatically if Apache or Caddy already uses port 80. |

### What gets installed
| Item | Location |
|---|---|
| App, settings, grabber | `/opt/pt-epg/` (`config.env` is where your settings live) |
| Published files | `/var/www/pt-epg/epg.xml.gz`, `epg.xml`, `report.txt` |
| Schedule | systemd timer `pt-epg.timer`, runs at **03:15 and 15:15**. If the VPS was off at that time, it catches up after boot. |
| Runs as | an unprivileged `epg` user |
| Web | `/etc/nginx/conf.d/pt-epg.conf` |

### Everyday commands
```sh
sudo systemctl start pt-epg            # update right now
systemctl list-timers pt-epg.timer     # when is the next run
journalctl -u pt-epg -n 50             # log of the last run
cat /var/www/pt-epg/report.txt         # which source each channel is using
sudo nano /opt/pt-epg/config.env       # DAYS, MIN_CHANNELS, ...
sudo bash vps/uninstall.sh             # remove everything
```

### Safety features
* **Bad runs don't replace the guide.** If fewer than 150 channels have data, nothing is published and the last good guide stays online.
* **Files are swapped atomically.** Your app never downloads a half-written file.
* **Grabber updates are checked.** It updates itself every run (fixes for MEO/NOS/Vodafone site changes arrive automatically). If an update fails to install, it rolls back to the version that worked.

### Changing channels on the VPS
Edit `/opt/pt-epg/channels.xml` (delete lines you don't want) or `/opt/pt-epg/aliases.json`. The change applies on the next run.
To add a playlist ID to a channel:
```json
"RTP.1.HD.pt": { "ids": ["Rtp1.pt", "rtp1.pt", "MY-PLAYLIST-ID"], "names": [] }
```

---

## Option B – GitHub Pages (no server)

1. Create a **public** repo and upload this folder, including `.github/`.
2. Go to **Settings → Pages → Source** and pick **GitHub Actions**.
3. Go to **Actions → Update EPG → Run workflow**.
4. Your URL is `https://<user>.github.io/<repo>/epg.xml.gz`

---

## Why the old URL broke
`JohnPulse.github.io/iptv-epg/epg-strong8k.xml.gz` is still being regenerated, but its Portuguese data came
from `epgshare01.online` (PT1). **That source stopped updating on 24 Oct 2026**, so every `.pt` channel in it ends that day.

## Rebuilding the channel list from scratch
Do this to pick up new channels added by MEO/NOS/Vodafone:
```sh
git clone --depth 1 https://github.com/iptv-org/epg.git ../epg
python3 scripts/build_channels.py --epg-repo ../epg --old old-epg.xml.gz   # --old is optional
sudo cp channels.xml aliases.json /opt/pt-epg/
```
