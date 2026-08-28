-- Stonetop campaign sync — D1 schema.
--
-- One row per *key inside* a store, not one row per store blob: that is what
-- makes two players ticking different boxes in the same poll window safe.
-- Applying a patch touches only the rows it names, so neither edit is lost.

CREATE TABLE IF NOT EXISTS entries (
  campaign TEXT NOT NULL,
  scope    TEXT NOT NULL,          -- 'shared' | 'gm'
  store    TEXT NOT NULL,          -- 'stonetop-wiki-checks', 'underfalls-hp', …
  k        TEXT NOT NULL,          -- key within the blob
  v        TEXT,                   -- JSON value; NULL = tombstone
  seq      INTEGER NOT NULL,       -- monotonic per campaign
  PRIMARY KEY (campaign, store, k)
);
CREATE INDEX IF NOT EXISTS entries_since ON entries (campaign, seq);

CREATE TABLE IF NOT EXISTS campaigns (
  campaign     TEXT PRIMARY KEY,
  player_token TEXT NOT NULL,
  gm_token     TEXT NOT NULL,
  seq          INTEGER NOT NULL DEFAULT 0,
  created      INTEGER NOT NULL
);

-- Campaign creation is the only unauthenticated endpoint, so it is the only
-- one that can be hammered. One row per (address, hour).
CREATE TABLE IF NOT EXISTS creates (
  ip     TEXT NOT NULL,
  window INTEGER NOT NULL,         -- unix hour
  n      INTEGER NOT NULL,
  PRIMARY KEY (ip, window)
);
