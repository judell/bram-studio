# Bram Studio

A narrated-screencast studio. Record your screen once, then narrate over it: play,
pause, scrub and jump through the recording while you talk, and the take follows your
playhead. Pause to think and the dead air is cut. Takes are named from your first
spoken words.

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
- **Recording:** Record opens the source off the air, so you can set up; Start recording
  goes on the air; Pause recording cuts everything until you resume; Restart discards and
  begins again.
- **Ink:** hold ⌘ and drag over the picture to draw: line, arrow, rectangle, oval, point
  (an arrow at the spot you click) or freehand. Ink is drawn into the take and fades.
- **Sound:** the take has your narration and the source's own audio, in sync. Each is
  leveled to -16 LUFS so they match; your voice is denoised; and your mic is silenced
  while the source plays, since you don't talk over it. Wear headphones, so the mic
  doesn't hear the source.
- **Callouts:** on a recorded take, ⌘-drag a box on the video and type. Drag the callout
  to move it, drag its eight handles to resize it (the text grows to fit), give it a
  speech-bubble tail at any compass point, and set when it appears and disappears.
  Callouts are drawn live over the video while you edit; **Apply** burns them into the
  take for export.
- **Takes and export:** drag takes into order, then **Export** joins them into one video.

## What you need

- [Bram](https://github.com/judell/bram), with this project as its target app, and
  `python3 serve_media.py` running (port 8765).
- `ffmpeg`, Python 3 with Pillow, and Xcode's command-line tools (`record.sh` compiles
  `record_native.swift` on first use).
- `whisper-cli` (whisper.cpp) and a model at
  `~/.local/share/whisper-models/ggml-small.en.bin` (or set `WHISPER_MODEL`), for naming
  takes.
- The recorder uses the MacBook Air microphone by default (`MIC` in `record.sh`).
