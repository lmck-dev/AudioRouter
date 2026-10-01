# The GUI and level meters

Moved out of `CLAUDE.md` on 25 Sep 2026 so it loads only when the work touches it.
Read this file before changing the code it describes.

## The GUI

`audiorouter/gui/` - PyQt6, entered at `gui/main.py`. It edits the config and
asks `Engine` to make reality match; it decides nothing about audio itself.

- **Colours come from the palette** (`gui/theme.py`), never hardcoded. A
  hardcoded dark scheme in JoyCal made every label invisible on a light Plasma
  theme. The two derived colours (warn, good) are the exception, and each has a
  light and a dark value.
- **Render it and look at it.** `QWidget.grab().save(path)` under
  `QT_QPA_PLATFORM=offscreen`; `spectacle` is broken on this box. Doing that is
  what caught the form bug below - the tests were all passing.
- **A form inside a `QScrollArea` needs `setSizeConstraint(SetMinAndMaxSize)`**
  or a 30-knob plugin is crushed into overlapping few-pixel rows instead of
  scrolling. Offscreen, the scrollbar only appears after a few
  `processEvents()` passes - a single pass renders a (false) missing scrollbar.
- **`EffectsPanel.set_channel()` ignores the channel it already shows.** Every
  apply and every graph event re-selects the channel, and a live knob change
  *causes* a graph event: rebuilding then destroyed the slider mid-drag.
- **Offscreen harnesses must process `DeferredDelete`** (`QCoreApplication.
  sendPostedEvents(None, QEvent.Type.DeferredDelete.value)`), not only
  `processEvents()`: replaced table cell widgets are `deleteLater()`-ed, and
  without it a stale "Send to" combo rendered over the Application column - a
  render bug that does not exist in the real event loop.
- **The volume slider ignores graph readings for 0.8 s after a local edit**
  (`VOLUME_SETTLE_S`): a refresh can carry the value from just before the change
  and would yank the slider back. Switching channel resets that window.
- **Two signals, two delays.** `EffectsPanel.changed` (shape) waits
  `APPLY_DELAY_MS` and shows a busy cursor; `tuned` (knob) starts a
  `TUNE_DELAY_MS` timer only if none is running, so a drag is heard while it
  happens, and a tune-only apply skips the full window refresh.
- **`QFormLayout.removeRow()`, never `takeAt()` + `deleteLater()`.** deleteLater
  only schedules destruction, so the previous effect's labels stay painted
  underneath the new ones and the text overlaps into gibberish.
- **One debounce, in the window.** The panels report every edit immediately (so
  nothing typed is lost) and `MainWindow._pending_apply` decides when to restart
  audio. An earlier version also debounced inside the effects panel, which made
  every change take 1.3s to be heard.
- **`Engine.apply()` runs on a worker thread** (`gui/applier.py`, 15 Sep 2026).
  It used to block the window for a whole restart (up to 8 s). Each run
  applies a *copy* of the config (`to_dict`/`from_dict`) inside its own
  `Engine`, because the panels edit `Channel`/`Effect` objects in place. Only
  one run at a time; edits made meanwhile fold into ONE follow-up with the
  newest settings. Closing the window runs any queued edit and waits (never two
  hosts for one channel). Measured live in the lab: `apply_now()` returns in
  0.2 ms, a 115 ms restart ran with the event loop never stalled over 11 ms.
- **A queued call to a plain Python method goes through a hidden PyQt proxy
  object**, so `QCoreApplication.sendPostedEvents(receiver)` never delivers it.
  `Applier._on_done` is a `@pyqtSlot` for that reason, or `flush()` on close
  silently skips the result.
- **GUI tests: `GuiTestCase` mocks `Engine.apply` for the whole test**, because
  closing the window (in cleanup, after a test's own mock has ended) runs any
  queued apply, which would start real channel hosts. Call `self.settle()`
  after `apply_now()` before asserting on results.
- **The device list is hardware only.** `Graph.devices()` filters on `device.id`,
  so a virtual sink a channel targets is not in the list; the panel still shows
  it and asks `device_present` before calling anything "not connected".
- The monitor thread must never touch a widget: `gui/monitor.py` turns each
  graph callback into a Qt signal and coalesces bursts into one refresh.


## Level meters (15 Sep 2026)

`meter.py` (engine side, no Qt, no numpy) + `gui/meters.py`. The selected
channel gets In (before effects) and Out (after) meters: one bar per ear with
the average (~300 ms), the peak, a peak-hold tick and a clip marker - the
owner's choice of style. Each side is a `parec --raw` *tap*:

| channel | In | Out |
|---|---|---|
| output | `--device=ar_<slug>.monitor` | `--monitor-stream=<serial of ar_<slug>_out>` |
| virtual cable | same | `--device=ar_<slug>_rec` |
| input | the mic (`device` or `@DEFAULT_SOURCE@`) | `--device=ar_<slug>` |

Measured live on a -20 dB channel: In -6.02, Out -26.02 dB, for all three
kinds, through the real window. Two taps cost ~1.5% of one core.

- **pipewire-pulse numbers sink inputs by `object.serial`** (checked against
  `pactl -f json list sink-inputs`), so the graph snapshot gives the Out tap.
- **Taps carry `node.dont-reconnect`/`node.dont-fallback`** (parec has no
  no-move flag) so a meter never wanders onto the real mic or another device,
  and **`audiorouter.meter=true`**, which `Engine._hand_over` skips: a
  metadata "move" of a stream that refuses moves still reports success, and
  the handover then waited its full timeout on every restart.
- **A restart ends the Out tap** (its playback stream is replaced). `Tap.key`
  includes the host pid so a graph event reopens taps, but after a restart
  settles there may be NO further event - the Out meter stayed dark for good.
  The panel therefore schedules its own retry and fetches a fresh graph
  (`graph_source`): 0.3 s the first time, 2 s after repeated endings (a mic
  that stays unplugged). Measured: Out back within 1 s of a restart.
- **Meters run only while the window is shown** (`showEvent`/`hideEvent`,
  which minimising also sends) and only for the selected channel. An input
  channel's In meter opens the mic; the owner accepted the desktop's
  "mic in use" indicator while one is selected.
- **`LevelReader` must close its pipe** or every reopened meter leaks a file
  descriptor. GUI tests replace `LevelReader` module-wide with `FakeReader`
  (`GuiTestCase`), so no test can start a real parec even if a window is shown.
- The two `ResourceWarning: subprocess ... still running` lines in a full run
  come from `tests/test_channels.py` and predate the meters.

## Two channel lists and a foldable Playing now (30 Sep 2026)

- **Outputs and inputs are separate lists** (`output_list`, `input_list`),
  each item carrying its slug in `UserRole`. The selection is a slug
  (`MainWindow._selected_slug`), not a row: select with
  `MainWindow.select_channel(slug)`, which also clears the other list. Tests
  select by slug for the same reason.
- "New output" / "New input" replace the old "what kind?" question.
- **Playing now folds** behind its heading, which counts the streams, and
  Remembered apps (`MainWindow.rules`, placed with `StreamsPanel.add_beside`)
  folds with it. The state
  is view state, so it lives in `QSettings` (`~/.config/audiorouter/gui.conf`),
  never in `config.json` (the daemon's file). **Tests must use
  `isolated_settings(tmp)`** or they write to the real `gui.conf`.

## Levels follow the highlighted effect (30 Sep 2026)

With "Show the highlighted effect" ticked (default, remembered in `gui.conf`),
In/Out are what the effect highlighted in the chain receives and puts out.
Unticked, or where no tap can be read, they are the whole channel as before.

- **Our own level tap** (`native/meter/`, `urn:audiorouter:meter`) listens at
  every boundary: slot 0 before the first rendered effect, slot k after the
  k-th (`effects.tap_slots` maps config positions to slots - a switched-off
  effect whose plugin is missing is not rendered and has none). Taps are
  always in the graph when `effects.taps_available()`, so changing the
  highlight never restarts anything.
- **PipeWire lists a plugin's output controls in `Props` but never refreshes
  them** (an LSP compressor's `ilv_l` stayed 1.0 with a tone playing). So the
  tap writes a ring of 64 {peak_l, peak_r, ms_l, ms_r} readings, one per 1024
  frames, to `$XDG_RUNTIME_DIR/audiorouter/meters/<host pid>.<slot>`
  (mmap), and `meter.FileLevelReader` polls it every 30 ms. The file appears
  only once audio flows (a suspended channel has none: silence, not an
  error); the plugin unlinks it on cleanup, and `Engine.apply` sweeps files
  of killed hosts.
- **A graph output port cannot also feed a tap**: "output port ... already
  used by link, use copy", the graph fails and the channel passes SILENCE.
  A tapped chain therefore ends in `tail_l`/`tail_r` copy nodes.
- **A host started without taps has none** (older version, plugin built
  later): `meter.host_has_taps` reads the running conf, and the panel says
  per-effect levels start at the next restart. Where taps can never run (no
  compiler), the tick box is hidden and nothing nags.
- Tests: `tests/__init__.py` pins `channels.taps_available` to False for
  the whole suite, and `GuiTestCase` pins the panel's copy, so whether this
  machine built the plugin cannot change a test. Patch them to True to test taps.
- Measured live (lab, 30 Sep 2026): -20 dB tone through gain -6 / compressor
  / gain -10 read -20/-26, -26/-26, -26/-36 per effect through the window's own
  code, device -36.00 unchanged; a knob change stayed live (same pid, Out
  -36 -> -30); host CPU 0.25% of a core with or without four taps.

## The mixer (30 Sep 2026)

`gui/mixer.py`, the first tab and the default view (owner: an extra view AND
the default; the Channels tab keeps every setting). One `ChannelStrip` per
channel, inputs left, outputs right, top to bottom in signal order: name,
kind, source (mic combo / apps playing), INSERTS (one lit toggle per effect;
double-click opens it in Channels; "+ Insert" opens the browser via
`EffectsPanel.choose_effect`), OUT or LISTEN, M + Edit, fader with dB scale
and a stereo meter, volume in dB.

- **Strips emit, the window decides**: `MainWindow._set_volume/_set_muted/
  _toggle_effect/_set_device/_set_listen` work by slug and go through the same
  save + debounce as the Channels view. Two faders dragged at once both land
  (`_set_volume` flushes another channel's pending value).
- **Rebuilt only when the desk's shape changes** (a signature of channels and
  effects); a plain refresh updates values, or a fader would vanish mid-drag.
- **Strip meters read each channel's last level tap** (`meter.output_tap`):
  no parec, and an input moves only while something records it (its capture
  is passive). Every tapped chain, even an empty one, has an output tap.
  Only the visible view's meters run (`MainWindow._update_meters`).
- **Inserts scroll inside the strip** and names are elided in the middle;
  otherwise a long chain pushed the fader off the bottom of the desk.
- **Two levels, like a console** (owner asked, 30 Sep 2026). TRIM (small,
  near the top) is the sink's volume - the desktop's - which acts BEFORE the
  effects (measured: 50% = -18.06 dB already at tap 0), so it sets how hard
  they are driven. The big FADER is `Channel.fader_db`, a `mixer` gain pair
  (`fader_l`/`fader_r`) that ends every chain, AFTER the effects; moving it
  is a live control change (measured: same pid, 0/-6/+6/off gave exactly
  -48.06/-54.06/-42.06/silence while the last effect's output stayed -48.06).
  Law: `mixer.FADER_LAW`, +10 dB top, 0 dB at 76% of travel, off at the bottom.
  The Channels view does not show the fader.
- **Pan and solo live at the fader** (1 Oct 2026). Both multiply into the
  `fader_l`/`fader_r` gains, so both are live control changes, never
  restarts. Pan (`Channel.pan`, -1..+1) is a BALANCE: centre leaves both
  sides alone, the far side fades on a quarter cosine (-3 dB at L50/R50, off
  at the end), the near side never rises. Double-click the slider to centre.
  Solo (`Channel.solo`, saved, so the login service agrees) cuts every other
  ENABLED channel of the SAME KIND - an output solo never cuts the mic a call
  is using. The cut (`Channel.solo_cut`) is never saved: `Config.update_solo()`
  works it out on load, add/remove, and at the top of `apply`/`start_channel`;
  call it after changing any `solo`. Measured live (lab): L50 = -3.01 dB on R,
  hard pan and a cut = digital silence, release restores -23.04 exactly, one
  host pid throughout.
- **The output tap is after the fader** (`effects.output_slot` = rendered
  effects + 1). `meter.output_tap` reads the slot from the RUNNING host's
  conf, so a host started by 0.4.0 (no fader) still meters until it restarts.
- **Meters are LED segments in zones**: green below -18 dBFS, amber to -6,
  red above (`meters.AMBER_FROM_DB`, `RED_FROM_DB`); lit to the average,
  half-lit to the peak, faint above. Zone colours are theme-derived with a
  light and a dark value (`Theme.meter_*`), like warn/good.
- **Lit inserts are a 30% tint of the highlight behind the normal text**, with
  a solid left bar. A full highlight with white text was hard to read (owner).
