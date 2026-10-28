#!/usr/bin/env python3
"""Report how far back each LingFeed source reaches. Run from the lingfeed folder:

    python3 coverage.py             # reads lingbuzz.json, arxiv.json, osf.json
    python3 coverage.py --online    # also asks Zenodo and OSF for their totals (a handful of requests)

Prints counts and dates only (no titles or authors), so the output is safe to paste into a chat.
"""
import argparse, collections, json, os, urllib.parse, urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))

def load(name):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def day(s):
    return (s or "")[:10] or "-"

def summarize(label, recs, created_key, updated_key, extra=None):
    print(f"\n== {label}")
    if not recs:
        print("   no records")
        return
    cr = [r[created_key] for r in recs if r.get(created_key)]
    up = [r[updated_key] for r in recs if r.get(updated_key)]
    print(f"   records: {len(recs)}")
    if cr:
        print(f"   creation dates:    oldest {day(min(cr))}   newest {day(max(cr))}   ({len(cr)} have one)")
    if up:
        print(f"   last-update dates: oldest {day(min(up))}   newest {day(max(up))}")
    if extra:
        extra(recs)
    months = collections.Counter(day(r.get(created_key))[:7] for r in recs if r.get(created_key))
    if months:
        print("   papers per creation month: " + ", ".join(f"{m}:{months[m]}" for m in sorted(months)[-12:]))
        if len(months) > 12:
            print(f"   (showing the latest 12 of {len(months)} months)")

def lingbuzz_extra(recs):
    src = collections.Counter(r.get("created_src") or "none" for r in recs)
    print(f"   creation-date source: {dict(src)}   (observed = seen arriving; estimated = from month)")

def online(args):
    print("\n== Online totals")
    for comm in ("linguistics", "langscipress", "lingfeed"):
        try:
            q = urllib.parse.urlencode({"communities": comm, "size": 1, "sort": "oldest"})
            req = urllib.request.Request(f"https://zenodo.org/api/records?{q}", headers={"User-Agent": "LingFeed/1.0 (preprint aggregator; https://lingfeed.github.io; lingfeedteam@gmail.com)", "Accept": "application/json"})
            d = json.load(urllib.request.urlopen(req, timeout=30))
            hits = d.get("hits", {}).get("hits", [])
            oldest = day(hits[0].get("created")) if hits else "?"
            print(f"   Zenodo {comm}: {d.get('hits', {}).get('total')} records; oldest created {oldest}")
        except Exception as e:
            print(f"   Zenodo {comm}: failed ({e})")
    for prov in ("psyarxiv", "socarxiv"):
        try:
            q = urllib.parse.urlencode({"filter[provider]": prov, "filter[subjects]": "584240d954be81056ceca960", "page[size]": 1})
            req = urllib.request.Request(f"https://api.osf.io/v2/preprints/?{q}", headers={"User-Agent": "LingFeed/1.0 (preprint aggregator; https://lingfeed.github.io; lingfeedteam@gmail.com)", "Accept": "application/json"})
            d = json.load(urllib.request.urlopen(req, timeout=30))
            total = (d.get("meta") or {}).get("total") or ((d.get("links") or {}).get("meta") or {}).get("total")
            print(f"   OSF {prov} (Linguistics filter): total {total}")
        except Exception as e:
            print(f"   OSF {prov}: failed ({e})")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--online", action="store_true")
    args = ap.parse_args()
    print(f"LingFeed coverage report, generated {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    d = load("lingbuzz.json")
    if d is None:
        print("\n== LingBuzz\n   lingbuzz.json not found")
    else:
        summarize("LingBuzz", d["records"], "created", "updated", lingbuzz_extra)
        print(f"   file last changed: {day(d.get('generated'))}")

    d = load("arxiv.json")
    if d is None:
        print("\n== arXiv\n   arxiv.json not found")
    else:
        summarize("arXiv (cs.CL)", d["records"], "published", "updated")
        print(f"   file last changed: {day(d.get('generated'))}")

    d = load("osf.json")
    if d is None:
        print("\n== OSF\n   osf.json not found")
    else:
        by = collections.defaultdict(list)
        for r in d["records"]:
            by[r.get("provider", "?")].append(r)
        for prov in sorted(by):
            summarize(f"OSF: {prov}", by[prov], "published", "updated")
        print(f"   file last changed: {day(d.get('generated'))}")

    if args.online:
        online(args)

if __name__ == "__main__":
    main()
