#!/usr/bin/env python3
"""Where Studio keeps things: the one place that knows.

    import studio_paths            # PROJECT, DB, MEDIA, WORK; REPO, SOURCES
    eval "$(python3 studio_paths.py)"   # the same names, for a shell script

A project's own things live in its folder, projects/<slug>/ (gitignored):
project.json (its name), studio.db (takes, callouts and shapes, narrations),
media/ (takes, clean copies, undo history, exports) and work/ (sessions and
render ingredients). projects/.open holds the open project's slug.
STUDIO_PROJECT, a folder, overrides it. With neither, the project is the repo
itself, which is where everything was before projects (migrate_project.py
moves it).

What projects share stays in the repo: sources/, the Audio bench
(voicetest.py: media/tests/ and the voice_tests table in the repo's
studio.db) and the scripts.

serve_media.py puts STUDIO_PROJECT in its environment at startup, so the
scripts it launches (overlay.py, register.py, ...) agree with it even if
projects/.open changes while it runs.
"""
import os
import shlex

REPO = os.path.dirname(os.path.abspath(__file__))
PROJECTS = os.path.join(REPO, "projects")
OPEN = os.path.join(PROJECTS, ".open")


def open_project():
    if os.environ.get("STUDIO_PROJECT"):
        return os.path.abspath(os.environ["STUDIO_PROJECT"])
    try:
        slug = open(OPEN).read().strip()
    except OSError:
        return REPO
    folder = os.path.join(PROJECTS, slug)
    # A plain folder name only; a stale pointer falls back to the repo.
    return folder if slug and os.path.basename(slug) == slug and os.path.isdir(folder) else REPO


PROJECT = open_project()
DB = os.path.join(PROJECT, "studio.db")
MEDIA = os.path.join(PROJECT, "media")
WORK = os.path.join(PROJECT, "work")
SOURCES = os.path.join(REPO, "sources")  # source movies, usually symlinks
TESTS = os.path.join(REPO, "media", "tests")  # the Audio bench's recordings

if __name__ == "__main__":
    for name in ("PROJECT", "DB", "MEDIA", "WORK", "SOURCES"):
        print(f"{name}={shlex.quote(globals()[name])}")
