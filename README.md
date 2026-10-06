# Bram Studio

A narrated-screencast studio. Cut scenes from screen recordings you've already made,
then edit each one: narrate over a stretch, insert a pause, cut what you don't need, and
add callouts and shapes. Export joins the scenes, in order, into one video.

- It supports the production style of
  [Heavy Metal Umlaut](https://jonudell.net/udell/2005-01-22-heavy-metal-umlaut-the-movie.html).
  Its first use is narrating Bram activity, and it isn't limited to that.
- Along the way we have needed, built, and are shipping new core XMLUI components:
  `MediaPlayer` ([xmlui-org/xmlui#3913](https://github.com/xmlui-org/xmlui/issues/3913)) and
  `PointerLayer` ([#3919](https://github.com/xmlui-org/xmlui/issues/3919)), with anchors
  that place content at picture coordinates
  ([#3922](https://github.com/xmlui-org/xmlui/issues/3922)).

Built with and inside [Bram](https://github.com/judell/bram). macOS only for now (the
voice recorder uses AVFoundation).

## What it does

- **Sources:** any `.mp4`, `.mov` or `.m4v` at the top of your Desktop shows up in the
  picker, newest first; `sources/` holds links to videos kept elsewhere. Tall (portrait)
  recordings fit the window.
- **Capturing scenes:** choose a source and its player opens with a two-handled slider
  over it. The handles and their nudges (1 s and one frame, each way) set the scene; Play
  scene, Intro 3s and Outro 3s let you hear it; **Make scene** adds it to the list as the
  next **Scene N**, with the source's own sound and a 3 s callout of its name at the top
  right. The next scene starts where the last one from that source ended. Rename a scene
  with the pencil beside its name.
- **Editing a scene:** play a scene to open the editor under its player, with tabs to
  annotate, cut, insert a pause, narrate over a stretch (your mic replaces the sound there,
  leveled to -16 LUFS and denoised) and see its history (Undo, Redo). A strip under the
  player shows where its sound is.
- **Callouts:** on a scene, ⌘-drag a box on the video and type. Drag the callout
  to move it, drag its eight handles to resize it (the text grows to fit), give it a
  speech-bubble tail at any compass point, and set when it appears and disappears.
  Callouts are drawn live over the video while you edit; **Apply** burns them into the
  scene for export. Boxes, ovals, arrows, lines and pointers work the same way.
- **Scenes and export:** drag scenes into order, then **Export** joins them into one video.
- **After editing elsewhere:** if you cut the export in another editor, run
  `python3 level_edit.py <edited.mp4>`. It removes low rumble and evens out the loudness of
  the joined sources, and writes `<name>-leveled.mp4` beside it with the video untouched.

## Where things are kept

Each screencast is a project, a folder under `projects/` (not committed): its
`studio.db` (takes, callouts and shapes, narrations), `media/` (takes, undo history,
exports) and `work/` (recording sessions and render ingredients). `projects/.open` names
the open one. Source movies (`sources/`) and the Audio page's test recordings are shared
by all projects. `studio_paths.py` is the one place that knows these paths.

The **Projects** page lists them (drag a project by its grip to reorder the list), with
**Open** to switch, **New project** to start an empty one, and **Delete** to move a project's
whole folder to the Trash (drag it back into `projects/` to restore it; the open project can't
be deleted). The open project's name shows in the header. Switching starts
`serve_media.py` over on that project, so it waits until no take, narration, render or
export is under way.

Each project is one line; expand it to see what its disk space is for: takes, re-mix ingredients,
exports, logs (kept, never offered for clean-up), undo history, deleted takes (ones Studio can still restore), and discarded (what
nothing in Studio uses or can restore). Anything in a
project's folder that Studio didn't put there is listed as **stray**, with a count on the
project's line: an editor's project folder saved beside an export, say.
In the exports folder an `.mp4` is expected whoever wrote it; anything else is stray.
**Delete** asks first, then moves the item to the macOS Trash. **Refresh** works the
sizes out again.

Three categories can be cleaned up from the same page, each with a button that says what
it will move and asks first, and lets you look through it: **discarded** (the largest:
deleted exports, takes deleted before Undo delete existed, abandoned recordings and
render leftovers, none of which anything uses), **deleted takes** (all, or only those
deleted more than 7 days ago; Studio can no longer restore them afterwards), and
**undo history** (the takes stay as they are; their History starts over). What is removed goes to the Trash as one folder named for the project and
the category, so the space comes back when you empty the Trash. Takes, re-mix
ingredients and exports have no button.

Projects aren't committed (they are recordings of your screen and voice, and they change
with every edit), so each one can name a **safekeeping** folder, a OneDrive or other synced
folder, say. **Copy to safekeeping** copies the takes, their re-mix ingredients, the exports,
the logs and a consistent copy of the database into `<folder>/<project>/`, laid out as the
project is. A later copy skips unchanged files and never deletes anything there. Undo
history, deleted and discarded files, stray files and the source movies aren't copied.

A Studio from before projects keeps working from the repo's own `studio.db`, `media/`
and `work/`. To move it into a project, stop `serve_media.py` and run
`python3 migrate_project.py <folder-name> "<Name>"`.

## What you need

- [Bram](https://github.com/judell/bram), with this project as its target app, and
  `python3 serve_media.py` running (port 8765).
- `ffmpeg`, Python 3 with Pillow, and Apple's Command Line Tools for `swiftc`
  (`xcode-select --install`; about 2.4 GB, not the full Xcode app; Homebrew installs
  them too). `serve_media.py` compiles `record_native.swift`, the narration recorder, on
  first use.
- `whisper-cli` (whisper.cpp) and a model at
  `~/.local/share/whisper-models/ggml-small.en.bin` (or set `WHISPER_MODEL`), for naming
  takes. Only `remix_take.py --regate`, for takes recorded live before scenes, uses the
  speech detector: Silero VAD at `~/.local/share/whisper-models/ggml-silero-v5.1.2.bin`
  (about 0.9 MB, from [ggml-org/whisper-vad](https://huggingface.co/ggml-org/whisper-vad);
  or set `VAD_MODEL`); without it, the voice isn't gated.
- Narration uses the MacBook Air microphone by default (set `MIC` in `serve_media.py`'s
  environment to use another).
- Dictating a note on the Exports page uses Bram's dictation script, so it needs a Bram
  that serves `/__shell/dictation.js` ([judell/bram#417](https://github.com/judell/bram/issues/417)).
  It listens on the browser's default microphone and transcribes with `whisper-server`
  (whisper.cpp): Bram's when it is running, otherwise one Studio starts with the model above.
