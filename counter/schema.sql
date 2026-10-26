-- LingFeed click counter: database layout (Cloudflare D1)

-- One row per paper that has been clicked. Details come from the first accepted report and are never overwritten.
CREATE TABLE IF NOT EXISTS papers (
  id TEXT PRIMARY KEY, src TEXT NOT NULL, title TEXT NOT NULL, authors TEXT NOT NULL, url TEXT NOT NULL, first_seen TEXT NOT NULL
);
-- Distinct-visitor clicks per paper per day. This is what the ranking adds up.
CREATE TABLE IF NOT EXISTS daily (day TEXT NOT NULL, id TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (day, id));

-- Short-lived anti-abuse tables (pruned after two days). Visitors are stored only as one-way hashes
-- that use a random salt that changes every day, so nothing here identifies a person or can be linked across days.
CREATE TABLE IF NOT EXISTS seen   (day TEXT NOT NULL, id TEXT NOT NULL, visitor TEXT NOT NULL, PRIMARY KEY (day, id, visitor));
CREATE TABLE IF NOT EXISTS vcap   (day TEXT NOT NULL, visitor TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (day, visitor));
CREATE TABLE IF NOT EXISTS netcap (day TEXT NOT NULL, id TEXT NOT NULL, net TEXT NOT NULL, n INTEGER NOT NULL, PRIMARY KEY (day, id, net));
CREATE TABLE IF NOT EXISTS salts  (day TEXT PRIMARY KEY, salt TEXT NOT NULL);
