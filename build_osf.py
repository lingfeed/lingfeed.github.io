#!/usr/bin/env python3
"""Build osf.json from the OSF API (standard library only).

Fetches preprints tagged with the OSF subject "Linguistics" from PsyArXiv and SocArXiv (the
filters in the discover pages you linked) and merges them into osf.json. OSF supplies exact timestamps.

    python3 build_osf.py               # first run: 3 pages if OSF filters by subject, else 15; later runs stop early
    python3 build_osf.py --pages 20    # go exactly this deep, ignoring early stopping: use it to backfill older papers
                                       # (re-runnable; papers already stored cost no extra requests)
    python3 build_osf.py --debug       # print the first raw API record, to diagnose format surprises

Besides osf.json (the script's working copy), it writes what the web page reads: a small
osf-recent.json (papers posted or given a new version in the last 60 days) and a large
osf-archive.json (everything older), the same way build_lingbuzz.py does.

To add another OSF-hosted server or subject, add an entry to SOURCES below and a matching
entry in the page's JSON_SOURCES (index.html).
"""
import argparse, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.osf.io/v2/preprints/"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "osf.json")
LINGUISTICS = "584240d954be81056ceca960"   # same subject id on every OSF preprint server
SOURCES = [
    {"provider": "psyarxiv", "label": "PsyArXiv", "subject": LINGUISTICS, "subject_text": "Linguistics"},
    {"provider": "socarxiv", "label": "SocArXiv", "subject": LINGUISTICS, "subject_text": "Linguistics"},
]
PER_PAGE = 100
KEEP = 100000   # effectively unlimited; the Linguistics subject has a few thousand preprints per server
MAX_SUMMARY = 700

def get_json(url, tries=4):
    req = urllib.request.Request(url, headers={"User-Agent": "LingFeed/1.0 (preprint aggregator; https://lingfeed.github.io; lingfeedteam@gmail.com)", "Accept": "application/json"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 400 or i == tries - 1:
                raise
            wait = 5 * (i + 1)
        except Exception as e:
            if i == tries - 1:
                raise
            wait = 5 * (i + 1)
        print(f"  request failed; retrying in {wait}s")
        time.sleep(wait)

def write_json(path, obj):
    """Save safely: write a temporary file, then swap it into place, so an interrupted save
    (Control-C, a crash) leaves the previous file intact instead of a half-written one."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def list_url(src, sort, extra):
    params = {"filter[provider]": src["provider"], "page[size]": PER_PAGE, "sort": sort}
    params.update(extra)
    return f"{API}?{urllib.parse.urlencode(params)}"

def ts(s):
    """Seconds since 1970 for an OSF timestamp. OSF gives times in UTC, sometimes without saying so."""
    try:
        d = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:
        return 0.0

def matches(item, src):
    """Local subject test: any node on any of the preprint's subject paths is our subject."""
    for path in item.get("attributes", {}).get("subjects") or []:
        for node in path:
            if node.get("id") == src["subject"] or (node.get("text") or "").lower() == src["subject_text"].lower():
                return True
    return False

def probe(src):
    """Find a server-side subject filter that works; return None to filter locally instead."""
    for extra in ({"filter[subjects]": src["subject"]}, {"filter[subjects]": src["subject_text"]}):
        try:
            d = get_json(list_url(src, "-date_created", {**extra, "page[size]": 1}))
        except Exception as e:
            print(f"  filter {extra} failed: {e}")
            continue
        if d.get("data"):
            print(f"  server-side subject filter works: {extra}")
            return extra
        print(f"  filter {extra} returned nothing")
    print("  no server-side subject filter worked; scanning recent preprints and filtering locally (slower)")
    return None

def href(link):
    return link.get("href") if isinstance(link, dict) else link

def authors_of(item, delay):
    """One extra request per preprint: the contributors list (names live in the embedded user)."""
    rel = (item.get("relationships", {}).get("contributors", {}).get("links", {}) or {}).get("related")
    url = href(rel)
    if not url:
        return []
    time.sleep(delay)
    try:
        data = get_json(url + ("&" if "?" in url else "?") + "embed=users&page[size]=50")
    except Exception as e:
        print(f"    contributors failed: {e}")
        return []
    names = []
    for c in data.get("data", []):
        if c.get("attributes", {}).get("bibliographic") is False:
            continue
        user = ((c.get("embeds") or {}).get("users") or {}).get("data") or {}
        name = (user.get("attributes") or {}).get("full_name") or c.get("attributes", {}).get("unregistered_contributor")
        if name:
            names.append(name.strip())
    return names

def parse(item, src):
    a = item.get("attributes", {})
    if a.get("is_latest_version") is False:
        return None
    pid = item["id"]
    m = re.match(r"(.+?)_v(\d+)$", pid)
    base = m.group(1) if m else pid
    version = int(m.group(2)) if m else int(a.get("version") or 1)
    leafs = [p[-1].get("text") for p in (a.get("subjects") or []) if p]
    keywords = list(dict.fromkeys(k for k in [*leafs, *(a.get("tags") or [])] if k))[:15]
    links = item.get("links", {})
    pdoi = href(links.get("preprint_doi")) or ""
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", pdoi) or (a.get("doi") or "")
    pf = ((item.get("relationships", {}).get("primary_file") or {}).get("data") or {}).get("id")
    summary = re.sub(r"\s+", " ", a.get("description") or "").strip()
    if len(summary) > MAX_SUMMARY:
        summary = summary[:MAX_SUMMARY].rsplit(" ", 1)[0] + " …"
    return {"id": base, "version": version, "title": re.sub(r"\s+", " ", a.get("title") or "").strip(),
            "summary": summary, "keywords": keywords, "provider": src["label"],
            "published": a.get("date_published") or a.get("date_created"), "updated": a.get("date_modified") or a.get("date_created"),
            "url": href(links.get("html")) or f"https://osf.io/preprints/{src['provider']}/{pid}",
            "pdf": f"https://osf.io/download/{pf}/" if pf else None, "doi": doi}

RECENT_DAYS = 60   # same period as LingBuzz's recent file; the page uses the "since" date written into the file

def write_public(records, now):
    """Write what the web page reads: a small osf-recent.json and a large osf-archive.json.
    A paper is "recent" if it was first posted, or given a new version, within RECENT_DAYS.
    The page always loads the recent file, and the archive only when someone searches or asks for older papers.
    osf.json itself stays the script's working copy and is not read by the page."""
    cutoff = now - timedelta(days=RECENT_DAYS)
    cut = cutoff.timestamp()
    recent, archive = [], []
    for r in records:
        listed_recently = ts(r.get("published")) >= cut or ((r.get("version") or 1) > 1 and ts(r.get("updated")) >= cut)
        (recent if listed_recently else archive).append(r)
    recent.sort(key=lambda r: ts(r.get("updated")), reverse=True)
    archive.sort(key=lambda r: ts(r.get("published")), reverse=True)
    since = cutoff.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    for name, recs, extra in (("osf-recent.json", recent, {"since": since}), ("osf-archive.json", archive, {})):
        path = os.path.join(os.path.dirname(OUT), name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("records") == recs and old.get("since") == extra.get("since"):
                continue   # unchanged: don't rewrite (or commit) the file
        write_json(path, {"generated": now.isoformat(timespec="seconds"), **extra, "records": recs})
    print(f"page files: {len(recent)} recent, {len(archive)} archive")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=0, help="pages (100 preprints each) per source; when given, no early stopping")
    ap.add_argument("--delay", type=float, default=0.7)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    db = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            db = {r["id"]: r for r in json.load(f).get("records", [])}
    counts = {}   # per server: papers added, given a new version, or otherwise changed in this run
    for src in SOURCES:
        mine = [r for r in db.values() if r.get("provider") == src["label"]]
        first_run = not mine   # per server, so adding a server later backfills it
        db_max = max((ts(r.get("updated")) for r in mine), default=0.0)
        print(f"{src['label']}: looking for a subject filter ...")
        extra = probe(src)
        server_side = extra is not None
        max_pages = args.pages or ((3 if server_side else 15) if first_run else 5)
        sort = "-date_modified"
        url = list_url(src, sort, extra or {})
        scanned = matched_n = 0
        for page in range(max_pages):
            try:
                data = get_json(url)
            except urllib.error.HTTPError as e:
                if e.code == 400 and page == 0 and sort != "-date_created":
                    sort = "-date_created"
                    url = list_url(src, sort, extra or {})
                    print("  sort by date_modified rejected; falling back to date_created")
                    data = get_json(url)
                else:
                    raise
            items = data.get("data", [])
            if page == 0 and not items:
                sys.exit(f"OSF returned no preprints for provider '{src['provider']}'; check the provider name in SOURCES.")
            scanned += len(items)
            hits = [it for it in items if server_side or matches(it, src)]
            matched_n += len(hits)
            if args.debug and hits and matched_n == len(hits):
                print(json.dumps(hits[0], indent=1)[:3000])
            for item in hits:
                rec = parse(item, src)
                if rec is None:
                    continue
                old = db.get(rec["id"])
                if old and old.get("updated") == rec["updated"] and old.get("authors"):
                    continue
                same = old and old.get("updated") == rec["updated"]
                rec["authors"] = old.get("authors") if same else authors_of(item, args.delay)
                if old and old.get("published"):
                    rec["published"] = old["published"]  # keep the first-seen publication date across new versions
                db[rec["id"]] = rec
                c = counts.setdefault(src["label"], {"added": 0, "new version": 0, "other": 0})
                if not old:
                    c["added"] += 1
                elif rec["version"] > (old.get("version") or 1):
                    c["new version"] += 1
                elif not same:
                    c["other"] += 1                      # title, abstract or keywords edited, no new version
            print(f"  page {page + 1}: scanned {scanned} so far, {matched_n} in {src['subject_text']}")
            nxt = href((data.get("links") or {}).get("next"))
            reached_known = not first_run and not args.pages and all(ts(it.get("attributes", {}).get("date_modified")) <= db_max for it in items)
            if not nxt or reached_known:
                break
            url = nxt
            time.sleep(args.delay)
        if not matched_n and first_run:
            print(f"  nothing matched in {scanned} preprints scanned; try a larger --pages (e.g. --pages 40)")

    now = datetime.now(timezone.utc)
    records = sorted(db.values(), key=lambda r: r["updated"] or "", reverse=True)[:KEEP]
    write_public(records, now)   # always, so the page files exist even when osf.json itself is unchanged
    for src in SOURCES:
        c = counts.get(src["label"], {"added": 0, "new version": 0, "other": 0})
        extra = f" ({c['other']} edited without a new version, not counted as updated)" if c["other"] else ""
        print(f"{src['label']}: {c['added']} added, {c['new version']} updated (new version){extra}")
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            if json.load(f).get("records") == records:
                print("no changes")
                return
    write_json(OUT, {"generated": now.isoformat(timespec="seconds"), "records": records})
    per = {}
    for r in records:
        per[r.get("provider", "?")] = per.get(r.get("provider", "?"), 0) + 1
    print(f"{len(records)} stored ({', '.join(f'{k} {v}' for k, v in sorted(per.items()))}) -> {OUT}")

if __name__ == "__main__":
    main()
