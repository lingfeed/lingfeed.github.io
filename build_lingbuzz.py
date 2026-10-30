#!/usr/bin/env python3
"""Build lingbuzz.json from LingBuzz (standard library only).

Normal run (schedule hourly):
    python3 build_lingbuzz.py
  Scrapes the ~30 papers on the front page and merges them into lingbuzz.json.
  LingBuzz shows no dates there, so LingFeed records when it FIRST SAW each paper as
  new ("created") or freshly changed ("updated"), tagged "observed" (accurate to within the
  scrape interval). Papers from the very first run, and backfilled ones, are tagged
  "estimated" and dated from their month instead. Run every ~15 minutes for best precision.

One-off history backfill:
    python3 build_lingbuzz.py --backfill 3
  Walks N pages (100 papers each) of LingBuzz's listing, beyond the front page. For each
  paper not yet stored it fetches the paper page to read its month ("October 2026") and
  keywords. LingBuzz publishes only months, so dates are ESTIMATED: the creation month comes
  from the paper's version history (v1), the update month from its latest version, and papers
  are spread across each month in id order (ids are sequential). Polite delay between requests (--delay).
  Safe to re-run; it resumes where the stored data ends and skips known papers.

Abstracts (shown on the page like Zenodo's):
  Whenever the scraper reads a paper's own page (new papers, new versions, edits, gap papers, backfill), it
  keeps the abstract printed there, and its "Published in" text (to recognise theses later), in lingbuzz-abstracts.json. Older papers' abstracts are fetched once, by hand:
    python3 build_lingbuzz.py --abstracts        # every stored paper not yet done (~10,000 pages, ~4 hours)
    python3 build_lingbuzz.py --abstracts 300    # only the 300 most recently listed papers
  This only reads paper pages and writes lingbuzz-abstracts-old.json (nothing else), so it can run on your own
  computer while the scheduled job keeps running; Control-C stops it, and running it again continues.
  Upload that one file afterwards. The page files get the abstracts at the job's next run: in full for the
  last 60 days, shortened to ARCHIVE_ABSTRACT_CHARS for older papers (with a link to the rest).

Every run ends with a summary line: how many papers were added and how many got a new version.
"""
import argparse, bisect, calendar, html, json, os, re, statistics, sys, time, urllib.request
from datetime import datetime, timedelta, timezone

SOURCE = "https://lingbuzz.net"
LISTING = "https://ling.auf.net/lingbuzz/_listing?community=The%20Buzz&start={start}"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lingbuzz.json")
ABSTRACTS = os.path.join(os.path.dirname(OUT), "lingbuzz-abstracts.json")          # written by the scheduled job
ABSTRACTS_OLD = os.path.join(os.path.dirname(OUT), "lingbuzz-abstracts-old.json")  # written only by --abstracts
ARCHIVE_ABSTRACT_CHARS = 700   # older papers' abstracts are shortened to this in lingbuzz-archive.json (size)
ABS_SEEN = {}                  # abstracts read in this run, by paper id ("" = the page has none)
PUB_SEEN = {}                  # "Published in" texts read in this run, by paper id ("" = none)
KEEP = 50000  # effectively unlimited: LingBuzz has about 10,500 papers in total
MONTHS = {m: i for i, m in enumerate(calendar.month_name) if m}
MONTH_RE = re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(\d{4})\b")

def fetch(url, tries=4):
    req = urllib.request.Request(url, headers={"User-Agent": "LingFeed/1.0 (preprint aggregator; https://lingfeed.github.io; lingfeedteam@gmail.com)"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            if i == tries - 1:
                raise
            wait = 10 * (i + 1)
            print(f"  request failed ({e}); retrying in {wait}s")
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

def text(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()

def parse(page, require_status=True):
    """Return papers in page order (newest first)."""
    out = []
    for row in re.split(r"<tr\b", page)[1:]:
        m = re.search(r'<a[^>]+href="[^"]*?/lingbuzz/(\d{3,7})(?:\?[^"]*)?"[^>]*>(.*?)</a>', row, re.S)
        if not m:
            continue
        pid, title = m.group(1), text(m.group(2))
        if not title or title.lower() in ("[pdf]", "pdf"):
            continue
        authors = [text(a) for a in re.findall(r'<a[^>]+_person/[^"]*"[^>]*>(.*?)</a>', row, re.S)]
        low = row.lower()
        status = "changed" if "freshly changed" in low else "new" if re.search(r">\s*new\s*<", low) else None
        if status is None and require_status:
            continue
        out.append({"id": pid, "title": title, "authors": [a for a in authors if a], "status": status,
                    "url": f"{SOURCE}/lingbuzz/{pid}", "pdf": f"{SOURCE}/lingbuzz/{pid}/current.pdf"})
    return out

def spaced_text(fragment):
    """Like text(), but tags become spaces so adjacent words don't fuse."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()

def abstract_of(page):
    """The abstract on a paper's own page: the text between the title block (</center>) and the table that
    starts with "Format:". "" if there is none. LaTeX-style ``quotes'' become curly quotes."""
    m = re.search(r"</center>(.*?)<table", page, re.S | re.I)
    if not m:
        return ""
    s = spaced_text(m.group(1)).replace("``", "\u201c").replace("''", "\u201d")
    return s[:10000] if len(s) >= 20 else ""

def published_of(page):
    """The "Published in" text on a paper's own page ("manuscript", "PhD dissertation, MIT", ...); "" if none."""
    m = re.search(r"Published in:\s*</td>\s*<td[^>]*>(.*?)</td>", page, re.S | re.I)
    return spaced_text(m.group(1))[:500] if m else ""

# ---- theses -------------------------------------------------------------------------------------------
# A LingBuzz paper is listed as a thesis (type "Thesis" on the page, under "Theses" in its type menu, not under
# "Preprints") when its "Published in" text names a thesis as a whole word (in several languages), or its title
# marks it as one in brackets or at the very end ("... [PhD thesis]", "... (Dissertation)", "... - Master Thesis").
# Not when the text or title says it is only part of one ("Chapter 4 of my PhD thesis", "based on my dissertation").
# Checked by hand against all 10,287 papers on 29 Oct 2026: 360 tagged, no false hits found. Not caught: theses
# whose "Published in" is empty or only a university name and whose title doesn't say so.
THESIS_WORD = re.compile(r"\b(dissertations?|diss\.|thesis|theses|doctoral|doctorate|habilitation|proefschrift|thèse|tesi|tese|"
                         r"tesis|dissertação|tesina|doktorarbeit|magisterarbeit|masterarbeit|diplomarbeit|bachelorarbeit|mémoire)\b", re.I)
THESIS_PART = re.compile(r"chapter|\bch\.|based on|\bpart( \w+)? of|section of|excerpt|adapted from|revised (version )?of|"
                         r"derived from|follow[- ]?up|appendix|proposal|prospectus|qualifying|generals? paper|handout|\bslides\b|\btalk\b", re.I)
_DEG = r"(?:ph\.?\s?d\.?|doctoral|master'?s?|m\.?a\.?|mphil|mres|b\.?a\.?|honou?rs|undergraduate|senior)"
_TW = r"(?:thesis|dissertation|diss\.)"
THESIS_TITLE = re.compile(rf"[\[(][^\])]*\b{_TW}[^\])]*[\])]|(?:[.:\-–—]\s*|\s){_DEG}\s*{_TW}\s*\.?\s*$|[.:\-–—]\s*{_TW}\s*\.?\s*$|^{_TW}\s*:", re.I)

def is_thesis(published, title):
    if published and THESIS_WORD.search(published):
        return not THESIS_PART.search(published)
    return bool(title and THESIS_TITLE.search(title) and not THESIS_PART.search(title))

def all_published():
    """Both files' "Published in" texts; the scheduled job's copy wins."""
    return {**load_page_file(ABSTRACTS_OLD)[1], **load_page_file(ABSTRACTS)[1]}

def load_page_file(path):
    """(abstracts, published) from lingbuzz-abstracts.json or lingbuzz-abstracts-old.json, each by paper id."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("abstracts", {}), d.get("published", {})
    except (OSError, ValueError):
        return {}, {}

def load_abstracts(path):
    return load_page_file(path)[0]

def save_abstracts(path, abstracts, published, now):
    abstracts, published = dict(sorted(abstracts.items())), dict(sorted(published.items()))
    if os.path.exists(path) and load_page_file(path) == (abstracts, published):
        return   # unchanged: don't rewrite (or commit) the file
    write_json(path, {"generated": now.isoformat(timespec="seconds"), "abstracts": abstracts, "published": published})

def all_abstracts():
    """Both abstract files together; the scheduled job's copy wins unless it is empty."""
    out = load_abstracts(ABSTRACTS_OLD)
    out.update({k: v for k, v in load_abstracts(ABSTRACTS).items() if v or k not in out})
    return out

def fetch_old_abstracts(db, limit, delay, now):
    """--abstracts: read the pages of stored papers that have no abstract on file yet, most recently listed
    first, into lingbuzz-abstracts-old.json. Touches no other file. Saves every 25 papers; resumable."""
    (have, have_pub), (old, old_pub) = load_page_file(ABSTRACTS), load_page_file(ABSTRACTS_OLD)
    done = lambda pid, a, p: pid in a and pid in p   # both the abstract and "Published in" on file
    order = sorted((r for r in db.values() if str(r["id"]).isdigit()),
                   key=lambda r: (r.get("updated") or r.get("created") or "", r["id"]), reverse=True)
    if limit:
        order = order[:limit]
    todo = [r["id"] for r in order if not done(r["id"], have, have_pub) and not done(r["id"], old, old_pub)]
    if not todo:
        print("All of these papers already have their abstracts on file. Nothing to do.")
        return
    mins = len(todo) * (delay + 0.5) / 60
    print(f"Reading {len(todo)} LingBuzz paper pages, one every {delay:g} s (about "
          f"{f'{mins / 60:.1f} hours' if mins >= 90 else 'a minute' if mins < 1.5 else f'{round(mins)} minutes'}).")
    print("Press Control-C to stop at any time; run the same command again to continue where it stopped.")
    found = none = failed = 0
    try:
        for k, pid in enumerate(todo, 1):
            time.sleep(delay)
            try:
                page = fetch(f"{SOURCE}/lingbuzz/{pid}")
            except Exception as e:
                print(f"  {pid}: could not read its page ({e}); will try again next time")
                failed += 1
                continue
            old[pid], old_pub[pid] = abstract_of(page), published_of(page)
            found, none = found + bool(old[pid]), none + (not old[pid])
            if k % 25 == 0:
                save_abstracts(ABSTRACTS_OLD, old, old_pub, now)
            if k % 100 == 0:
                print(f"  ... {k} of {len(todo)} done")
    except KeyboardInterrupt:
        print("\nStopped. What was read so far is saved; run the same command again to continue.")
    save_abstracts(ABSTRACTS_OLD, old, old_pub, now)
    print(f"LingBuzz abstracts (and \"Published in\"): {found} found, {none} pages without an abstract"
          + (f", {failed} pages not readable" if failed else "") + f" -> {os.path.basename(ABSTRACTS_OLD)}")

def paper_info(pid):
    """Return (created_month, updated_month, revised, keywords, n_versions); months are (year, month).

    The month printed under the authors is the date of the CURRENT version. The
    'previous versions' list (v1 [May 2026] ...) gives when the paper was first posted."""
    page = fetch(f"{SOURCE}/lingbuzz/{pid}")
    ABS_SEEN[pid], PUB_SEEN[pid] = abstract_of(page), published_of(page)
    txt = spaced_text(page)
    m = re.search(r"<br\s*/?>\s*(" + "|".join(MONTHS) + r")\s+(\d{4})\s*</", page) or MONTH_RE.search(txt)
    updated = (int(m.group(2)), MONTHS[m.group(1)]) if m else None
    versions = {int(v): (int(y), MONTHS[mo]) for v, mo, y in
                re.findall(r"\bv(\d+)\s*\[\s*(" + "|".join(MONTHS) + r")\s+(\d{4})\s*\]", txt)}
    created = versions[min(versions)] if versions else updated
    kw = re.search(r"keywords:(.*?)</tr>", page, re.S | re.I)
    keywords = []
    if kw:
        parts = [k.strip() for k in spaced_text(kw.group(1)).lstrip(": ").split(",")]
        keywords = list(dict.fromkeys(k for k in parts if k and len(k) < 60))[:15]
    return created, updated, bool(versions), keywords, len(versions) + 1

def load():
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            meta = json.load(f)
        return {r["id"]: r for r in meta.get("records", [])}, meta
    return {}, {}

def save(db, now, front=None, unlisted=None):
    records = sorted(db.values(), key=lambda r: r["updated"] or "", reverse=True)[:KEEP]
    # url and pdf follow from the id, so they are not stored (the page rebuilds them); keeps the file small
    records = [{k: v for k, v in r.items() if k not in ("url", "pdf")} for r in records]
    old = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
    if front is None:
        front = old.get("front", [])
    if unlisted is None:
        unlisted = old.get("unlisted", [])
    if old.get("records") == records and old.get("front") == front and old.get("unlisted", []) == unlisted:
        return len(records)   # don't rewrite (or commit) when nothing changed
    write_json(OUT, {"generated": now.isoformat(timespec="seconds"), "front": front, "unlisted": unlisted, "records": records})
    return len(records)

GAP_PAGES = 3   # listing pages (100 papers each) searched for papers that left the front page before LingFeed saw them

def fill_gaps(db, before_max, unlisted, delay):
    """Find papers posted while the scraper wasn't watching (stopped, or LingBuzz down) that have already
    scrolled off the ~30-paper front page.

    LingBuzz numbers papers in upload order, so any number between the highest one stored before this run
    and the highest one stored now that is still missing was posted in between. Those papers are looked
    up in the first GAP_PAGES pages of LingBuzz's listing beyond the front page (the listing is ordered by
    the author's written date, so a recent upload is normally near the top). Numbers not found there are
    usually withdrawn or unlisted papers; they are remembered in `unlisted` and not looked for again.
    Returns the ids added; date_gap_papers() dates them."""
    top = max((int(i) for i in db if i.isdigit()), default=0)
    want = {f"{i:06d}" for i in range(before_max + 1, top)} - set(db) - set(unlisted)
    if not want or not before_max:
        return []
    print(f"{len(want)} paper number(s) between the previous run and now are not stored; looking for them in LingBuzz's listing ...")
    added, failed = [], set()
    for p in range(GAP_PAGES):
        start = 31 + 100 * p
        try:
            rows = parse(fetch(LISTING.format(start=start)), require_status=False)
        except Exception as e:
            print(f"  listing page at {start} could not be read ({e}); will look again next run")
            return added
        for rec in rows:
            if rec["id"] not in want:
                continue
            time.sleep(delay)
            try:
                cm, um, revised, keywords, nver = paper_info(rec["id"])
            except Exception as e:
                print(f"  {rec['id']}: could not read its page ({e}); will look again next run")
                failed.add(rec["id"])
                continue
            rec.update(status="changed" if revised else "new", revised=revised, nver=nver, keywords=keywords,
                       month=list(cm) if cm else None, umonth=list(um) if um else None,
                       created=None, updated=None, created_src="gap", updated_src="gap")
            db[rec["id"]] = rec
            added.append(rec["id"])
            want.discard(rec["id"])
        if not rows or not want - failed:
            break
    gone = sorted(want - failed)
    if gone:
        unlisted.extend(gone)
        print(f"  not in the listing (probably withdrawn or unlisted), not looked for again: {', '.join(gone)}")
    return added

def date_gap_papers(db, now):
    """Date papers found by fill_gaps(). Numbers follow upload order, so each one is placed between the
    nearest lower- and higher-numbered papers whose arrival LingFeed saw, in proportion to its number.
    Shown with "~" on the page (created_src "gap" is not "observed"), and never re-dated later."""
    anchors = sorted((int(i), to_ts(r["created"])) for i, r in db.items()
                     if i.isdigit() and r.get("created") and r.get("created_src") == "observed")
    top = max(int(i) for i in db if i.isdigit())
    for pid_s, r in db.items():
        if r.get("created_src") != "gap" or r.get("created"):
            continue
        pid = int(pid_s)
        lower = [a for a in anchors if a[0] < pid]
        upper = [a for a in anchors if a[0] > pid]
        hi_id, hi_t = upper[0] if upper else (top + 1, now.timestamp())
        lo_id, lo_t = lower[-1] if lower else (pid - 1, hi_t)
        hi_t = max(hi_t, lo_t)
        t = lo_t + (hi_t - lo_t) * (pid - lo_id) / (hi_id - lo_id)
        r["created"] = r["updated"] = to_iso(t)

RECENT_DAYS = 60   # papers listed (first posted, or given a new version) within this many days go in the "recent" file
PUBLIC_FIELDS = ("id", "title", "authors", "keywords", "created", "updated", "created_src", "updated_src", "revised", "status")

def write_public(db, now):
    """Write what the web page reads: a small lingbuzz-recent.json and a large lingbuzz-archive.json.
    The page always loads the recent file, and the archive only when someone searches or asks for older papers.
    lingbuzz.json itself stays the scraper's working copy and is not read by the page."""
    cutoff = now - timedelta(days=RECENT_DAYS)
    cut = cutoff.timestamp()
    recent, archive = [], []
    abstracts, published = all_abstracts(), all_published()
    for r in db.values():
        slim = {k: r[k] for k in PUBLIC_FIELDS if r.get(k) is not None}
        listed_recently = bool(r.get("created") and to_ts(r["created"]) >= cut) or \
                          bool(r.get("revised") and r.get("updated") and to_ts(r["updated"]) >= cut)
        if is_thesis(published.get(r["id"], ""), r.get("title", "")):
            slim["thesis"] = True
        a = abstracts.get(r["id"])
        if a:
            if not listed_recently and len(a) > ARCHIVE_ABSTRACT_CHARS:   # older papers: shortened, to keep the file small
                a, slim["abstract_cut"] = a[:ARCHIVE_ABSTRACT_CHARS].rsplit(" ", 1)[0] + " …", True
            slim["abstract"] = a
        (recent if listed_recently else archive).append(slim)
    recent.sort(key=lambda r: r.get("updated") or "", reverse=True)
    archive.sort(key=lambda r: r.get("created") or r.get("updated") or "", reverse=True)
    since = cutoff.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    for name, records, extra in (("lingbuzz-recent.json", recent, {"since": since}), ("lingbuzz-archive.json", archive, {})):
        path = os.path.join(os.path.dirname(OUT), name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            if old.get("records") == records and old.get("since") == extra.get("since"):
                continue   # unchanged: don't rewrite (or commit) the file
        write_json(path, {"generated": now.isoformat(timespec="seconds"), **extra, "records": records})

def report_gaps(db):
    ids = sorted(int(i) for i in db if i.isdigit())
    if not ids:
        print("no records stored")
        return
    have = set(ids)
    missing = [i for i in range(ids[0], ids[-1] + 1) if i not in have]
    print(f"stored {len(ids)} papers; ids {ids[0]:06d}..{ids[-1]:06d} ({ids[-1] - ids[0] + 1} ids); {len(missing)} ids not stored")
    ranges, start = [], None
    for i in missing:
        if start is None:
            start = prev = i
        elif i == prev + 1:
            prev = i
        else:
            ranges.append((start, prev)); start = prev = i
    if start is not None:
        ranges.append((start, prev))
    print("missing ranges (first 25): " + ", ".join(f"{a:06d}" if a == b else f"{a:06d}-{b:06d}" for a, b in ranges[:25]))
    print("(some missing ids will be papers LingBuzz has withdrawn or never listed)")

def mark_estimated(db, delay):
    """Give month-based dates to records whose stored time is only 'when we first ran', or
    that were dated by an older version of this script (which treated the current-version
    month as the creation month)."""
    todo = [r for r in db.values()
            if "updated_src" not in r
            or (r.get("created_src") == "estimated" and not r.get("est"))
            or (r.get("est") and "umonth" not in r)
            or (not r.get("month") and not r.get("est"))
            or ("revised" not in r and not r.get("est"))]
    for r in db.values():  # backfilled records were marked changed exactly when they had earlier versions
        if r.get("est") and "revised" not in r:
            r["revised"] = r.get("status") == "changed"
    if todo:
        print(f"Looking up version history for {len(todo)} records (~{delay}s each) ...")
    for r in todo:
        r.setdefault("updated_src", "estimated")
        if r.get("created_src") != "observed":
            r["created_src"] = "estimated"
        time.sleep(delay)
        try:
            cm, um, revised, keywords, nver = paper_info(r["id"])
        except Exception as e:
            print(f"  {r['id']}: {e}")
            continue
        r["keywords"], r["revised"], r["nver"] = keywords, revised, nver
        if cm and um:
            r.update(est=True, month=list(cm), umonth=list(um))
            if revised and r.get("status") != "changed":
                r["status"] = "changed"

def monitoring_start(db, now):
    """When LingFeed began watching LingBuzz: the earliest moment it saw anything arrive. A paper we did not
    see arriving was uploaded before that, so no estimated date may be later than it."""
    seen = [to_ts(r[k]) for r in db.values() for k in ("created", "updated")
            if r.get(k + "_src") == "observed" and r.get(k)]
    return datetime.fromtimestamp(min(seen), timezone.utc) if seen else now

def spread(db, cap, field, mkey):
    """Spread estimated papers over their month in id order, but never past `cap` (when monitoring began)."""
    groups = {}
    for r in db.values():
        if (r.get("est") and r.get(mkey) and r.get(field + "_src") != "observed"
                and not (field == "updated" and r.get("updated_by_rank"))):
            groups.setdefault(tuple(r[mkey]), []).append(r)
    for (y, mo), recs in groups.items():
        recs.sort(key=lambda r: int(r["id"]))
        start = datetime(y, mo, 1, tzinfo=timezone.utc)
        end = min(start + timedelta(days=calendar.monthrange(y, mo)[1]), cap)   # the current month ends at `cap`
        for i, r in enumerate(recs):
            ts = start + (end - start) * ((i + 0.5) / len(recs)) if end > start else cap
            r[field] = min(ts, cap).isoformat(timespec="seconds")

PCT = 0.75    # authors write before they upload, so take a high percentile of neighbouring written dates
WINDOW = 20   # ids on each side of a paper used for that percentile

def upload_months(db):
    """Estimate the month each paper was UPLOADED, not the month its author says it was written.

    LingBuzz asks authors for the date the paper was written, so that date can long precede the
    upload (old manuscripts posted later). Ids are sequential in upload order, so a rolling high
    percentile of the written dates of neighbouring ids tracks the upload period, and a running
    maximum keeps it from going backwards. Written months stay in month/umonth; the result is
    stored as up_month."""
    for r in db.values():  # undo adjustments made by earlier versions of this script
        if r.get("month_raw"):
            r["month"] = r.pop("month_raw")
        if r.get("umonth_raw"):
            r["umonth"] = r.pop("umonth_raw")
        r.pop("up_month", None)
    recs = sorted((r for r in db.values() if r.get("est") and r.get("month") and str(r["id"]).isdigit()),
                  key=lambda r: int(r["id"]))
    idx = [r["month"][0] * 12 + r["month"][1] - 1 for r in recs]
    floor = float("-inf")
    for k, r in enumerate(recs):
        w = sorted(idx[max(0, k - WINDOW): k + WINDOW + 1])
        floor = max(floor, w[int(PCT * (len(w) - 1))])
        r["up_month"] = [floor // 12, floor % 12 + 1]

def estimate_dates(db, now):
    """Dates by upload order: ids spread across their estimated upload month, in id order."""
    upload_months(db)
    cap = monitoring_start(db, now)
    spread(db, cap, "created", "up_month")
    spread(db, cap, "updated", "umonth")   # fallback for revised papers not dated by listing order
    for r in db.values():
        if not r.get("est"):
            continue
        if r.get("status") == "new" and r.get("updated_src") != "observed" and not r.get("updated_by_rank"):
            r["updated"] = r.get("created") or r.get("updated")   # never revised: last change = upload
        elif r.get("created") and r.get("updated") and r["updated"] < r["created"]:
            r["updated"] = r["created"]

def to_ts(s):
    return datetime.fromisoformat(s).timestamp()

def to_iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")

def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r, i = [0.0] * len(v), 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    rx, ry, n = ranks(xs), ranks(ys), len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else 0.0

def report_rank_correlations(db, ranks):
    """Diagnostic: which ordering does LingBuzz's listing follow? 1.00 = exactly newest-first by that key."""
    rows = [(rk, db[i]) for i, rk in ranks.items() if i in db and str(i).isdigit() and db[i].get("month")]
    month = lambda m: m[0] * 12 + m[1]
    keys = (("paper id (upload order)", lambda r: int(r["id"])),
            ("written date of first version", lambda r: month(r["month"])),
            ("written date of latest version", lambda r: month(r.get("umonth") or r["month"])))
    print(f"how the listing is ordered, over {len(rows)} papers (1.00 = exactly newest-first by that key):")
    for label, key in keys:
        parts = []
        for name, sel in (("all", lambda r: True), ("revised", lambda r: r.get("revised")), ("never revised", lambda r: not r.get("revised"))):
            sub = [(rk, r) for rk, r in rows if sel(r)]
            parts.append(f"{name} {spearman([-rk for rk, _ in sub], [key(r) for _, r in sub]):.2f}" if len(sub) > 2 else f"{name} n/a")
        print(f"  {label:34s} " + "   ".join(parts))

def longest_chain(vals):
    """Indices of a longest strictly decreasing subsequence of vals."""
    neg, tails, tails_idx, prev = [-v for v in vals], [], [], [-1] * len(vals)
    for i, x in enumerate(neg):
        k = bisect.bisect_left(tails, x)
        if k == len(tails):
            tails.append(x); tails_idx.append(i)
        else:
            tails[k] = x; tails_idx[k] = i
        prev[i] = tails_idx[k - 1] if k > 0 else -1
    out, i = [], (tails_idx[-1] if tails_idx else -1)
    while i != -1:
        out.append(i); i = prev[i]
    return out[::-1]

def date_revisions_by_rank(db, ranks, now):
    """Date revised papers by their position in LingBuzz's listing, which is ordered by last change.

    Papers that look never-revised AND sit in upload order in the listing are anchors: for them
    last change = upload, which we know from id order. Every other paper (revised, or edited without
    a new version, so it jumped up the listing) is placed between its two nearest anchors. Needs a
    full walk of the listing."""
    items = sorted(((rk, db[i]) for i, rk in ranks.items() if i in db), key=lambda t: t[0])
    cand = [i for i, (_, r) in enumerate(items)
            if not r.get("revised") and r.get("created") and str(r["id"]).isdigit()
            and (r.get("est") or r.get("created_src") == "observed")]
    chain = [cand[k] for k in longest_chain([int(items[i][1]["id"]) for i in cand])]
    frac = len(chain) / len(cand) if cand else 0.0
    print(f"listing-order check: {frac:.1%} of apparently unrevised papers lie on one consistent "
          f"upload-order sequence ({len(cand) - len(chain)} do not)")
    if frac < 0.8:
        print("  too low to trust the listing order; revised papers keep their written-month dates")
        return 0
    on_chain = set(chain)
    times, floor = [None] * len(items), float("inf")
    for i in chain:                                   # later in the listing can't be later in time
        floor = min(floor, to_ts(items[i][1]["created"]))
        times[i] = floor
    n, i, dated, edited = len(items), 0, 0, 0
    while i < n:
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < n and times[j] is None:
            j += 1
        hi = times[i - 1] if i > 0 else now.timestamp()
        lo = times[j] if j < n else hi
        k = j - i
        for m in range(k):
            r = items[i + m][1]
            if r.get("est") and r.get("updated_src") != "observed":
                t = to_iso(hi - (hi - lo) * (m + 1) / (k + 1))
                r["updated"] = max(t, r["created"]) if r.get("created") else t
                r["updated_src"], r["updated_by_rank"] = "estimated", True
                if not r.get("revised"):              # changed after upload, but no new version
                    r["edited"] = True
                    edited += 1
                dated += 1
        i = j
    if edited:
        print(f"  {edited} papers with no version history were nevertheless changed after upload (edits; not shown as new versions)")
    return dated

def backfill(pages, delay, db, now, ranks):
    added = 0
    for p in range(pages):
        start = 31 + 100 * p
        print(f"Listing page starting at {start} ...")
        rows = parse(fetch(LISTING.format(start=start)), require_status=False)
        if not rows:
            print("  no rows parsed; stopping (listing HTML may differ from expectations)")
            break
        for j, rec in enumerate(rows):
            ranks[rec["id"]] = start + j            # position in the listing (1 = most recently changed)
            if rec["id"] in db:
                continue
            time.sleep(delay)
            try:
                cm, um, revised, keywords, nver = paper_info(rec["id"])
            except Exception as e:
                print(f"  skip {rec['id']}: {e}")
                continue
            if not (cm and um):
                print(f"  skip {rec['id']}: no month found")
                continue
            rec.update(status="changed" if revised else "new", revised=revised, nver=nver, est=True, month=list(cm), umonth=list(um),
                       keywords=keywords, created=None, updated=None,
                       created_src="estimated", updated_src="estimated")
            db[rec["id"]] = rec
            added += 1
            if added % 25 == 0:  # checkpoint, so an interrupted run loses almost nothing
                estimate_dates(db, now)
                save(db, now)
                print(f"    ... {added} backfilled (latest id {rec['id']})")
        estimate_dates(db, now)
        save(db, now)  # checkpoint after each listing page
        print(f"  {added} backfilled so far")

def main(front_page=None):
    """front_page: LingBuzz's front page if the caller has just read it (refresh_lingbuzz.py), to avoid reading it twice."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0, help="listing pages (100 papers each) to backfill")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between paper-page requests")
    ap.add_argument("--versions", action="store_true", help="store the version count of every revised paper (about 2,000 requests), so later changes can be told from edits exactly")
    ap.add_argument("--check-listing", action="store_true", help="diagnostic: how is LingBuzz's listing ordered, and can it date revisions? (it is ordered by the author-written date, so it can't)")
    ap.add_argument("--gaps", action="store_true", help="report which paper ids are not stored, then exit")
    ap.add_argument("--abstracts", type=int, nargs="?", const=0, default=None, metavar="N",
                    help="only fetch abstracts of stored papers that have none on file (the N most recently listed; all if N is left out) into lingbuzz-abstracts-old.json, then exit")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    db, meta = load()
    if args.gaps:
        report_gaps(db)
        return
    if args.abstracts is not None:
        fetch_old_abstracts(db, args.abstracts, args.delay, now)
        return
    seeding = not db  # first ever run: everything on the front page predates us
    before = set(db)            # for the summary: papers stored before this run
    before_max = max((int(i) for i in db if i.isdigit()), default=0)   # for the gap check
    unlisted = list(meta.get("unlisted", []))
    new_versions, edits, recovered = [], [], []

    scraped = parse(front_page if front_page is not None else fetch(SOURCE))
    if not scraped:
        sys.exit("No papers parsed: LingBuzz's HTML may have changed. Inspect the page and adjust parse().")
    prev_front = meta.get("front", [])
    front = [rec["id"] for rec in scraped]

    def page_nver(pid):
        try:
            return paper_info(pid)[4]
        except Exception as e:
            print(f"  {pid}: could not read version history ({e})")
            return None

    for rank, rec in enumerate(scraped):
        ts = (now - timedelta(seconds=rank)).isoformat(timespec="seconds")
        old = db.get(rec["id"])
        if old is None:
            rec["created"] = ts if rec["status"] == "new" else None
            rec["updated"] = ts
            if not seeding:  # genuinely seen arriving: date is exact to within the scrape interval
                rec["created_src"] = "observed" if rec["status"] == "new" else None
                rec["updated_src"] = "observed"
                if rec["status"] == "changed":
                    time.sleep(args.delay)
                    nv = page_nver(rec["id"])
                    rec["nver"], rec["revised"] = nv or 1, bool(nv and nv > 1)
                else:
                    rec["nver"], rec["revised"] = 1, False   # its page (and abstract) is read by mark_estimated()
            db[rec["id"]] = rec
        else:
            old.update(title=rec["title"], authors=rec["authors"])
            # a paper that jumped to a higher place on the front page was changed since the last run
            moved_up = rec["id"] not in prev_front or rank < prev_front.index(rec["id"])
            if rec["status"] == "changed" and moved_up:
                time.sleep(args.delay)
                nv = page_nver(rec["id"])
                if nv is not None:
                    if nv > (old.get("nver") or (1 if not old.get("revised") else nv - 1)):   # a new version was posted
                        old.update(revised=True, nver=nv, status="changed", updated=ts, updated_src="observed")
                        old.pop("updated_by_rank", None)
                        old.pop("edited", None)
                        new_versions.append(rec["id"])
                    else:                                                                      # metadata edit or file swap
                        old.update(nver=nv, edited=True)
                        edits.append(rec["id"])

    if not seeding:
        recovered = fill_gaps(db, before_max, unlisted, args.delay)
        if recovered:
            date_gap_papers(db, now)
    mark_estimated(db, args.delay)
    if args.versions:
        todo = [r for r in db.values() if r.get("revised") and not r.get("nver")]
        print(f"Reading the version count of {len(todo)} revised papers (~{args.delay}s each) ...")
        for k, r in enumerate(todo, 1):
            time.sleep(args.delay)
            nv = page_nver(r["id"])
            if nv:
                r["nver"] = nv
            if k % 100 == 0:
                save(db, now)
                print(f"    ... {k} done")
    ranks = {rec["id"]: i + 1 for i, rec in enumerate(scraped)}
    if args.backfill:
        backfill(args.backfill, args.delay, db, now, ranks)
    estimate_dates(db, now)
    if args.backfill and args.check_listing:
        if len(ranks) >= 0.9 * sum(1 for r in db.values() if r.get("est")):
            report_rank_correlations(db, ranks)
            print(f"{date_revisions_by_rank(db, ranks, now)} revised papers dated from their position in the listing")
        else:
            print("listing not walked far enough for the check; use a larger --backfill")
    n = save(db, now, front, unlisted)
    if ABS_SEEN:
        a, p = load_page_file(ABSTRACTS)
        save_abstracts(ABSTRACTS, {**a, **ABS_SEEN}, {**p, **PUB_SEEN}, now)
    write_public(db, now)
    moved = sum(1 for r in db.values() if r.get("up_month") and r.get("month")
                and abs(r["up_month"][0] * 12 + r["up_month"][1] - r["month"][0] * 12 - r["month"][1]) >= 2)
    if moved:
        print(f"{moved} papers were uploaded 2+ months after the date their author gave them; dated by upload order")
    print(f"{len(scraped)} on front page, {n} stored -> {OUT}")
    added = len(set(db) - before)
    notes = ([f"{len(recovered)} of them found after leaving the front page, dated by estimate"] if recovered else []) + \
            ([f"{len(edits)} edited without a new version, not counted as updated"] if edits else [])
    print(f"LingBuzz: {added} added, {len(new_versions)} updated (new version)" + (f" ({'; '.join(notes)})" if notes else ""))

if __name__ == "__main__":
    main()
