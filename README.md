# Bram Studio

Bram Studio makes two kinds of project: 

- **Screencast:** cut scenes from screen recordings you've already made, then narrate over
  a stretch, insert a pause, cut what you don't need, and add callouts and shapes. Final
  video joins the scenes, in order, into one video. It supports the production style of
  [Heavy Metal Umlaut](https://jonudell.net/udell/2005-01-22-heavy-metal-umlaut-the-movie.html).
- **Podcast:** edit a recorded conversation by its text. Delete words to cut them, elide
  "um"s and stutters, and hear each paragraph as edited. Final video renders the edit;
  Final transcript says who's speaking, matched from a few samples of each voice. Both
  keep every version, and two transcripts can be compared.

Built with and inside [Bram](https://github.com/judell/bram);
macOS only for now (the voice recorder uses AVFoundation).

<details>
<summary><b>Getting started</b></summary>

No experience needed. You'll get an AI assistant, run a few lines in Terminal, and from
then on the assistant helps with the rest.

**Before you start**

1. **What you need:** a Mac; an AI assistant plan, either **Claude** (Pro or Max) or
   **ChatGPT** (for Codex); a screen recording or podcast recording, sitting on your Mac's
   Desktop as an `.mp4` file; and about half an hour the first time, mostly downloads.
2. **Open Terminal:** press ⌘ Space, type **Terminal**, press Return. A window with a
   text prompt opens. For each command below, paste it into that window and press Return.
3. **Let Terminal find what you install.** Paste this, then close the Terminal window and
   open a new one. Do this even if you're not sure you need it; running it again does no
   harm.

   ```sh
   echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
   ```

4. **Install your assistant**, the one that matches your plan. **Skip this if you already
   use Claude Code or Codex.**
   - **Claude (Claude Code):** paste this, then type `claude` and follow its sign-in.
     Type `/exit` when it's ready.

     ```sh
     curl -fsSL https://claude.ai/install.sh | bash
     ```

   - **ChatGPT (Codex):** Codex comes through Homebrew, a standard installer for tools
     like this, so install that first with the command published at
     [brew.sh](https://brew.sh):

     ```sh
     /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
     ```

     It asks for your **Mac login password**, and nothing appears as you type; that's
     normal. When it finishes it shows **Next steps** with a line or two to paste; paste
     them. Then paste `brew install --cask codex`, then type `codex` and choose **Sign in
     with ChatGPT**. Press Ctrl-C when you're signed in.

**Get Bram Studio running.** One line at a time:

```sh
curl -fsSL https://github.com/judell/bram/releases/latest/download/install.sh | bash
cd ~
git clone https://github.com/judell/bram-studio.git
cd bram-studio
bram
```

- The first line installs Bram. **Skip it if you already have Bram.** Running it again is
  harmless; it just updates to the latest version.
- `cd ~` goes to your home folder, so Bram Studio lands in a known place.
- `git clone` downloads Bram Studio into a folder named `bram-studio`. If a window offers
  to install "command line developer tools", click **Install**, wait for it to finish,
  then paste the `git clone` line again.
- `cd bram-studio` goes into that folder.
- `bram` starts Bram there. If its window asks **Which agent do you use?**, click
  **Claude** or **Codex**, whichever you use.

**Hand it to the assistant.** Bram Studio opens in your browser. Click **Get me going**:
Bram's window comes forward with a message ready, and you press Return. The assistant
takes it from there. It checks your Mac, installs what's missing with you (it asks first,
and tells you when and where to type your Mac password), and helps you start your first
podcast.

**Next time:** open Terminal and run `cd ~/bram-studio`, then `bram`.
**Stuck?** Ask the assistant in Bram's window, in plain words.

</details>

<details>
<summary><b>Screencast in detail</b></summary>

- **Sources:** any `.mp4`, `.mov` or `.m4v` at the top of your Desktop shows up in the
  picker, newest first; `sources/` holds links to videos kept elsewhere. Tall (portrait)
  recordings fit the window.
- **Capturing scenes:** choose a source and its player opens with a two-handled slider over
  it. The handles and their nudges (1 s and one frame, each way) set the scene; Play scene,
  Intro 3s and Outro 3s let you hear it; **Make scene** adds it to the list as the next
  **Scene N**, with the source's own sound. The next scene starts where the last one from
  that source ended. Rename a scene with the pencil beside its name; drag scenes into order.
- **Editing a scene:** play a scene to open the editor under its player, with tabs to
  annotate, cut, insert a pause, narrate over a stretch (your mic replaces the sound there,
  leveled to -16 LUFS and denoised) and see its history (Undo, Redo). A strip under the
  player shows where its sound is.
- **Callouts and shapes:** on a scene, ⌘-drag a box on the video and type. Drag a callout to
  move it, drag its eight handles to resize it (the text grows to fit), give it a
  speech-bubble tail at any compass point, and set when it appears and disappears. Boxes,
  ovals, arrows, lines and pointers work the same way. They're drawn live while you edit,
  and burned into each changed scene when the final video is made. Each change is a step in
  the scene's history, so Undo takes it back.
- **Exports:** **Make final video** (then **Remake**), at the top of the Exports tab, joins
  the scenes in list order into `media/exports/<time>-takes.mp4`. Every version is kept,
  numbered v1, v2… and listed newest first with a player, **Download video** and **Delete**.
  The tab says when the scenes have changed since the newest version (their order, their
  files, their callouts). Below, every video in `media/exports/` is listed with a note,
  including ones you edited elsewhere and saved there.
- **After editing elsewhere:** if you cut an export in another editor, run
  `python3 level_edit.py <edited.mp4>`. It removes low rumble and evens out the loudness of
  the joined sources, and writes `<name>-leveled.mp4` beside it with the video untouched.

</details>

<details>
<summary><b>Podcast in detail</b></summary>

- **Transcribe:** a new project's Workspace offers **Transcribe** (whisper.cpp's
  `whisper-cli`; the `ggml-large-v3-turbo` model when it's in
  `~/.local/share/whisper-models/`, else `small.en`). whisper is asked to keep "um", "uh"
  and repeats, so they can be cut, and is given the project's speakers' names, if any yet,
  so they're spelled right. **Raw transcript** shows whisper's segments with their times.
- **Other readings:** a second model (the full `ggml-large-v3`, when it's in the same
  folder, NVIDIA's Parakeet, fast and not whisper-derived, when its model is installed, or Apple's own
  on-device engine on macOS 26, with nothing to download by hand) hear the recording in the background (about 12 minutes for 48): they run on their own
  after Transcribe, fastest first (turn that off in Settings), and the transcript line in the Workspace
  shows which is hearing; **Hear with** buttons there run one again or all. They appear under
  **Other readings** in the Workspace list where they disagree with
  the text, for the selected card or all of them: **Play** the stretch, or **Use this** to
  take its reading word for word (**Undo** takes it back).
- **Editing (Workspace):** the transcript as plain text with no time codes, a paragraph per
  card. Delete what you want gone; the paragraph saves when you click outside it. Deleted
  words are cut, a word changed in place is a correction (its time stays), and cut words
  typed back are restored. Each card plays its paragraph as edited, and the video on the
  left follows it; play the video yourself and it takes over the sound. **Undo** and
  **Redo** work across all paragraphs. Each paragraph has **Elide N tics**, which cuts its
  fillers ("um", "uh", and any you add in Settings, such as "you know") and stutters ("and, and") in one step, and **Restore
  tics**. **Replace** changes a word or phrase everywhere it's kept (matched in any case),
  each word keeping its time, as one step **Undo** takes back. **Cuts** (folded) lists what
  was taken out. Paragraph breaks are set once from all
  the words and never move.
- **Where a cut falls:** whisper's word times can be off by up to a second, so each cut's
  edges are placed by the sound: `align.py` hears the few seconds around an edge with a CTC
  speech model and cuts in the quietest 10 ms between the last kept word and the first cut
  one. Without the model, edges fall back to whisper's times.
- **Samples:** add the speakers in Settings › Speakers, then turn on **Samples** and mark a
  few stretches of each voice (drag across words, or double-click one, and pick whose voice
  it is). The list under the switch shows every sample with **Play** and **Clear**. A check
  runs by itself whenever samples change: a stretch with someone else's voice in it leads
  with **⚠ Undecided**, and its flagged part can be played and cleared on its own. One bad
  sample pulls its speaker's voiceprint toward the other voice.
- **Final video:** **Make final video** renders every paragraph as its card plays it and
  joins them. Every version is kept (`media/exports/finals/`, the newest also as
  `media/exports/<project>-final.mp4`), with a player, **Download video** and **Delete**,
  and the page says when the edit has changed since the newest.
- **Final transcript:** **Make final transcript** matches every word you didn't mark to the
  voice it sounds like (`voiceprint.py`: NeMo TitaNet-small speaker embeddings under
  sherpa-onnx, about a minute for an hour), then lists the turns with their times in the
  final video. Matching hears only what's in the edit (cut speech is silenced), decides each
  change of speaker again at the nearest pause or sentence end, and folds short runs that
  don't clearly sound like their own speaker into the turn around them. Turns marked (?)
  were unsure. Each turn has **Edit**, which opens the Workspace on its paragraph with a
  link back. Every version is kept with a record of each word's speaker, and **Compare**
  shows what changed between two of them, for instance after adding samples.
  **Remake** is dimmed when nothing would change. **Download Markdown** saves a version.
- **Settings › Podcast editing:** the shortest pause between words (80 ms), the fade at
  each cut (10 ms), when a paragraph breaks (a 0.5 s pause, or past 120 words), the word
  lists Elide tics uses, and **Who's speaking**: how much speech each voice judgment hears,
  how often one is made, the shortest turn, how far a change of speaker may move, and how
  much sample each speaker needs. Each says what you'd notice; to see a change's effect,
  Remake the transcript and Compare.
- **Models**, in their own venv (Homebrew's Python refuses system-wide installs):

  ```sh
  python3 -m venv ~/.local/share/bram-studio/venv
  ~/.local/share/bram-studio/venv/bin/pip install sherpa-onnx numpy
  mkdir -p ~/.local/share/speech-models && cd ~/.local/share/speech-models
  curl -L https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-ctc-en-conformer-small.tar.bz2 | tar xj
  curl -L -o nemo_en_titanet_small.onnx https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/nemo_en_titanet_small.onnx
  ```

</details>

<details>
<summary><b>Projects and where things are kept</b></summary>

Each screencast or podcast is a project, a folder under `projects/` (not committed): its
`project.json` (its name, its genre, chosen once when it is made, and the source movies it
is made from), `studio.db`, `media/` (takes, undo history, exports, finals) and `work/`
(recording sessions, transcripts, render ingredients). `projects/.open` names the open one.
Source movies (`sources/`) and the Audio page's test recordings are shared by all
projects. `studio_paths.py` is the one place that knows these paths.

The header's picker opens a project, and **New project** starts one (a name, a genre and a
first source). Switching starts `serve_media.py` over on that project, so it waits until
nothing is under way. A fresh checkout has no projects: the first time `serve_media.py`
starts, it creates **My project**. With projects but none marked open, it opens the most
recently changed one.

**Manage** is the open project's own page:

- **Disk use:** what its space is for (takes, re-mix ingredients, exports, logs, undo
  history, deleted takes, discarded), and anything in its folder Studio didn't put there,
  listed as stray. Three categories can be cleaned up (discarded; deleted takes; undo
  history), each with a button that says what it will move and asks first; what goes, goes
  to the Trash as one folder.
- **Safekeeping:** a synced folder (OneDrive, say) the project is copied to with **Copy to
  safekeeping**: takes, their re-mix ingredients, exports, logs and a consistent copy of
  the database, laid out as the project is. A later copy skips unchanged files and never
  deletes anything there.
- **Rename** (the pencil) and **Delete this project**, which opens the project you changed
  most recently and moves this one's folder to the Trash (drag it back into `projects/` to
  restore it).
- Stray items in the folders all projects share.

Each take's sound is also kept losslessly, in `media/.audio/<take>.flac`. Every cut, pause,
narration and their removals build the new sound from it and encode it once, so a take
edited many times sounds as good as one edited once.

A Studio from before projects keeps working from the repo's own `studio.db`, `media/` and
`work/`. To move it into a project, stop `serve_media.py` and run
`python3 migrate_project.py <folder-name> "<Name>"`.

</details>

<details>
<summary><b>What you need</b></summary>

`./check.sh` reports what's here and what's missing, in plain words, and installs nothing.

- [Bram](https://github.com/judell/bram), with this project as its target app, and Studio's
  server running (port 8765, or the next free one if another checkout's Studio holds it;
  recorded in `studio-port.json`): `./start.sh` starts `serve_media.py` in the background,
  logging to `serve_media.log`, or says it's already running. If it's running code older
  than the files on disk (after a pull, say), it restarts it, unless a narration, render or
  export is under way. `./start.sh --restart` restarts it after a server change,
  and `./start.sh --stop` stops it, unless a narration, render or export is under way.
  Bram runs `./start.sh` at each launch (`.bram.json` `launch.run`, in Bram builds from
  9d6afebd on), once you click **Allow** on its banner on this computer (`./check.sh`
  says whether you have), and runs
  `./start.sh --stop` when you turn that off in Bram's Settings › Companion App
  (`launch.stop`).
- `ffmpeg`, Apple's Command Line Tools for `swiftc` and Python
  (`xcode-select --install`; about 2.4 GB, not the full Xcode app; Homebrew installs them
  too), and Studio's own Python with Pillow: `python3 -m venv ~/.local/share/bram-studio/venv`,
  then `~/.local/share/bram-studio/venv/bin/pip install Pillow`. `start.sh` runs the server
  with that Python when it exists, with Homebrew's folders first on `PATH`.
  `serve_media.py` compiles `record_native.swift`, the narration recorder, on first use.
- `whisper-cli` (whisper.cpp) and a model at `~/.local/share/whisper-models/ggml-small.en.bin`
  (or set `WHISPER_MODEL`), for naming takes and transcribing. Only `remix_take.py --regate`,
  for takes recorded live before scenes, uses the speech detector: Silero VAD at
  `~/.local/share/whisper-models/ggml-silero-v5.1.2.bin` (about 0.9 MB, from
  [ggml-org/whisper-vad](https://huggingface.co/ggml-org/whisper-vad); or set `VAD_MODEL`);
  without it, the voice isn't gated.
- For podcasts, the models above (Podcast in detail).
- Narration uses the MacBook Air microphone by default (set `MIC` in `serve_media.py`'s
  environment to use another).
- Dictating a note on the Exports page uses Bram's dictation script, so it needs a Bram that
  serves `/__shell/dictation.js` ([judell/bram#417](https://github.com/judell/bram/issues/417)).
  It listens on the browser's default microphone and transcribes with `whisper-server`
  (whisper.cpp): Bram's when it is running, otherwise one Studio starts with the model above.

</details>

<details>
<summary><b>Notes</b></summary>

Along the way we have needed, built, and are shipping new core XMLUI components:
`MediaPlayer` ([xmlui-org/xmlui#3913](https://github.com/xmlui-org/xmlui/issues/3913)) and
`PointerLayer` ([#3919](https://github.com/xmlui-org/xmlui/issues/3919)), with anchors that
place content at picture coordinates ([#3922](https://github.com/xmlui-org/xmlui/issues/3922)).

</details>
