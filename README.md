# Bram Studio

A narrated-screencast studio. Record your screen once, then narrate over it: play,
pause, scrub and jump through the recording while you talk, and the take follows your
playhead. Pause to think and the dead air is cut. Takes are named from your first
spoken words.

- It supports the production style of
  [Heavy Metal Umlaut](https://jonudell.net/udell/2005-01-22-heavy-metal-umlaut-the-movie.html).
  Its first use is narrating Bram activity, and it isn't limited to that.
- Along the way we have needed, built, and are shipping a new core XMLUI component,
  `MediaPlayer` ([xmlui-org/xmlui#3913](https://github.com/xmlui-org/xmlui/issues/3913)).

Built with and inside [Bram](https://github.com/judell/bram). macOS only for now (the
voice recorder uses AVFoundation).
