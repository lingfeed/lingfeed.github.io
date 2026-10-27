#!/usr/bin/env python3
"""Build arxiv.json from the arXiv API (standard library only).

Fetches papers in arXiv's Computation and Language category (cs.CL, including
cross-lists), newest-updated first, and merges them into arxiv.json. arXiv supplies exact
timestamps: "published" (version 1) and "updated" (latest version), so no date estimation
is needed.

    python3 build_arxiv.py              # first run: ~1000 papers (about 5 days); later runs: only what's new
    python3 build_arxiv.py --pages 3    # cap the number of API pages (100 papers each)
    python3 build_arxiv.py --category cs.CL

arXiv's API terms ask for no more than one request every 3 seconds; this script waits
between requests. New papers become visible in the API in a daily batch after arXiv's
announcement, so running it hourly is plenty.

Every run ends with a summary line: how many papers were added and how many got a new version.
"""
import argparse, json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

API = "https://export.arxiv.org/api/query"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arxiv.json")
NS = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
PER_PAGE = 100
KEEP = 1500          # records retained (about a week of cs.CL)
MAX_SUMMARY = 700    # characters of abstract kept, to keep arxiv.json small
MAX_AUTHORS = 20

def get(url, tries=4):
    req = urllib.request.Request(url, headers={"User-Agent": "LingFeed/1.0 (preprint aggregator; https://lingfeed.github.io; lingfeedteam@gmail.com)"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:
            if i == tries - 1:
                raise
            wait = 5 * (i + 1)
            print(f"  request failed ({e}); retrying in {wait}s")
            time.sleep(wait)

def squash(s):
    return re.sub(r"\s+", " ", s or "").strip()

def parse(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = []
    for e in root.findall("a:entry", NS):
        m = re.search(r"abs/(.+?)(?:v(\d+))?$", e.findtext("a:id", "", NS))
        if not m:
            continue
        aid, ver = m.group(1), int(m.group(2) or 1)
        authors = [squash(a.findtext("a:name", "", NS)) for a in e.findall("a:author", NS)]
        summary = squash(e.findtext("a:summary", "", NS))
        if len(summary) > MAX_SUMMARY:
            summary = summary[:MAX_SUMMARY].rsplit(" ", 1)[0] + " …"
        prim = e.find("x:primary_category", NS)
        out.append({
            "id": aid, "version": ver, "title": squash(e.findtext("a:title", "", NS)),
            "authors": authors[:MAX_AUTHORS] + (["et al."] if len(authors) > MAX_AUTHORS else []),
            "summary": summary,
            "categories": [c.get("term") for c in e.findall("a:category", NS)],
            "primary": prim.get("term") if prim is not None else "",
            "published": e.findtext("a:published", "", NS), "updated": e.findtext("a:updated", "", NS),
            "url": f"https://arxiv.org/abs/{aid}", "pdf": f"https://arxiv.org/pdf/{aid}",
            "doi": squash(e.findtext("x:doi", "", NS)), "journal_ref": squash(e.findtext("x:journal_ref", "", NS)),
        })
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default="cs.CL")
    ap.add_argument("--pages", type=int, default=0, help="max API pages (default: 10 on first run, 10 later but stops early)")
    ap.add_argument("--delay", type=float, default=3.0)
    args = ap.parse_args()

    db = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            db = {r["id"]: r for r in json.load(f).get("records", [])}
    max_pages = args.pages or 10
    first_run = not db

    fetched = 0
    added = new_versions = other = 0   # for the summary line
    for page in range(max_pages):
        q = urllib.parse.urlencode({"search_query": f"cat:{args.category}", "sortBy": "lastUpdatedDate",
                                    "sortOrder": "descending", "start": page * PER_PAGE, "max_results": PER_PAGE})
        recs = parse(get(f"{API}?{q}"))
        if not recs:
            if page == 0:
                sys.exit("arXiv returned no entries; the API may be down or the response format changed.")
            break
        fetched += len(recs)
        unchanged = sum(1 for r in recs if db.get(r["id"], {}).get("updated") == r["updated"])
        for r in recs:
            old = db.get(r["id"])
            if not old:
                added += 1
            elif r["version"] > (old.get("version") or 1):
                new_versions += 1
            elif old.get("updated") != r["updated"]:
                other += 1                     # changed at arXiv without a new version
            db[r["id"]] = r
        print(f"page {page + 1}: {len(recs)} papers, {unchanged} already stored")
        if not first_run and unchanged == len(recs):
            break  # reached material we already have
        time.sleep(args.delay)

    extra = f" ({other} changed without a new version, not counted as updated)" if other else ""
    print(f"arXiv: {added} added, {new_versions} updated (new version){extra}")
    records = sorted(db.values(), key=lambda r: r["updated"], reverse=True)[:KEEP]
    if os.path.exists(OUT):  # don't rewrite (or commit) when nothing changed
        with open(OUT, encoding="utf-8") as f:
            if json.load(f).get("records") == records:
                print("no changes")
                return
    # Save safely: write a temporary file, then swap it into place, so an interrupted save leaves the old file intact.
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "records": records},
                  f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, OUT)
    print(f"{fetched} fetched, {len(records)} stored -> {OUT}")

if __name__ == "__main__":
    main()
