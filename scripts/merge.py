#!/usr/bin/env python3
"""
Merge the per-site guides produced by iptv-org/epg into ONE guide.

For every channel (e.g. "RTP1.pt") it looks at every source that has it
(meo.pt, nostv.pt, vodafone.pt ...) and keeps the source with the best
coverage from "now" onwards. So if MEO breaks for a channel, NOS or
Vodafone takes over automatically.

It then also writes the same programmes under every alias ID listed in
aliases.json, so the tvg-id values already in your playlist keep working.

Usage:
  python3 scripts/merge.py --input guides --aliases aliases.json --output public/epg.xml
Only the Python standard library is used.
"""
import argparse
import glob
import gzip
import json
import math
import os
import shutil
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

# Lower index = preferred when two sources are equally good
PRIORITY = ["meo.pt", "nostv.pt", "vodafone.pt", "rtp.pt", "sporttv.pt",
            "tvi.iol.pt", "opto.sic.pt", "meuguia.tv", "chaines-tv.orange.fr"]


def parse_time(s):
    # XMLTV format: "20261024133000 +0000"
    s = s.strip()
    fmt = "%Y%m%d%H%M%S %z" if " " in s else "%Y%m%d%H%M%S"
    dt = datetime.strptime(s, fmt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def base_id(xmltv_id):
    # build_channels.py already gives every channel one clean id
    return xmltv_id.strip()


def load_guide(path):
    site = os.path.basename(path).replace(".xml", "")
    channels, programmes = {}, {}
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag == "channel":
            cid = base_id(el.get("id", ""))
            if cid:
                channels.setdefault(cid, {"names": [], "icon": None, "url": None})
                for dn in el.findall("display-name"):
                    if dn.text and dn.text not in channels[cid]["names"]:
                        channels[cid]["names"].append(dn.text)
                icon = el.find("icon")
                if icon is not None and not channels[cid]["icon"]:
                    channels[cid]["icon"] = icon.get("src")
            el.clear()
        elif el.tag == "programme":
            cid = base_id(el.get("channel", ""))
            el.set("channel", cid)
            # <image> is a recent XMLTV addition that older players/parsers
            # don't know; <icon> already carries the picture, so drop it.
            for img in el.findall("image"):
                el.remove(img)
            programmes.setdefault(cid, []).append(ET.tostring(el, encoding="unicode").strip())
            # keep start/stop for scoring
            programmes[cid][-1] = (el.get("start"), el.get("stop"), programmes[cid][-1])
            el.clear()
    return site, channels, programmes


def coverage_hours(progs, now, max_len_h=12):
    """Hours of guide from now on, counting overlaps only once and ignoring
    the bogus multi-day "programmes" some sources emit."""
    spans = []
    for start, stop, _ in progs:
        try:
            a, b = parse_time(start), parse_time(stop)
        except Exception:
            continue
        a = max(a, now)
        if b <= a:
            continue
        spans.append((a.timestamp(), min(b.timestamp(), a.timestamp() + max_len_h * 3600)))
    total, cur_a, cur_b = 0.0, None, None
    for a, b in sorted(spans):
        if cur_b is None or a > cur_b:
            if cur_b is not None:
                total += cur_b - cur_a
            cur_a, cur_b = a, b
        else:
            cur_b = max(cur_b, b)
    if cur_b is not None:
        total += cur_b - cur_a
    return total / 3600


def dedupe(progs):
    """Sort by start time and drop programmes that start at the same time as the previous one."""
    out, seen = [], set()
    for p in sorted(progs, key=lambda p: parse_time(p[0])):
        if p[0] in seen:
            continue
        seen.add(p[0])
        out.append(p)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="guides")
    ap.add_argument("--aliases", default="aliases.json")
    ap.add_argument("--output", default="public/epg.xml")
    ap.add_argument("--channels", default="channels.xml",
                    help="channels.xml - the order of sources per channel there = preference order")
    ap.add_argument("--only-aliased", action="store_true",
                    help="only output channels that appear in aliases.json")
    ap.add_argument("--min-channels", type=int, default=0,
                    help="fail (exit 1) if fewer channels than this have data, so a broken run "
                         "never replaces a good guide")
    ap.add_argument("--min-percent", type=float, default=60,
                    help="...but never ask for more than this %% of the channels in channels.xml, "
                         "so a short hand-picked list doesn't trip the check (default 60)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    files = sorted(glob.glob(os.path.join(args.input, "*.xml")))
    if not files:
        sys.exit(f"No guides found in {args.input}/")

    sources = [load_guide(f) for f in files]
    sources.sort(key=lambda s: PRIORITY.index(s[0]) if s[0] in PRIORITY else 99)

    aliases = {}
    if os.path.exists(args.aliases):
        with open(args.aliases, encoding="utf-8") as fh:
            aliases = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}

    all_ids = []
    for _, chans, progs in sources:
        for cid in list(chans) + list(progs):
            if cid not in all_ids:
                all_ids.append(cid)
    if args.only_aliased:
        all_ids = [c for c in all_ids if c in aliases]

    # per-channel preference: order of the sources in channels.xml (first = preferred)
    pref = {}
    if os.path.exists(args.channels):
        for el in ET.parse(args.channels).getroot().iter("channel"):
            pref.setdefault(el.get("xmltv_id", ""), []).append(el.get("site"))

    def ordered(cid):
        order = pref.get(cid)
        if not order:
            return sources
        return sorted(sources, key=lambda s: order.index(s[0]) if s[0] in order else 99)

    chosen, report = {}, []
    for cid in all_ids:
        best = None
        for site, chans, progs in ordered(cid):
            p = progs.get(cid, [])
            if not p:
                continue
            hours = coverage_hours(p, now)
            if best is None or hours > best[1] + 1:  # needs >1h more to beat a preferred source
                best = (site, hours, p)
        if best and best[1] > 0:
            chosen[cid] = best
            report.append(f"{cid:40s} {best[0]:12s} {best[1]:6.1f}h")
        else:
            report.append(f"{cid:40s} {'-- NO DATA --':12s}")

    # The safety minimum scales with the size of your channel list:
    # min(MIN_CHANNELS, min-percent of the channels you picked)
    wanted = sorted(c for c in set(pref) if c) if pref else all_ids
    need = args.min_channels
    if need and wanted:
        need = min(need, math.ceil(len(wanted) * args.min_percent / 100))
    missing = [c for c in wanted if c not in chosen]
    if len(chosen) < need:
        print("\n".join(report))
        print(f"\n{len(missing)} of {len(wanted)} channels in channels.xml got NO data:")
        print("  " + ", ".join(missing[:200]))
        sites = {}
        for site, chans, progs in sources:
            sites[site] = sum(1 for c in progs if progs[c])
        print("Channels with programmes per site: " + ", ".join(f"{k}={v}" for k, v in sites.items()))
        sys.exit(f"Only {len(chosen)} channels have data (need {need} = min({args.min_channels}, "
                 f"{args.min_percent:g}% of {len(wanted)})) - NOT publishing.")
    if missing:
        print(f"Note: {len(missing)} of {len(wanted)} picked channels got no data: " + ", ".join(missing[:50]))

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    n_prog = 0
    with open(args.output, "w", encoding="utf-8") as out:
        out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        out.write(f'<tv generator-info-name="my-pt-epg" date="{now.strftime("%Y%m%d%H%M%S +0000")}">\n')

        # ---- channels
        for cid in all_ids:
            if cid not in chosen:
                continue
            names, icon = [], None
            for _, chans, _ in sources:
                c = chans.get(cid)
                if c:
                    names += [n for n in c["names"] if n not in names]
                    icon = icon or c["icon"]
            alias = aliases.get(cid, {})
            names += [n for n in alias.get("names", []) if n not in names]
            for out_id in [cid] + [a for a in alias.get("ids", []) if a != cid]:
                out.write(f"  <channel id={quoteattr(out_id)}>\n")
                for n in names:
                    out.write(f'    <display-name lang="pt">{escape(n)}</display-name>\n')
                if icon:
                    out.write(f"    <icon src={quoteattr(icon)} />\n")
                out.write("  </channel>\n")

        # ---- programmes
        for cid in all_ids:
            if cid not in chosen:
                continue
            progs = dedupe(chosen[cid][2])
            out_ids = [cid] + [a for a in aliases.get(cid, {}).get("ids", []) if a != cid]
            old_attr = f"channel={quoteattr(cid)}"
            for _, _, xml in progs:
                for out_id in out_ids:
                    out.write("  " + xml.replace(old_attr, f"channel={quoteattr(out_id)}", 1) + "\n")
                    n_prog += 1
        out.write("</tv>\n")

    with open(args.output, "rb") as src, gzip.open(args.output + ".gz", "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)

    rep_path = os.path.join(os.path.dirname(args.output) or ".", "report.txt")
    with open(rep_path, "w", encoding="utf-8") as fh:
        fh.write(f"Generated {now.isoformat()}\n\n" + "\n".join(report) + "\n")

    print(f"{len(chosen)}/{len(all_ids)} channels with data, {n_prog} programmes written -> {args.output}(.gz)")


if __name__ == "__main__":
    main()
