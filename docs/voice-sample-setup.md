# Voice samples: making the workflow discoverable

A spec for [#6](https://github.com/judell/bram-studio/issues/6). Facts are as of
commit `b132764`. Nothing here is built yet; implementation is separate work,
taken up once this is agreed.

## The problem

Podcast speaker identification works incrementally, and that is its strength.
You start editing right away and mark samples whenever you like. A minimal set
gets you going, and you add more later where telling speakers apart is tricky.
Nothing has to be finished before anything else can start. In use, this has
proven better than a fixed sequence.

The trouble is that **nothing in Studio says it works this way.** A newcomer
can't tell from the screen that:

- a few seconds of each voice is enough to start;
- samples can be added in the middle of editing, without losing anything;
- adding samples is *how you fix* a wrong speaker in the final transcript.

Other transcription tools teach a different model: identify an unknown
speaker, assign a person, confirm, then match. Some people may expect that
model.

This spec takes two questions in order:

1. How do we make the existing incremental workflow discoverable?
2. Is that the wrong mental model for some people, and do they need another
   path?

## How it works today

- **Speakers** are added in **Settings › Speakers**.
- **Samples** is a switch in the Workspace (`components/Podcast.xmlui`,
  `tagMode`). While it's on, dragging across words (or double-clicking one)
  and picking a speaker marks a sample. The list under the switch shows each
  sample with **Play** and **Clear**.
- Each change starts a background check (`voiceprint.py --check`, through
  `serve_media.py`'s `check_samples`). The check flags samples that have
  someone else's voice in them: **⚠ Undecided**, and a switch summary like
  `5 samples · ⚠ 1 with someone else in them`.
- A line under the editor gives seconds per speaker (`taggedLine()`:
  `Samples: Jon 42 s · Steve 30 s`). With no speakers it says
  `No speakers yet: add them in Settings › Speakers.`
- **Settings › Podcast editing › Samples needed per speaker (s)**
  (`vpMinTaggedS`, 2–120). A speaker with less sample than this isn't
  matched, and their words go to others.
- **Matching happens in one place: Make / Remake final transcript**
  (`components/FinalTranscript.xmlui`, `POST /podcast/final-transcript`,
  about a minute an hour). Commit 8e567d0 removed the separate, hidden Match
  voices button.
  - Remake is dimmed when nothing would change.
  - Turns that matched no voice clearly are marked **(?)**.
  - Each turn's **Edit** pencil opens the Workspace on its paragraph, with a
    link back.
  - **Compare** shows what changed between two versions.

Two findings on the way:

- **The Samples switch is disabled until a paragraph is current**
  (`enabled="{currentPara !== null || tagMode}"`). A first-time user who
  hasn't clicked into a card sees a greyed-out switch and no reason why. That
  is a discoverability problem in its own right.
- A comment above the switch in `components/Podcast.xmlui` still describes
  "Match voices (voiceprint.py, hidden)". It should be updated along with the
  implementation.

## The three phases

Keep these distinct in every label and message:

| Phase | Where | What happens |
|---|---|---|
| **Collect samples** | Workspace, Samples on | Teaches Studio what each voice sounds like. Changes no speaker on any word. Cheap: only the background check runs. |
| **Match voices** | Final transcript, Make / Remake | Assigns every unmarked word to a voice. The one costly step, and the only point you commit to. |
| **Review and refine** | Final transcript, then back to the Workspace | Read the turns, look at (?), fix by adding a sample where a turn is wrong, Remake, Compare. |

The loop from *Review* back to *Collect* is the workflow's main idea, and it is
what's most hidden today.

## Part 1. Making the incremental workflow discoverable

The principle: **explain the model where the user already is, and show state
instead of prescribing steps.** Nothing here adds a required action.

**First round: three low-risk cues, nothing else.**

- "Sample as you go" in the Samples tooltip and empty state (1.1).
- Why the Samples switch is disabled, or remove that restriction (1.2).
- A hint by (?) turns: edit the paragraph, add a sample, Remake (1.1).

The state line (1.3) and the label (1.6) only reword what Studio already
shows, so they can come in the same round if they're cheap. The "Add a sample
here" control (1.4) and any readiness display (1.5) are **deferred** until
testing shows the cues aren't enough. Both risk making the flexible workflow
feel like a procedure. (This narrowing follows a design review by Codex on
the worklist item for #6.)

### 1.1 Say what a sample is, once, in place

Use short text at three points where the question arises:

- **Samples switch tooltip:** "Mark a few seconds of each speaker's voice.
  Samples are used when you make the final transcript. Start with a little;
  add more any time to fix who's speaking."
- **First time Samples is turned on in a project, when there are no samples
  yet:** an empty-state line in the samples list. "Drag across some words
  (or double-click one) and pick who's speaking. A few clean stretches per
  person is enough to start; you can keep editing and add more later."
- **Final transcript, beside (?) turns, or in the page's intro text when any
  exist:** "Wrong or unsure? Edit the turn, mark a sample of the right voice
  there, then Remake."

### 1.2 Explain the disabled switch

When the Samples switch is disabled because no paragraph is current, its
tooltip says so: "Click into a paragraph first." Alternatively, the switch
could become enabled without a current paragraph; whether it can depends on
why the restriction exists, which is for implementation to find out.

### 1.3 Show state, not a checklist

The editor's existing line becomes the state indicator. Each state below can
be derived from data Studio already has, and each names one next action:

| State | Shown |
|---|---|
| No speakers | `No speakers yet: add them in Settings › Speakers.` (as today) |
| Speakers, no samples | `No samples yet. Turn on Samples and mark a little of each voice.` |
| Some samples | `Samples: Jon 42 s · Steve 6 s`, as today (no readiness against a minimum: see 1.5) |
| A sample flagged | The existing `⚠ N with someone else in them`, with "Play it, then Clear or shorten it" |
| Samples changed since the newest final transcript | `Samples changed since the final transcript: Remake to use them.` |

### 1.4 Make "refine" an action (deferred)

From a turn in Final transcript, **Edit** already lands on the right paragraph.
A later round could have the Workspace, when you arrive by that link, offer
**Add a sample here**: Samples turns on, and you mark the stretch and pick the
speaker. That turns "add samples where the transcript went wrong" from
something you have to know into a button you can see. Deferred: the (?) hint
in 1.1 says the same thing in words, and a button there could read as a
required step. Build it only if testing shows people don't follow the hint.

### 1.5 Readiness (deferred)

**No readiness display for now.** Showing each speaker's seconds against
**Samples needed per speaker** would be simple arithmetic, and "Steve: 6 s of
the 10 s needed to be matched" would be exact: below the threshold, a speaker
isn't matched at all. But any count toward a target reads as progress toward
"complete", which implies match quality can be predicted. The threshold stays
an implementation detail, explained in Settings as it is today.

Saying when sampling is *complete* — "enough for good matches" — is a
different claim, and **it may not be computationally tractable**. Quality
depends on how clean, varied and distinct the voices are, not only on
seconds. **Open question for implementation:** whether any affordable signal
predicts match quality before matching. Candidates include the existing
`--check` result, how far apart speakers' voiceprints are, and the share of
(?) turns after a Remake. Until there's an answer, the UI makes no readiness
claim, and leaves "good enough" to the review phase, where (?) turns and
Compare show it directly.

### 1.6 Labels

Rename **Samples** to **Voice samples**: same meaning, but self-explanatory
out of context. *Teach voices* (from #6) is more action-oriented, but reads
oddly as a switch that stays on. Keep the existing summary on or beside it.

## Part 2. Does anyone need a different path?

Some people may expect the sequence other transcription tools teach. Options:

- **A. No alternate path.** Part 1's cues explain the model where it's used.
  Lowest cost, and adds no second way of doing things.
- **B. An optional checklist** beside the Samples switch: add speakers →
  sample each one → make the final transcript → review. Each step ticks
  itself from the states in 1.3, so it never disagrees with what you've done,
  and you can do steps out of order. It never blocks editing. You can dismiss
  it once per Studio; a setting brings it back.
- **C. A one-time explainer** ("How speaker samples work") the first time
  Samples is turned on: the three phases and the refine loop, then out of the
  way.

**Recommendation: A, with C if needed.** Ship Part 1's first round and watch a
first-time user. Add the explainer (C) if they still don't get the refine
loop. Build the checklist (B) only if people specifically want a sequence. A
checklist risks teaching the very "finish sampling first" model that the
incremental workflow is better than. Even so, B as specced above is safe if
it turns out to be needed.

## Constraints (from #6)

- Zero, one or many samples; no fixed number or duration required.
- Matching can be put off as long as you like. Adding a sample starts nothing
  expensive; only the existing background check runs.
- Matching stays inside **Make final transcript**. A separate Match voices
  button was considered and **rejected**: it would split back apart what
  8e567d0 merged, and put matching in two places.
- Nothing blocks transcript editing, and nothing in Part 1 can be "skipped",
  because nothing is mandatory.

## #6's acceptance criteria

| Criterion | Met by |
|---|---|
| A first-time user understands what a sample is, what it affects, and what happens next | 1.1, 1.3, the three phases |
| Users can still add zero, one or many samples and postpone matching | Constraints; Part 1 adds no steps |
| The current workflow state is visible without blocking editing | 1.3 |
| The guided layer can be dismissed and adds no mandatory steps | Part 2 (B is dismissible; A and C add nothing persistent) |
| Collection, matching and review are distinct | The three phases; 1.1 and 1.3 wording |

## Open questions

1. **Can sample completeness be estimated affordably** (1.5)? Answer this in
   implementation, before showing any readiness at all.
2. Why does the Samples switch need a current paragraph (1.2), and can that
   go?
3. Should adding speakers move from Settings into the Workspace, so the first
   step happens where the samples are marked?
4. Where does "dismissed" live for B: per Studio (settings) or per project?
