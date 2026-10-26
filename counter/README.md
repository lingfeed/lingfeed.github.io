# LingFeed click counter

This small service counts clicks on papers listed in LingFeed (on the site and in the RSS feed) and provides the
"Most clicked" ranking. It runs on Cloudflare Workers, whose free plan is plenty for this, and needs no server of your own.

It is already set up and running at `https://lingfeed-counter.lingfeed.workers.dev` (that address is set as
`COUNTER_URL` in `index.html` and `go.html`). The steps below are for changing it, or for setting it up again from scratch.

All commands are typed in Terminal, in the counter folder:
```
cd ~/Sites/lingfeed/counter
```
`npx wrangler` runs Cloudflare's tool (it needs Node.js, from https://nodejs.org). The first time, `npx` may ask
to install it: type `y`.

## After changing `worker.js` or `wrangler.toml`

```
npx wrangler deploy
```
If it says you aren't logged in, run `npx wrangler login` first and click Allow in the browser window that opens.
To check the service afterwards, open `https://lingfeed-counter.lingfeed.workers.dev/health` in a browser: it should say `ok`.

## Which sites may use it

`ALLOWED_ORIGINS` in `wrangler.toml` lists the sites allowed to report clicks and read the ranking, separated by commas,
each with no trailing slash. At launch, replace `https://YOUR-SITE.example` with LingFeed's real address and remove
`http://localhost:8000` (so testing on the Mac no longer counts), then deploy. The launch checklist in STATUS.md has the
full steps, including clearing the test clicks.

## Setting it up from scratch (only if starting over)

1. Make a free account at https://dash.cloudflare.com and run `npx wrangler login`.
2. Create the database: `npx wrangler d1 create lingfeed-counter`. It prints a `database_id`; put it in `wrangler.toml`.
3. Create the tables: `npx wrangler d1 execute lingfeed-counter --remote --file=schema.sql`
4. Choose a long random password for removing papers from the ranking: `npx wrangler secret put ADMIN_TOKEN`
5. Deploy: `npx wrangler deploy`. It prints the service's address; if it differs from the one above, put it in
   `COUNTER_URL` in both `index.html` and `go.html`.

## Removing a paper from the ranking

If someone is abusing the count for a paper:
```
curl -X DELETE -H "Authorization: Bearer YOUR-ADMIN-TOKEN" https://lingfeed-counter.lingfeed.workers.dev/admin/paper/z1234567
```
The id is `z` + the Zenodo number shared by all versions of the paper (its "concept" record number), `l` + the
LingBuzz number (six digits), `a` + the arXiv number, or `o` + the OSF code. The ranking is cached for up to
30 minutes, so a removed paper can take that long to disappear from the list.

## How counting works

- One click per visitor per paper per day counts; at most 40 papers per visitor per day, and at most 10 counted
  clicks per paper per day from one network. Known automated agents are ignored.
- A paper appears in the ranking once it has `MIN_COUNT` counted clicks; this is set to 1 (every counted click shows).
- The ranking covers the last `WINDOW_DAYS` (183, about six months) and is recalculated at most every 30 minutes.
- Clicks in the RSS feed reach the counter through `go.html` on the site, so they count only when a real browser
  opens the link (feed readers' and email scanners' link checks are not counted).

## What it stores

Per paper: title, authors and link (from the first accepted report), and a count per day.
For protection against inflation it also keeps, for two days only, one-way hashes of visitors that use a random
salt that is replaced every day. No IP addresses, browser details or personal information are stored, and a visitor
cannot be recognised from one day to the next.

## Cost

Cloudflare's free plan allows 100,000 requests a day for the Worker, and for the database 5 million rows read and
100,000 rows written a day, with 5 GB of storage (limits as published in October 2026; current figures at
https://developers.cloudflare.com/workers/platform/pricing/). Each counted click writes 4 to 5 rows, so the free
plan handles roughly 20,000 counted clicks a day. If you ever exceed it, the paid plan is $5 a month.

## Settings

In `worker.js`: `MIN_COUNT` (clicks needed to be listed), `WINDOW_DAYS` (ranking period),
`MAX_PAPERS_PER_VISITOR_DAY`, `MAX_PER_NETWORK_PAPER_DAY`, and `BOT_UA` (user agents to ignore).
Deploy after any change.
