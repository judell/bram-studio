#!/usr/bin/env python3
"""Where Studio keeps things: the one place that knows.

    import studio_paths            # PROJECT, DB, MEDIA, WORK; REPO, SOURCES
    eval "$(python3 studio_paths.py)"   # the same names, for record.sh

A project's own things live in its folder: studio.db (takes, callouts and
shapes, narrations), media/ (takes, clean copies, undo history, exports) and
work/ (sessions and render ingredients). STUDIO_PROJECT names that folder;
without it the project is the repo itself, which is where everything was
before projects.

What projects share stays in the repo: sources/, the Audio bench
(voicetest.py: media/tests/ and the voice_tests table) and the scripts.

serve_media.py puts STUDIO_PROJECT in its environment at startup, so the
scripts it launches (record.sh, overlay.py, ...) agree with it.
"""
import os
import shlex

REPO = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.abspath(os.environ.get("STUDIO_PROJECT") or REPO)
DB = os.path.join(PROJECT, "studio.db")
MEDIA = os.path.join(PROJECT, "media")
WORK = os.path.join(PROJECT, "work")
SOURCES = os.path.join(REPO, "sources")  # source movies, usually symlinks

if __name__ == "__main__":
    for name in ("PROJECT", "DB", "MEDIA", "WORK", "SOURCES"):
        print(f"{name}={shlex.quote(globals()[name])}")
