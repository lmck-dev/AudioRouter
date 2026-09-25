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
