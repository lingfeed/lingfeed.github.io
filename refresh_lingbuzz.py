#!/usr/bin/env python3
"""Run the LingBuzz update for the scheduled job, without an email for every run while LingBuzz is down.

    python3 refresh_lingbuzz.py      # what .github/workflows/refresh.yml runs every 15 minutes

GitHub emails the owner whenever a scheduled run FAILS. LingBuzz has sporadic outages, and the job runs
96 times a day, so letting every run fail would send close to 100 emails per day of outage. Instead:

  * first run that can't read LingBuzz's front page   -> fails (one email), outage start recorded
  * later runs during the same outage                 -> finish quietly
  * first run after each further ALERT_AGAIN_HOURS (12, 24, 36 ... hours into the outage) -> fails again
    (one more email), for as long as the outage lasts
  * first run that reads LingBuzz again                -> normal update; outage record cleared
  (The page's out-of-date notice also warns visitors once the data is 2 days old.)

"Can't read" covers both LingBuzz not answering and LingBuzz answering with a page where no papers can be
found (an error page during an outage, or a change in its layout, which needs fixing in build_lingbuzz.py).
The outage record lives in lingbuzz-status.json, committed by the workflow even when a run fails.
For manual runs, use build_lingbuzz.py directly; this file only adds the outage handling.
"""
import json, os, sys
from datetime import datetime, timezone

import build_lingbuzz as lb

ALERT_AGAIN_HOURS = 12
STATUS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lingbuzz-status.json")

def load_status():
    try:
        with open(STATUS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}

def save_status(st):
    if os.path.exists(STATUS) and load_status() == st:
        return                                   # unchanged: don't rewrite (or commit) the file
    tmp = STATUS + ".tmp"   # save safely: write a temporary file, then swap it into place
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
        f.write("\n")
    os.replace(tmp, STATUS)

def check_front_page():
    """(problem, page): problem is None if LingBuzz's front page can be read, otherwise a description;
    page is the front page itself, passed on to the update so it isn't read twice."""
    try:
        page = lb.fetch(lb.SOURCE)
    except Exception as e:
        return f"LingBuzz did not respond ({e})", None
    if not lb.parse(page):
        return "LingBuzz answered, but no papers could be read from its front page (an error page, or its layout changed)", None
    return None, page

def main():
    now = datetime.now(timezone.utc)
    st = load_status()
    problem, page = check_front_page()

    if problem:
        if not st.get("down_since"):
            save_status({"down_since": now.isoformat(timespec="seconds"), "alerts": 1, "problem": problem})
            print(f"LINGBUZZ OUTAGE: {problem}. Reporting it now; next reminder in "
                  f"{ALERT_AGAIN_HOURS} hours if it is still down.")
            sys.exit(1)                          # a failed run = one email
        hours = (now - datetime.fromisoformat(st["down_since"])).total_seconds() / 3600
        due = int(hours // ALERT_AGAIN_HOURS) + 1        # emails due by now: 1 at the start, +1 per 12 hours
        if due > st.get("alerts", 1):
            save_status({**st, "alerts": due, "problem": problem})
            print(f"LINGBUZZ STILL DOWN after {hours:.0f} hours (since {st['down_since']}): {problem}. "
                  f"Next reminder in {ALERT_AGAIN_HOURS} hours if it is still down.")
            sys.exit(1)                          # one more email: every 12 hours while the outage lasts
        print(f"LingBuzz still unreadable ({hours:.1f} hours, since {st['down_since']}); already reported. {problem}")
        return                                   # quiet: the run succeeds without updating anything

    if st.get("down_since"):
        hours = (now - datetime.fromisoformat(st["down_since"])).total_seconds() / 3600
        print(f"LingBuzz is back after an outage of about {hours:.1f} hours.")
    save_status({})                              # no outage (also creates the file the workflow commits)
    sys.argv = [sys.argv[0]]                     # build_lingbuzz's normal run, with no options
    lb.main(front_page=page)                     # uses the page just read, rather than reading it again

if __name__ == "__main__":
    main()
