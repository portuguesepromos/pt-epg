#!/usr/bin/env python3
"""
Make dark logos readable on dark apps (OTT Navigator, TiviMate, ...).

Reads the finished guide, downloads every channel logo, and for logos that are
mostly black/dark on a transparent background it writes a light version into
<public>/logos/ and points the channel's <icon> at that copy.

  --mode invert  (default) flips light and dark but keeps the colours:
                 black -> white, dark grey -> light grey, navy -> light blue,
                 red / green / yellow stay as they are.
  --mode plate   keeps the logo as it is and puts it on a light rounded tile.

Logos that already have their own (non-transparent) background, SVGs and links
that fail to download are left untouched, so a problem here never breaks the guide.

  python3 scripts/fix_logos.py --xml public/epg.xml \\
      --base-url https://USER.github.io/REPO [--mode invert|plate]

Needs Pillow + numpy (apt install python3-pil python3-numpy).
"""
import argparse
import gzip
import hashlib
import io
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    import numpy as np
    from PIL import Image, ImageDraw
except ImportError:
    print("fix_logos: Pillow/numpy not installed - leaving logos as they are")
    sys.exit(0)

MAX_W, MAX_H = 600, 400
UA = {"User-Agent": "pt-epg-logo-fixer/1.0 (+https://github.com/iptv-org/epg; personal EPG build)"}
# some hosts (Wikimedia) rate-limit bursts: max 2 parallel downloads per host
HOST_LOCKS, HOST_LOCKS_GUARD = {}, threading.Lock()


def fetch(url, tries=4):
    host = urllib.parse.urlsplit(url).netloc
    with HOST_LOCKS_GUARD:
        sem = HOST_LOCKS.setdefault(host, threading.Semaphore(2))
    for n in range(tries):
        try:
            with sem:
                req = urllib.request.Request(url, headers=UA)
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.read(8_000_000)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                return None
        except Exception:
            pass
        time.sleep(1.5 * (n + 1))
    return None


def analyse(img):
    """Returns (is_dark, reason). img is RGBA float array 0..1."""
    a = img[..., 3]
    transparent = (a < 0.1).mean()
    opaque = a > 0.5
    if opaque.sum() < 50:
        return False, "empty"
    if transparent < 0.04:
        return False, "has its own background"
    rgb = img[..., :3][opaque]
    light = (rgb.max(axis=1) + rgb.min(axis=1)) / 2          # HSL lightness
    dark_share = (light < 0.33).mean()                       # black / dark grey / navy ...
    white_share = (light > 0.8).mean()                       # parts that would turn dark
    if dark_share > 0.55 or (dark_share > 0.3 and white_share < 0.1):
        return True, f"dark ({dark_share:.0%} dark, {white_share:.0%} white pixels)"
    return False, f"ok ({dark_share:.0%} dark pixels)"


def invert_lightness(img):
    """Lightness L -> 1-L with chroma kept: add (1 - max - min) to every channel."""
    rgb = img[..., :3]
    shift = 1.0 - rgb.max(axis=2, keepdims=True) - rgb.min(axis=2, keepdims=True)
    out = img.copy()
    out[..., :3] = np.clip(rgb + shift, 0, 1)
    return out


def plate(pil):
    w, h = pil.size
    pad = max(6, int(min(w, h) * 0.12))
    tile = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle([0, 0, tile.width - 1, tile.height - 1],
                                           radius=pad, fill=(242, 244, 247, 255))
    tile.alpha_composite(pil, (pad, pad))
    return tile


def process(url, out_dir, mode):
    data = fetch(url)
    if not data or data.lstrip()[:5] in (b"<?xml", b"<svg ") or b"<svg" in data[:300]:
        return url, None, "skipped (download failed or SVG)"
    try:
        pil = Image.open(io.BytesIO(data))
        pil.load()
        pil = pil.convert("RGBA")
    except Exception:
        return url, None, "skipped (not an image Pillow can read)"
    pil.thumbnail((MAX_W, MAX_H))
    arr = np.asarray(pil).astype(np.float32) / 255.0
    dark, why = analyse(arr)
    if not dark:
        return url, None, why
    if mode == "plate":
        fixed = plate(pil)
    else:
        fixed = Image.fromarray((invert_lightness(arr) * 255).round().astype(np.uint8), "RGBA")
    name = hashlib.sha1(f"{mode}|{url}".encode()).hexdigest()[:16] + ".png"
    fixed.save(os.path.join(out_dir, name), optimize=True)
    return url, name, why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", default="public/epg.xml")
    ap.add_argument("--base-url", required=True, help="public URL of the folder epg.xml is served from")
    ap.add_argument("--mode", choices=["invert", "plate"], default="invert")
    ap.add_argument("--workers", type=int, default=16)
    args = ap.parse_args()

    xml_path = args.xml
    out_dir = os.path.join(os.path.dirname(xml_path) or ".", "logos")
    os.makedirs(out_dir, exist_ok=True)
    text = open(xml_path, encoding="utf-8").read()

    # only channel logos (channels come before the first programme)
    cut = text.find("<programme")
    head, tail = (text, "") if cut < 0 else (text[:cut], text[cut:])
    urls = sorted(set(re.findall(r'<icon src="([^"]+)"', head)))
    if not urls:
        print("fix_logos: no channel logos in the guide")
        return

    import html
    with ThreadPoolExecutor(args.workers) as ex:
        results = list(ex.map(lambda u: process(html.unescape(u), out_dir, args.mode), urls))

    base = args.base_url.rstrip("/")
    fixed = 0
    for esc_url, (url, name, why) in zip(urls, results):
        if name:
            fixed += 1
            head = head.replace(f'<icon src="{esc_url}"', f'<icon src="{base}/logos/{name}"')
            print(f"  fixed  {url}  -> logos/{name}  [{why}]")
    with open(xml_path, "w", encoding="utf-8") as fh:
        fh.write(head + tail)
    with open(xml_path, "rb") as src, gzip.open(xml_path + ".gz", "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
    skipped = sum(1 for r in results if r[2].startswith("skipped"))
    print(f"fix_logos: {fixed} dark logos made readable ({args.mode}), "
          f"{len(urls) - fixed - skipped} already fine, {skipped} skipped (SVG / download failed)")


if __name__ == "__main__":
    main()
