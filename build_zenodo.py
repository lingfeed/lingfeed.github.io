#!/usr/bin/env python3
"""Build zenodo-recent.json: Zenodo's recent records, so the page's first screen needn't ask Zenodo live.

    python3 build_zenodo.py

For each Zenodo community the site shows (Linguistics, Language Science Press) it saves every record created
in the last RECENT_DAYS days, ALL versions (the page's "New" view needs first versions, "New and Updated" the
latest ones; the page picks). Records are trimmed to the fields the page uses, in the same shape Zenodo sends,
so the page reads them exactly as it reads Zenodo's own answers. It also saves each community's totals.

The page uses this file for its first screen when there is no search and no type chosen, and if it is less than
two days old; otherwise it asks Zenodo live, as before. Searches, the type menu and "Load more" past the recent
period always ask Zenodo live. build_feed.py also reads this file.

The file is only rewritten when its contents change. Its "since" date moves daily, so it changes at least daily.
Zenodo answers at most 25 records per request without an account; this script waits a moment between requests.
"""
import html, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

API = "https://zenodo.org/api/records"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "zenodo-recent.json")
COMMUNITIES = ["linguistics", "langscipress"]   # same ids as COMMUNITIES in index.html
RECENT_DAYS = 60       # same period as the other sources' recent files
PAGE = 25              # Zenodo's limit per request without an account
MAX_PAGES = 120        # safety limit per community (3,000 records)
DELAY = 1.0            # seconds between requests
MAX_DESC = 2000        # characters of abstract kept

def get_json(url, tries=4):
    req = urllib.request.Request(url, headers={"User-Agent": "LingFeed/1.0 (preprint aggregator)", "Accept": "application/json"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = re.sub(r"\s+", " ", e.read().decode("utf-8", "replace"))[:200]
            if e.code == 429 and i < tries - 1:          # too many requests: wait and try again
                print("  Zenodo asked us to slow down; waiting 60s")
                time.sleep(60)
                continue
            if e.code >= 500 and i < tries - 1:
                time.sleep(10 * (i + 1))
                continue
            raise RuntimeError(f"HTTP {e.code} from Zenodo: {detail}") from None
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(10 * (i + 1))

def when(s):
    try:
        d = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

def pdf_of(h):
    """The record's first PDF, found the same way the page finds it."""
    f = h.get("files")
    if isinstance(f, list):
        files = [(x.get("key") or x.get("filename"), (x.get("links") or {}).get("self") or (x.get("links") or {}).get("download")) for x in f]
    elif isinstance(f, dict) and f.get("entries"):
        files = [(x.get("key"), (x.get("links") or {}).get("content")
                  or f"https://zenodo.org/records/{h['id']}/files/{urllib.parse.quote(x.get('key') or '')}") for x in f["entries"].values()]
    else:
        files = []
    return next(((n, u) for n, u in files if n and u and n.lower().endswith(".pdf")), None)

def trim(h):
    """Keep only what the page uses (normZ in index.html), in Zenodo's own shape."""
    m = h.get("metadata") or {}
    rt = (m.get("resource_type") or {}).get("title")
    rt = rt.get("en") if isinstance(rt, dict) else rt
    kw = m.get("keywords") or [s.get("subject") for s in m.get("subjects") or [] if s.get("subject")]
    desc = html.unescape(re.sub(r"<[^>]+>", " ", m.get("description") or ""))
    desc = re.sub(r"\s+", " ", desc).strip()
    if len(desc) > MAX_DESC:
        desc = desc[:MAX_DESC].rsplit(" ", 1)[0] + " …"
    md = {"title": m.get("title"),
          "creators": [{"name": c.get("name") or (c.get("person_or_org") or {}).get("name") or ""} for c in m.get("creators") or []],
          "resource_type": {"title": rt or ""}, "keywords": kw or [], "description": html.escape(desc, quote=False)}
    if (m.get("journal") or {}).get("title"):
        md["journal"] = {"title": m["journal"]["title"]}
    if m.get("doi"):
        md["doi"] = m["doi"]
    ver = ((m.get("relations") or {}).get("version") or [None])[0]
    if ver and ver.get("index") is not None:
        md["relations"] = {"version": [{"index": ver["index"]}]}
    out = {"id": h["id"], "created": h.get("created"), "metadata": md,
           "links": {"self_html": (h.get("links") or {}).get("self_html") or (h.get("links") or {}).get("html")}}
    for k in ("conceptrecid", "doi"):
        if h.get(k):
            out[k] = h[k]
    if (h.get("parent") or {}).get("id"):
        out["parent"] = {"id": h["parent"]["id"]}
    if (h.get("versions") or {}).get("index") is not None:
        out["versions"] = {"index": h["versions"]["index"]}
    pdf = pdf_of(h)
    if pdf:
        out["files"] = [{"key": pdf[0], "links": {"self": pdf[1]}}]
    return out

def community(cid, cutoff):
    """All versions created since `cutoff`, newest first, plus the totals the page shows."""
    q = urllib.parse.urlencode({"communities": cid, "size": 1})
    total_latest = get_json(f"{API}?{q}").get("hits", {}).get("total")
    records, total_all, complete_since = [], None, cutoff
    for page in range(1, MAX_PAGES + 1):
        time.sleep(DELAY)
        q = urllib.parse.urlencode({"communities": cid, "sort": "newest", "size": PAGE, "page": page, "all_versions": "true"})
        d = get_json(f"{API}?{q}")
        hits = d.get("hits", {}).get("hits", [])
        if total_all is None:
            total_all = d.get("hits", {}).get("total")
        records += [trim(h) for h in hits if (when(h.get("created")) or cutoff) >= cutoff]
        oldest = when(hits[-1].get("created")) if hits else None
        if len(hits) < PAGE or (oldest and oldest < cutoff):
            break
    else:   # safety limit reached: the file is complete only back to its oldest record
        complete_since = min(when(r["created"]) for r in records)
        print(f"  {cid}: stopped after {MAX_PAGES} pages; complete back to {complete_since:%Y-%m-%d}")
    return {"total_latest": total_latest, "total_all": total_all,
            "complete_since": complete_since.isoformat(timespec="seconds"), "records": records}

def main():
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=RECENT_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)
    old = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                old = json.load(f)
        except ValueError:
            old = {}
    comms = {}
    for cid in COMMUNITIES:
        try:
            comms[cid] = community(cid, cutoff)
        except Exception as e:
            # Keep the previous file rather than save one with a community missing; once it is two days old the
            # page asks Zenodo live instead. To avoid an email every 15 minutes during a Zenodo outage, the run
            # only fails (= one email) once a day, at the first run after midnight UTC, if the file is over a day old.
            print(f"Zenodo {cid} could not be read ({e}); zenodo-recent.json left as it was")
            made = when(old.get("generated"))
            if (not made or now - made > timedelta(days=1)) and now.hour == 0 and now.minute < 15:
                print("The Zenodo file is more than a day old.")
                sys.exit(1)
            return
    for cid, c in comms.items():
        before = {r["id"] for r in (old.get("communities") or {}).get(cid, {}).get("records", [])}
        added = sum(1 for r in c["records"] if r["id"] not in before) if before else len(c["records"])
        print(f"Zenodo {cid}: {len(c['records'])} records from the last {RECENT_DAYS} days ({added} new since the last run)")
    data = {"since": cutoff.isoformat(timespec="seconds"), "communities": comms}
    if {k: old.get(k) for k in data} == data:
        print("no changes")
        return
    tmp = OUT + ".tmp"   # save safely: write a temporary file, then swap it into place
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"generated": now.isoformat(timespec="seconds"), **data}, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, OUT)
    print(f"-> {OUT}")

if __name__ == "__main__":
    main()
