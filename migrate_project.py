#!/usr/bin/env python3
"""Move a pre-projects Studio into its first project folder. Run once.

    python3 migrate_project.py <slug> <name> [--dry-run]
    python3 migrate_project.py bram-demo "Bram demo"

Before projects, the repo held one of everything: studio.db, media/, work/.
This makes projects/<slug>/ (see studio_paths.py) and moves them into it:

- media/ and work/ move by rename (instant, nothing is copied), except
  media/tests/, the Audio bench's recordings, which stay shared.
- The takes, overlays and narrations tables go to the project's studio.db.
  The repo's studio.db keeps voice_tests only.
- projects/.open names the project, so Studio opens it.

Take URLs don't change: serve_media.py serves the open project's media/ at
the same addresses. Stop serve_media.py first; it refuses while a take is
being recorded.
"""
import json
import os
import shutil
import sqlite3
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
PROJECT_TABLES = ("takes", "overlays", "narrations")
SHARED_TABLES = ("voice_tests",)


def tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def counts(db, names):
    have = tables(db)
    return {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in names if t in have}


def main():
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv
    if len(args) != 2:
        sys.exit(__doc__)
    slug, name = args
    if not slug or os.path.basename(slug) != slug or slug.startswith("."):
        sys.exit(f"{slug!r} isn't a plain folder name")
    old_db, old_media, old_work = (os.path.join(REPO, p) for p in ("studio.db", "media", "work"))
    projects = os.path.join(REPO, "projects")
    project = os.path.join(projects, slug)
    if os.path.exists(project) or os.path.exists(os.path.join(projects, ".open")):
        sys.exit("projects/ already has a project or an open one: nothing to migrate")
    if not os.path.isfile(old_db) or not os.path.isdir(old_media):
        sys.exit("no studio.db and media/ here to migrate")
    if os.path.exists(os.path.join(old_media, ".record-state")):
        sys.exit("a take is being recorded: finish it first")
    was = counts(sqlite3.connect(old_db), PROJECT_TABLES)
    print(f"{slug} ({name}): {was}; media/ and work/ move to projects/{slug}/, media/tests/ stays")
    if dry:
        return
    os.makedirs(project)
    with open(os.path.join(project, "project.json"), "w") as f:
        json.dump({"name": name}, f, indent=1)
    # The project's database: a consistent copy, minus what is shared.
    new_db = os.path.join(project, "studio.db")
    src, dst = sqlite3.connect(old_db), sqlite3.connect(new_db)
    src.backup(dst)
    for t in SHARED_TABLES:
        dst.execute(f"DROP TABLE IF EXISTS {t}")
    dst.commit()
    dst.execute("VACUUM")
    if counts(dst, PROJECT_TABLES) != was:
        sys.exit(f"the copy doesn't match ({counts(dst, PROJECT_TABLES)}): nothing was moved; remove projects/{slug}")
    dst.close()
    # The repo's database keeps what is shared.
    for t in PROJECT_TABLES:
        src.execute(f"DROP TABLE IF EXISTS {t}")
    src.commit()
    src.execute("VACUUM")
    src.close()
    os.rename(old_media, os.path.join(project, "media"))
    os.makedirs(old_media)
    tests = os.path.join(project, "media", "tests")
    if os.path.isdir(tests):
        shutil.move(tests, os.path.join(old_media, "tests"))
    if os.path.isdir(old_work):
        os.rename(old_work, os.path.join(project, "work"))
    with open(os.path.join(projects, ".open"), "w") as f:
        f.write(slug + "\n")
    print(f"moved; projects/.open -> {slug}. Start serve_media.py.")


if __name__ == "__main__":
    main()
