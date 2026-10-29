#!/usr/bin/env python3
"""Build feed.xml: LingFeed as an RSS feed (standard library only).

    python3 build_feed.py

The feed lists papers first posted, or given a new version, in the last FEED_DAYS days (at most
MAX_ITEMS), newest first, from the same sources the site shows by default: Zenodo (Linguistics,
Language Science Press and LingFeed), LingBuzz, PsyArXiv and SocArXiv. arXiv is left out, as on the site, because
its ~200 papers a day would swamp the feed. A paper given a new version appears again, titled
"Updated: ...".

LingBuzz, OSF and Zenodo papers come from the data files the other scripts write (lingbuzz-recent.json,
osf-recent.json, zenodo-recent.json); if the Zenodo file is missing or old, Zenodo is asked directly.

Each item's link goes to go.html on the site, which counts the click like a click on the site and then
forwards the reader to the paper. So SITE below must be the site's real address.

feed.xml is only rewritten when its list of papers changes. feed.xsl (next to it) makes the feed
readable when someone opens it in a browser.
"""
import html, json, os, re, sys, unicodedata, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

SITE = "https://lingfeed.github.io"     # the site’s address, without a trailing slash
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "feed.xml")
FEED_DAYS = 14
MAX_ITEMS = 150
MAX_SUMMARY = 700
ZENODO = "https://zenodo.org/api/records"
COMMUNITIES = [("linguistics", "Zenodo"), ("langscipress", "Zenodo"), ("lingfeed", "Zenodo")]   # all listed simply as "Zenodo"

def when(s):
    """A timestamp from the data, as an aware datetime (the data is in UTC, sometimes without saying so)."""
    try:
        d = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

def short(s):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= MAX_SUMMARY else s[:MAX_SUMMARY].rsplit(" ", 1)[0] + " …"

def item(id_, title, authors, url, listed, updated, source, summary, version_key):
    return {"id": id_, "title": title, "authors": authors, "url": url, "date": listed, "updated": updated,
            "source": source, "summary": short(summary), "guid": f"lingfeed:{id_}:{version_key}"}

def read_json(name):
    path = os.path.join(HERE, name)
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def lingbuzz_items():
    out = []
    for r in read_json("lingbuzz-recent.json").get("records", []):
        changed = r["revised"] if "revised" in r else r.get("status") == "changed"
        listed = when(r.get("updated") if changed else r.get("created"))
        if not listed:
            continue
        pid = r["id"]
        out.append(item("l" + pid, r.get("title", ""), r.get("authors") or [], f"https://lingbuzz.net/lingbuzz/{pid}",
                        listed, changed, "LingBuzz", r.get("abstract", ""), r.get("updated") if changed else "v1"))
    return out

def osf_items():
    out = []
    for r in read_json("osf-recent.json").get("records", []):
        changed = (r.get("version") or 1) > 1
        listed = when(r.get("updated") if changed else r.get("published"))
        if not listed or not r.get("url"):
            continue
        out.append(item("o" + r["id"], r.get("title", ""), r.get("authors") or [], r["url"], listed, changed,
                        r.get("provider") or "OSF", r.get("summary", ""), f"v{r.get('version') or 1}"))
    return out

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "LingFeed/1.0 (preprint aggregator; https://lingfeed.github.io; lingfeedteam@gmail.com)", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:   # include what the server said, to make problems easy to diagnose
        detail = re.sub(r"\s+", " ", e.read().decode("utf-8", "replace"))[:200]
        raise RuntimeError(f"HTTP {e.code} from {url.split('?')[0]}: {detail}") from None

ZENODO_PAGE = 25        # Zenodo allows at most 25 records per request without an account
ZENODO_MAX_PAGES = 20   # safety limit per community (500 records)

def zenodo_hits(cid, cutoff):
    """A community's records, newest first, page by page until they are older than the feed reaches back."""
    for page in range(1, ZENODO_MAX_PAGES + 1):
        q = urllib.parse.urlencode({"communities": cid, "sort": "newest", "size": ZENODO_PAGE, "page": page})
        hits = get_json(f"{ZENODO}?{q}").get("hits", {}).get("hits", [])
        yield from hits
        oldest = when(hits[-1].get("created")) if hits else None
        if len(hits) < ZENODO_PAGE or (oldest and oldest < cutoff):
            return

def zenodo_file_hits(cid):
    """A community's records from zenodo-recent.json (written by build_zenodo.py), each paper's latest version only,
    or None if the file is missing or more than two days old (then Zenodo is asked directly)."""
    try:
        d = read_json("zenodo-recent.json")
    except (OSError, ValueError):
        return None
    made = when(d.get("generated"))
    f = (d.get("communities") or {}).get(cid)
    if not made or datetime.now(timezone.utc) - made > timedelta(days=2) or f is None:
        return None
    def vidx(h):
        rel = ((h.get("metadata") or {}).get("relations") or {}).get("version")
        return rel[0].get("index", 0) if rel else (h.get("versions") or {}).get("index", 1) - 1
    best = {}
    for h in f.get("records", []):
        concept = h.get("conceptrecid") or (h.get("parent") or {}).get("id") or h["id"]
        b = best.get(concept)
        if b is None or vidx(h) > vidx(b):
            best[concept] = h
    return sorted(best.values(), key=lambda h: h.get("created") or "", reverse=True)

def zenodo_items(cutoff):
    """Each paper's latest version, as on the page's "New and Updated" view (Zenodo's default)."""
    seen = {}
    for cid, label in COMMUNITIES:
        hits = zenodo_file_hits(cid)
        if hits is None:
            hits = zenodo_hits(cid, cutoff)
        for h in hits:
            if h["id"] in seen:
                continue
            m = h.get("metadata") or {}
            vi = (m.get("relations") or {}).get("version", [{}])[0].get("index") if (m.get("relations") or {}).get("version") else None
            ver = vi if vi is not None else ((h.get("versions") or {}).get("index", 1) - 1)
            listed = when(h.get("created"))
            if not listed:
                continue
            authors = [c.get("name") or (c.get("person_or_org") or {}).get("name") or "" for c in m.get("creators") or []]
            desc = html.unescape(re.sub(r"<[^>]+>", " ", m.get("description") or ""))
            # counted under the number shared by all versions (as on the page); the link goes to this version
            concept = h.get("conceptrecid") or (h.get("parent") or {}).get("id") or h["id"]
            seen[h["id"]] = item(f"z{concept}", m.get("title") or "(untitled)", [a for a in authors if a],
                                 f"https://zenodo.org/records/{h['id']}", listed, ver >= 1, label, desc, f"r{h['id']}")
    return list(seen.values())

# The only addresses go.html will forward to (the same patterns as in go.html and counter/worker.js).
# A paper whose address doesn't fit is given its source's plain address instead, or left out.
LINK_OK = {
    "z": lambda id_, url: re.fullmatch(r"https://zenodo\.org/records/\d{1,12}", url),
    "l": lambda id_, url: re.fullmatch(rf"https://lingbuzz\.net/lingbuzz/{re.escape(id_[1:])}", url),
    "o": lambda id_, url: re.fullmatch(rf"https://osf\.io/(preprints/[a-z]+/)?{re.escape(id_[1:])}(_v\d+)?/?", url),
}
ID_OK = {"z": r"z\d{1,12}", "l": r"l\d{6}", "o": r"o[a-z0-9]{5}"}

def checked(it):
    """The item with an address go.html accepts, or None if there is no such address."""
    kind = it["id"][:1]
    if not re.fullmatch(ID_OK.get(kind, r"(?!)"), it["id"]):
        return None
    if LINK_OK[kind](it["id"], it["url"]):
        return it
    if kind == "o":                       # OSF changed its address format: its plain address always works
        print(f"  note: unexpected OSF address {it['url']}; using https://osf.io/{it['id'][1:]}")
        return {**it, "url": f"https://osf.io/{it['id'][1:]}"}
    return None

def fold_words(s):
    """Lower-case words without accents or punctuation, as the page compares them."""
    s = unicodedata.normalize("NFD", (s or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.findall(r"[^\W_]+", s)

def merge_cross_posts(items):
    """Show a paper posted on two sources once, with the same rule as the page: same title (ignoring capitals,
    accents and punctuation; at least 3 words and 20 letters), a shared author name, different sources.
    The item links to the copy not on LingBuzz (the earliest, if several) and names every source."""
    groups, out = {}, []
    for it in items:
        words = fold_words(it["title"])
        key = " ".join(words)
        if len(words) < 3 or len(key) < 20:
            out.append([it]); continue
        names = {w for a in it["authors"] for w in fold_words(a) if len(w) >= 3}
        it["_names"] = names
        for g in groups.setdefault(key, []):
            if all(x["source"] != it["source"] for x in g) and any(x["_names"] & names for x in g):
                g.append(it); break
        else:
            g = [it]; groups[key].append(g); out.append(g)
    merged = []
    for g in out:
        if len(g) == 1:
            merged.append(g[0]); continue
        others = [x for x in g if x["source"] != "LingBuzz"]
        primary = min(others or g, key=lambda x: x["date"])
        newer = [x for x in g if x["updated"]]
        rest = sorted((x for x in g if x is not primary), key=lambda x: x["source"] == "LingBuzz")   # LingBuzz last
        merged.append({**primary,
                       "date": max(x["date"] for x in newer) if newer else min(x["date"] for x in g),
                       "updated": bool(newer),
                       "source": " + ".join([primary["source"]] + [x["source"] for x in rest]),
                       "summary": primary["summary"] or next((x["summary"] for x in rest if x["summary"]), "")})
    return merged

def go_link(it):
    p = {"id": it["id"], "to": it["url"], "t": it["title"][:300], "a": "; ".join(it["authors"][:6])}
    return f"{SITE}/go.html?{urllib.parse.urlencode(p)}"

def render_items(items):
    out = []
    for it in items:
        title = ("Updated: " if it["updated"] else "") + it["title"]
        byline = " · ".join(x for x in ("; ".join(it["authors"]), it["source"]) if x)
        desc = f"<p>{html.escape(byline)}</p>" + (f"<p>{html.escape(it['summary'])}</p>" if it["summary"] else "")
        out.append(
            "<item>"
            f"<title>{escape(title)}</title>"
            f"<link>{escape(go_link(it))}</link>"
            f'<guid isPermaLink="false">{escape(it["guid"])}</guid>'
            f"<pubDate>{format_datetime(it['date'].astimezone(timezone.utc), usegmt=True)}</pubDate>"
            f"<category>{escape(it['source'])}</category>"
            + "".join(f"<dc:creator>{escape(a)}</dc:creator>" for a in it["authors"][:20]) +
            f"<description>{escape(desc)}</description>"
            "</item>")
    return "\n".join(out)

def main():
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=FEED_DAYS)
    items, counts = [], {}
    for name, fn in (("LingBuzz", lingbuzz_items), ("PsyArXiv and SocArXiv", osf_items), ("Zenodo", lambda: zenodo_items(cutoff))):
        try:
            got = fn()
        except Exception as e:
            # Rather than drop a whole source from the feed, keep the previous feed and try again next hour.
            kept = "feed.xml left as it was" if os.path.exists(OUT) else "no feed written"
            print(f"{name} could not be read ({e}); {kept}")
            return
        counts[name] = len(got)
        items += got
    items = [c for c in map(checked, items) if c]
    items = merge_cross_posts([it for it in items if it["date"] >= cutoff])
    items = sorted((it for it in items if it["date"] >= cutoff and it["date"] <= now + timedelta(hours=1)),
                   key=lambda it: it["date"], reverse=True)[:MAX_ITEMS]
    body = render_items(items)
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            old = f.read()
        old_items = old[old.find("<item>"): old.rfind("</item>") + len("</item>")] if "<item>" in old else ""
        if old_items == body:                    # same papers: don't rewrite (or commit) just for a new build date
            print(f"feed: no changes ({len(items)} papers)")
            return
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<?xml-stylesheet type="text/xsl" href="feed.xsl"?>\n'
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
           "<channel>\n"
           "<title>LingFeed</title>\n"
           f"<link>{SITE}/</link>\n"
           f'<atom:link href="{SITE}/feed.xml" rel="self" type="application/rss+xml"/>\n'
           "<description>New linguistics papers and preprints from LingBuzz, Zenodo, PsyArXiv and SocArXiv, in one feed.</description>\n"
           "<language>en</language>\n"
           f"<lastBuildDate>{format_datetime(now, usegmt=True)}</lastBuildDate>\n"
           "<ttl>60</ttl>\n"
           + body + "\n</channel>\n</rss>\n")
    tmp = OUT + ".tmp"   # save safely: write a temporary file, then swap it into place
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(xml)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, OUT)
    print(f"feed: {len(items)} papers from the last {FEED_DAYS} days "
          f"(of {', '.join(f'{k} {v}' for k, v in counts.items())} recent) -> {OUT}")

if __name__ == "__main__":
    main()
