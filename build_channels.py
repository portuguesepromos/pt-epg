#!/usr/bin/env python3
"""
Builds channels.xml (what to grab) and aliases.json (extra IDs to publish).

* Takes every channel that MEO, NOS and Vodafone publish (plus a few
  broadcaster sites: rtp.pt, sporttv.pt, tvi.iol.pt, opto.sic.pt) from a
  local clone of iptv-org/epg.
* Groups the SAME channel across those sites (by name and by iptv-org ID)
  and gives each group ONE id, so merge.py can fall back between sources.
* If you pass an old EPG (--old), the ids/names from that EPG are reused,
  so the tvg-id values already in your playlist keep matching.

Run it again whenever you want to refresh the channel list:
  python3 scripts/build_channels.py --epg-repo ../epg --old old.xml.gz
"""
import argparse
import gzip
import json
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import OrderedDict

# Sites in order of preference (first = preferred when equally good)
SITES = ["meo.pt", "nostv.pt", "vodafone.pt", "rtp.pt", "sporttv.pt", "tvi.iol.pt", "opto.sic.pt"]
# Single channels taken from other sites (site, site_id) - things no PT operator carries
EXTRA = [("meuguia.tv", "SIC"), ("chaines-tv.orange.fr", "90449")]  # SIC Internacional

GENERIC_FEEDS = {"SD", "HD", "FHD", "UHD", "4K", "Portugal", "EMEA", "Europe"}

# rebrands / different names for the same channel:  normalized old name -> normalized new name
RENAMES = {
    "fox": "starchannel", "foxlife": "starlife", "foxcrime": "starcrime",
    "foxmovies": "starmovies", "foxcomedy": "starcomedy", "blaze": "amcbreak",
    "elevensports1": "dazn1", "elevensports2": "dazn2", "elevensports3": "dazn3",
    "elevensports4": "dazn4", "elevensports5": "dazn5",
    "rtp3": "rtpnoticias", "rtpinternacional": "rtpmundo",
    "btv1": "btv", "benficatv": "btv", "benficatv1": "btv", "slbtv": "btv",
    "historychannel": "canalhistoria", "historia": "canalhistoria",
    "disneyjr": "disneyjunior", "canalnos": "canalnos",
    "panda": "canalpanda", "porto": "portocanal", "natgeo": "nationalgeographic",
    "russiatoday": "rt", "france24fr": "france24french", "sicinternational": "sicinternacional", "france24english": "france24english", "tpanoticias": "tpainternacional",
}


def norm(name, loose=False):
    n = unicodedata.normalize("NFKD", name or "")
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = re.sub(r"[\u1d2c-\u1d6a\u2c7d\u1d43-\u1d5b]+", " ", n)    # superscript tags ᴴᴰ ᴿᴬᵂ ⱽᴵᴾ
    n = n.lower().replace("+", " plus ").replace("&", " e ").replace("!", "")
    n = re.sub(r"^\s*[a-z\- ]{2,8}\s*[:|]\s*", "", n)               # "PT: ", "MEO: ", "IT| "
    n = re.sub(r"\((i|ingles|english)\)", " english ", n)
    n = re.sub(r"\((f|frances|french|francais)\)", " french ", n)
    n = re.sub(r"\b(hd|fhd|uhd|4k|sd|raw|vip|backup)\b", " ", n)
    if loose:
        n = re.sub(r"\b(tv|canal|channel|the|portugal|pt)\b", " ", n)
    n = re.sub(r"[^a-z0-9]", "", n)
    return RENAMES.get(n, n)


def site_key(xmltv_id):
    base, _, feed = (xmltv_id or "").partition("@")
    return base if (not feed or feed in GENERIC_FEEDS) else f"{base}@{feed}"


def load_site(repo, site):
    path = os.path.join(repo, "sites", site, f"{site}.channels.xml")
    out = []
    for el in ET.parse(path).getroot().iter("channel"):
        out.append(dict(site=site, site_id=el.get("site_id"), lang=el.get("lang") or "pt",
                        xmltv_id=el.get("xmltv_id") or "", name=(el.text or "").strip()))
    return out


def open_any(p):
    return gzip.open(p) if p.endswith(".gz") else open(p, "rb")


class DSU:
    def __init__(self, n): self.p = list(range(n))
    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x
    def union(self, a, b): self.p[self.find(a)] = self.find(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epg-repo", default="epg", help="path to a clone of iptv-org/epg")
    ap.add_argument("--old", help="old EPG (xml or xml.gz) whose IDs you want to keep")
    ap.add_argument("--only-old", action="store_true", help="only keep channels found in the old EPG")
    ap.add_argument("--old-suffix", default=".pt",
                    help="only reuse old IDs ending with this (default .pt; use '' for all)")
    ap.add_argument("--channels-out", default="channels.xml")
    ap.add_argument("--aliases-out", default="aliases.json")
    args = ap.parse_args()

    entries = []
    for s in SITES:
        entries += load_site(args.epg_repo, s)
    for s, sid in EXTRA:
        entries += [e for e in load_site(args.epg_repo, s) if e["site_id"] == sid]

    # ---- group the same channel across sites
    dsu, by_name, by_id = DSU(len(entries)), {}, {}
    for i, e in enumerate(entries):
        for key, table in ((norm(e["name"]), by_name), (site_key(e["xmltv_id"]).lower(), by_id)):
            if not key:
                continue
            if key in table:
                dsu.union(i, table[key])
            else:
                table[key] = i
    groups = OrderedDict()
    for i in range(len(entries)):
        groups.setdefault(dsu.find(i), []).append(i)

    strict_index, loose_index = {}, {}
    for g, members in groups.items():
        for i in members:
            e = entries[i]
            for cand in (e["name"], site_key(e["xmltv_id"]).split(".")[0]):
                strict_index.setdefault(norm(cand), g)
                loose_index.setdefault(norm(cand, loose=True), g)

    # ---- map the old EPG ids onto the groups
    old_ids = {g: [] for g in groups}
    old_names = {g: [] for g in groups}
    unmatched = []
    if args.old:
        for _, el in ET.iterparse(open_any(args.old)):
            if el.tag == "channel":
                oid = el.get("id")
                if args.old_suffix and not oid.lower().endswith(args.old_suffix.lower()):
                    el.clear()
                    continue
                names = [d.text for d in el.findall("display-name") if d.text]
                g = None
                cands = names + [re.sub(r"\.[a-z]{2}$", "", oid).replace(".", " ")]
                for idx, loose in ((strict_index, False), (loose_index, True)):
                    for c in cands:
                        g = idx.get(norm(c, loose))
                        if g is not None:
                            break
                    if g is not None:
                        break
                if g is None:
                    if oid.lower().endswith(".pt"):
                        unmatched.append(f"{oid}  ({names[0] if names else ''})")
                else:
                    old_ids[g].append(oid)
                    old_names[g] += [n for n in names if n not in old_names[g]]
            if el.tag in ("channel", "programme"):
                el.clear()

    # ---- write channels.xml + aliases.json
    used, lines, aliases = set(), [], OrderedDict()
    for g, members in groups.items():
        if args.only_old and not old_ids[g]:
            continue
        first = entries[members[0]]
        if old_ids[g]:
            cid = old_ids[g][0]
        else:
            cid = site_key(first["xmltv_id"]).replace("@", ".") or re.sub(r"\W", "", first["name"]) + ".pt"
        n = 2
        while cid in used:
            cid, n = f"{cid}{n}", n + 1
        used.add(cid)
        seen_sites = set()
        for i in members:
            e = entries[i]
            if e["site"] in seen_sites:
                continue
            seen_sites.add(e["site"])
            lines.append(
                f'  <channel site="{e["site"]}" site_id="{e["site_id"]}" lang="{e["lang"]}" '
                f'xmltv_id="{escape_attr(cid)}">{escape_text(e["name"])}</channel>')
        if old_ids[g][1:] or old_names[g]:
            aliases[cid] = {"ids": old_ids[g][1:], "names": old_names[g]}

    with open(args.channels_out, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<channels>\n' + "\n".join(lines) + "\n</channels>\n")
    with open(args.aliases_out, "w", encoding="utf-8") as fh:
        json.dump(aliases, fh, ensure_ascii=False, indent=2)

    n_ch = len({l.split('xmltv_id="')[1].split('"')[0] for l in lines})
    print(f"{n_ch} channels ({len(lines)} source entries) -> {args.channels_out}")
    print(f"{sum(len(v['ids']) + 1 for v in aliases.values())} old IDs kept -> {args.aliases_out}")
    if unmatched:
        print(f"\nOld .pt channels with NO source found ({len(unmatched)}):")
        print("\n".join("  " + u for u in unmatched))


def escape_attr(s):
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def escape_text(s):
    return s.replace("&", "&amp;").replace("<", "&lt;")


if __name__ == "__main__":
    main()
