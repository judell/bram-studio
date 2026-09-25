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
  source TEXT  -- the source movie's file name (sources/)
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
