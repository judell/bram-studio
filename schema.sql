CREATE TABLE IF NOT EXISTS takes (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  duration_s REAL,
  src_start TEXT,
  src_end TEXT,
  file TEXT NOT NULL,
  url TEXT NOT NULL,
  notes TEXT,
  source TEXT,  -- the source movie's file name (sources/)
  warning TEXT,  -- e.g. "no player events; duration=…" (record.sh)
  position INTEGER,  -- display order in the takes list (drag to reorder)
  overlay_applied TEXT  -- its callouts as last burned in (overlay.py APPLIED_SQL)
);

-- One row per 10s voice test from the audio test bench (voicetest.py).
CREATE TABLE IF NOT EXISTS voice_tests (
  id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  mic TEXT NOT NULL,
  recorder TEXT NOT NULL,
  expected_s REAL,
  captured_s REAL,
  short_pct REAL,
  clicks INTEGER,
  peak_db REAL,
  rms_db REAL,
  raw_url TEXT NOT NULL,
  leveled_url TEXT NOT NULL
);

-- Text burned into a take (overlay.py): captions from its narration and
-- callouts, in 0-1 picture coordinates and take seconds.
CREATE TABLE IF NOT EXISTS overlays (
  id INTEGER PRIMARY KEY,
  take_id INTEGER NOT NULL,
  kind TEXT NOT NULL,  -- 'caption' or 'callout'
  text TEXT NOT NULL,
  x1 REAL, y1 REAL, x2 REAL, y2 REAL,
  t_in REAL NOT NULL,
  t_out REAL NOT NULL,
  tail TEXT  -- a callout's speech-bubble tail: n, ne, e, se, s, sw, w, nw, or none
);
