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

-- Transcripts of sources, their segments and who speaks in each (the
-- Speakers page; serve_media.py's /transcripts/start, /speakers/*,
-- /segments/speaker, which create these at startup for older projects).
-- Segment times are source seconds. Speakers belong to the project.
CREATE TABLE IF NOT EXISTS transcripts (
  source TEXT PRIMARY KEY,  -- the source movie's file name
  model TEXT,
  prompt TEXT,  -- the names given to whisper
  status TEXT NOT NULL,  -- 'running', 'done' or 'failed: ...'
  started_at TEXT,
  finished_at TEXT,
  duration_s REAL
);
CREATE TABLE IF NOT EXISTS segments (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  t0 REAL NOT NULL,
  t1 REAL NOT NULL,
  text TEXT NOT NULL,
  speaker_id INTEGER  -- speakers.id, or NULL while unlabeled
);
CREATE TABLE IF NOT EXISTS speakers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  color TEXT NOT NULL,
  position INTEGER
);

-- Editing by text (the Podcast page's Edit text tab; serve_media.py creates
-- these at startup too). A transcript's words, from whisper's tokens, with
-- their source times; editing the text cuts, corrects or restores words and
-- never moves a time. Each save, undo-able, is a transcript_edits row.
CREATE TABLE IF NOT EXISTS words (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  i INTEGER NOT NULL,  -- position in the source
  t0 REAL NOT NULL,
  t1 REAL NOT NULL,
  text TEXT NOT NULL,  -- as whisper wrote it
  shown TEXT,  -- a correction, or NULL
  kept INTEGER NOT NULL DEFAULT 1,
  cut_reason TEXT,  -- 'edit' (deleted in the text) or 'tic'
  speaker_id INTEGER
);
CREATE TABLE IF NOT EXISTS transcript_edits (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  created_at TEXT NOT NULL,
  changes TEXT NOT NULL  -- JSON {cut, restored, corrected, restoredWas, reason}
);

-- Where a cut between word i-1 and word i falls, by the sound (align.py), so
-- each boundary is aligned once. method: ctc, ctc-gap or unaligned.
CREATE TABLE IF NOT EXISTS word_edges (
  source TEXT NOT NULL,
  i INTEGER NOT NULL,
  t REAL NOT NULL,
  method TEXT NOT NULL,
  PRIMARY KEY (source, i)
);
