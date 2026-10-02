-- A project's database: projects/<slug>/studio.db (studio_paths.py). The Audio
-- bench's voice_tests table is shared by all projects: it lives in the repo's
-- studio.db and voicetest.py creates it.
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

-- Text and shapes burned into a take (overlay.py): captions from its
-- narration, callouts, and shapes drawn like recording ink, in 0-1 picture
-- coordinates and take seconds.
CREATE TABLE IF NOT EXISTS overlays (
  id INTEGER PRIMARY KEY,
  take_id INTEGER NOT NULL,
  kind TEXT NOT NULL,  -- 'caption', 'callout' or 'shape'
  text TEXT NOT NULL,  -- empty for shapes
  x1 REAL, y1 REAL, x2 REAL, y2 REAL,  -- an arrow: tail x1,y1, tip x2,y2; a pointer: tip x1,y1
  t_in REAL NOT NULL,
  t_out REAL NOT NULL,
  tail TEXT,  -- a callout's speech-bubble tail: n, ne, e, se, s, sw, w, nw, or none
  shape TEXT  -- a shape's kind: rect, ellipse, arrow, pointer
);

-- Narrations recorded over part of a take (serve_media.py's /narrate/*): the
-- stretch each covers, in take seconds, and the audio it replaced, so it can
-- be taken back out (/narrate/remove). serve_media.py creates this at startup.
CREATE TABLE IF NOT EXISTS narrations (
  id INTEGER PRIMARY KEY,
  take_id INTEGER NOT NULL,
  t_in REAL NOT NULL,
  t_out REAL NOT NULL,
  audio TEXT NOT NULL,  -- a WAV in media/.narrated/: what was there before
  created_at TEXT NOT NULL
);
