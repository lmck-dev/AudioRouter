# AudioRouter - notes for the next session

Per-app audio routing with independent per-channel effects. Native PipeWire
filter-chains, one `pipewire -c <conf>` process per channel. **EasyEffects
multi-instance was ruled out by measurement - do not re-propose it.**

Run the tests with `python -m unittest discover -s tests -t .` (no PipeWire
needed, no dependencies).

## Layout

| module        | holds                                                     |
|---------------|-----------------------------------------------------------|
| `plugins.py`  | what plugin backends this machine can load                 |
| `turtle.py`   | dependency-free Turtle reader (LV2 descriptions)           |
| `lv2.py`      | installed LV2 plugins, their controls, which we can host; cached |
| `effects.py`  | curated effects + plugin effects, rendered as a stereo graph |
| `channels.py` | conf generation, process lifecycle, pid files              |
| `pwgraph.py`  | read-only `pw-dump` model + live monitor                   |
| `routing.py`  | rules, planning, and the actual move                       |
| `config.py`   | atomic JSON persistence                                    |
| `engine.py`   | **policy**: reconcile config against reality; auto-router  |
| `install.py`  | menu launcher (.desktop) and login service (systemd user unit) |
| `cli.py`      | argument parsing and printing only - no logic              |

The GUI drives `Engine` directly. Anything that needs thinking
belongs in the engine, never in the CLI.

## Traps that have already cost time

- **A filter graph must declare its `inputs` and `outputs` explicitly.**
  PipeWire derives a filter's ports from every *unconnected* port, so one
  `mixer` node (8 inputs, 1 used) makes the filter look 8-in/1-out. The graph
  then refuses to start - `invalid ports ... Check inputs and outputs objects` -
  and **the channel is silent while still appearing in the graph, fully linked,
  looking perfectly healthy.** `render_chain()` returns the entry and exit ports
  for exactly this reason.
- **`pipewire -c <name>` resolves a relative path against PipeWire's config
  directories, not the working directory.** A relative name fails with
  `can't load config: No such file or directory` and exit 254. `Channel` always
  passes an absolute path; hand-written test confs must too, or you will spend a
  cycle "proving" a bug that never ran.
- **`log.level: 0` in a channel conf hides the reason it died.** Raise it to 2
  or 4 in the rendered conf when a channel will not start, then read
  `$XDG_RUNTIME_DIR/audiorouter/<slug>.log`.
- **The LV2 loader is a separate package** (`pipewire-module-filter-chain-lv2`,
  installed on this box 13/09/2026). Without it no LV2 effect can run;
  `plugins.py` probes for it and the engine degrades with an installable message
  instead of a crash.
- **`application.process.binary` is the executable, not the command.** `paplay`
  reports `pacat`; a `binary` rule for "paplay" matches nothing. Prefer `app`.
- **A filter-chain's own playback node has `media.class = Stream/Output/Audio`,
  exactly like an application stream.** Routing one would feed a channel back
  into a channel. Every node we create is stamped `audiorouter.channel=<slug>`
  and `Node.is_app_stream` excludes them. Keep that stamp.
- **`pw-play --target=<node id>` silently falls back to the default sink** - it
  needs `object.serial` or a unique node name. This is how a test tone once went
  through the speakers at full volume.
- **Node ids and `object.serial` change on every restart.** Never persist
  either. Our own `node.name` is the only stable handle, which is most of why
  this architecture was chosen.
- **`pw-link -l` cannot disambiguate duplicate node names** (EasyEffects names
  every sink `easyeffects_sink`). Use `pw-dump` and node ids.
- **`pkill -f <pattern>` matches Claude's own shell command line and kills the
  turn.** Kill by pid from `pgrep -x`, after checking `/proc/<pid>/cmdline`.
  `pgrep -x pipewire` also matches the user's main daemon - always check the
  cmdline before killing.
- **Knob changes and on/off are live; shape changes restart.** `Channel.control_changes()`
  compares the rendered conf with the one beside the pid file *with every
  `control` block removed*. Same shape -> `Channel.set_controls()` sends one
  `pw-cli set-param <sink id> Props '{"params":["node:control",v,...]}'` and
  rewrites the conf; different shape (effect added/removed/moved/switched off,
  a filter's steepness) -> restart. Measured: a live change lands in ~28 ms with
  the same pid. Anything that changes a node's `config` is a shape change, which
  is why the delay's `max-delay` is sized for the knob's whole range.
- **Every effect sits behind a crossfading switch** (`effects._with_bypass`,
  15 Sep 2026): wet = effect x `ramp`, dry = input x (1 - ramp), summed. The
  builtin `ramp` is AUDIO rate and moves toward `Stop` from wherever it is, so
  on/off is ONE live change (`sw<i>_target:Add` 1/0) and the 50 ms linear fade
  (`SWITCH_FADE_S`) runs in the audio thread. Measured against the old
  stepped `pw-cli` fade: sample-to-sample jumps 1.00x a clean sine (old
  3.2-6.3x, i.e. clicks), 10-90% in 40.0 ms as a straight line (old: 20 ms
  stairs), a switch reversed mid-fade turns round at -13 dB with no jump, and
  apply is ~28 ms (old ~90 ms, sleeping between steps). A switched-off effect
  still uses CPU.
- **A `ramp`'s controls CANNOT be set from a conf or `Props`.** Its ports declare
  no range and filter-graph's `port_set_control_value` clamps every value to
  the range: Start/Stop/Duration all read 0 and the switch passed only the dry
  sound. Control LINKS bypass the clamp, so each ramp control is fed from a
  `linear` node's `Notify` (`Control*Mult + Add`; `Add` has a range of +-10).
  A test asserts no ramp node carries a `control` block - keep it.
- **A ramp starts at 0**, so every new host faded dry->wet and a restart let
  4 ms of +6 dB through (3/3 runs). One `fadeclock` ramp per chain rises
  0 -> 50 ms and feeds every switch's Duration: a new host snaps to its state
  in one sample. Verified: no blip on restart (3 runs), on the first sound
  after the sink was SUSPENDED, or with the effect starting off.
- **Lab captures: `parec --latency-msec=20` and wait before SIGINT.** Without
  it the last ~1-2 s of a capture was missing, which read as "later switches
  do nothing" and nearly sent the fade work down the wrong path.
- An interactive `pw-cli` fed over stdin does NOT exit when stdin closes - it
  hung a test for two minutes.
- `pw-dump`'s `Props` readback did not show a live `set-param` change that was
  audibly applied: measure the audio, do not trust the readback.
- **Restarts are make-before-break** (`Channel.start(handover=...)`). The old
  way stopped the host first: measured, a stream fell back to the DEFAULT
  OUTPUT for ~110 ms - unprocessed sound on the speakers, even from the
  headphones channel. Now the new host starts beside the old one, its sink is
  found by process id (both sinks share the name for a moment, and
  `Channel.sink_node` lets the pid file decide), `Engine._hand_over` moves the
  streams and waits until each is linked to the new sink (~43 ms), and only
  then is the old host stopped. A new host that fails leaves the old one
  running. Measured: no silent 5 ms slice and no level jump across a restart.
- **`kill(pid, 0)` succeeds on a zombie.** Channel hosts are our children, so
  every stop waited its full 4 s and then SIGKILLed a process that had already
  exited; the login service also collected zombie hosts. `_pid_alive` reaps
  our own children and reads `/proc/<pid>/stat` for anyone else's.
- **`node.dont-reconnect=true` on a test stream blocks moves entirely**, so a
  handover measured with it proves nothing (the stream never moved).
- **Every chain is an explicit stereo graph.** Mono nodes are placed per side
  (`name_l`/`name_r`); a stereo plugin is one node fed both sides, which is what
  makes its dynamics linked. PipeWire's own per-channel replication of a mono
  graph would make stereo plugins impossible. Node control names follow the
  node names, so the `_l`/`_r` suffixes are part of the live-update contract.
- **LSP limiter defaults defeat a "ceiling".** `boost` (default on) turns the
  level back up by what was taken off - a -20 dB ceiling changed the level by
  0.5 dB - and `alr` (automatic level regulation) pulls peaks ~8 dB *below* the
  ceiling. The curated limiter sets both to 0; measured peaks then sit exactly
  at -20.00 / -12.00 dB.
- **Turtle blank nodes must be tagged per parse with a counter, not `id()`.**
  Python reuses ids as soon as a parser is freed, and every LSP plugin silently
  received every other plugin's ports (a compressor with 15 audio inputs).
- **The plugin catalogue is cached** at `~/.cache/audiorouter/lv2-catalogue.json`,
  keyed by the mtime/size of every `.ttl`; a cold build reads ~16 MB of Turtle
  and takes ~5 s. Bump `lv2.CACHE_VERSION` when `Plugin`/`Control` change shape
  or the hiding rules change, or users keep stale data.
- **LSP "link" controls do nothing here** (they need shared memory between
  plugin instances); they are hidden with the UI-only toggles in `lv2._UI_ONLY`.
  LSP impulse-response plugins load but need a sample file we cannot pass yet.
- **"Is this LV2 plugin installed" is answered by the parsed catalogue**
  (`plugins.lv2_installed`). A text search of manifests missed Rubber Band
  (`rubberband:livestereo` against a prefix ending in `#`) and SWH plugins, and
  their channels refused to start as "not installed".
- **The effects toolbox** (installed 13/09/2026): `lv2-noise-suppression-for-voice
  lv2-rubberband-plugins lv2-swh-plugins lv2-x42-plugins lv2-guitarix-plugins
  lv2-vocoder-plugins lv2-abGate lv2-eq10q` -> 489 usable plugins. Measured live:
  RNNoise, GxWah, GxTremolo, x42-Autotune, SWH Flanger all process; Rubber Band
  +12 semitones turned 440 Hz into 880 Hz. Vocoders are unusable (they need a
  second carrier input). The browser groups by `lv2.categorise()` (LV2 class,
  then name) with the maker in a column; search prefers an effect's own name
  over its group's name.
- `~/.lv2/LV2` on this box is an empty directory; lilv logs a harmless
  "failed to open .../manifest.ttl" for it in every channel log.
- **A test `Engine.apply()` in the real runtime dir STOPS the user's channels**:
  every channel not in the test config is an orphan. Live experiments must set
  `XDG_RUNTIME_DIR=/run/user/1000/arlab PIPEWIRE_RUNTIME_DIR=/run/user/1000
  PULSE_RUNTIME_PATH=/run/user/1000/pulse`.
- **Never run `init` in the lab config.** It adds a Speakers channel with the
  same slug as the owner's, so the lab host takes the same node name and
  `apply` moves the owner's playing apps onto it; stopping it drops them onto
  the raw device. Done 14 Sep 2026 (Zen, sent back by hand). Add only
  `lab*` channels.
- **The werman RNNoise LV2 plugin (`lv2-noise-suppression-for-voice` 1.10) is
  broken under filter-chain.** Measured 14 Sep 2026 on natural speech (Kokoro)
  through a fake mic: half level, ~63 ms late, residual +3.2 dB against the dry
  signal, 3x the clicks; erratic at forced quanta of 480 and 1024 too. The same
  audio through system `librnnoise.so.0` directly (ctypes, 480-sample frames) is
  near-transparent: gain 0.99, residual -16.5 dB. Its settings are LV2 patch
  parameters on an atom port, which filter-chain cannot send, so it also shows
  no controls. Espeak is a poor test voice for RNNoise; use Kokoro
  (`~/.claude/hooks/speak-kokoro.py`). `lv2.KNOWN_BROKEN` marks both unusable.
- **Our own RNNoise plugin** (`audiorouter/native/`, URI
  `urn:audiorouter:rnnoise`, "Voice noise suppression"). C with no headers:
  the LV2 and librnnoise ABIs are declared in the file, linked with
  `-l:librnnoise.so.0`. `native.ensure_rnnoise()` compiles it into
  `~/.lv2/audiorouter-rnnoise.lv2` when missing or older than the source; the
  GUI and `watch` call it at start, and it never raises. Latency is 1440
  samples: 480 of frame buffering plus RNNoise's own 960, which the dry side of
  "Amount" is delayed to match (a 50% mix measured closer to dry than 100%, so
  no comb). Measured live through a fake mic: speech level unchanged and
  residual -16.4 dB, the same as librnnoise run directly; 37 dB of fan/hum noise
  removed; pauses at -90 dB, and digital silence with the voice gate on; knob
  changes live. RNNoise can rate a steady sine as voice, so gate tests use noise.
  Lab runs with a scratch plugin need `LV2_PATH` *and* `XDG_CACHE_HOME` set, or
  the lab rebuilds the owner's catalogue cache.
- **EasyEffects and Audio Router cannot run together.** EasyEffects relinks
  every app stream onto `easyeffects_sink`, so a "Send to" silently snaps back
  and the stream reads "not routed" (owner hit this 13/09/2026). Detected by
  that sink in the graph (`Graph.conflicting_routers()`); the window shows a
  banner and `status` prints a warning first.
- **Test tones must not play as `paplay`.** The owner's config has a remembered
  rule `paplay -> Speakers` (a "Send to" on one of Claude's test tones), and the
  login service routed five test beeps onto the real speakers on 13/09/2026.
  Play with `--client-name=ar-lab-tone --property=application.name=ar-lab-tone`,
  and confirm with a *silent* file where the stream lands before any audible one.
- **Channel volume is the sink's own volume, not a config value.** Set with
  `wpctl set-volume/set-mute <sink id>`, read from the node's `Props`
  `channelVolumes` (cube root = the % every desktop slider shows; measured 50% =
  -18.06 dB, 25% = -36.13 dB). WirePlumber restores it by node name when a
  channel restarts, and the desktop's sound settings change the same value, so
  storing a copy would only fight them.
- **Input channels and virtual cables** (13/09/2026). `Channel.kind` is
  `output` or `input`. An input's filter-chain captures the mic (`ar_<slug>_in`,
  `node.passive` so the real mic stays suspended until something records) and
  plays into a virtual mic `ar_<slug>` (`media.class = Audio/Source`); `listen`
  adds a `libpipewire-module-loopback` in the same conf into an output channel.
  A `recordable` output plays into `ar_<slug>_rec` (Audio/Source) and, unless
  its device is `NOWHERE`, a loopback plays that to the device. Measured: -6.00
  and -12.00 dB exactly, recorded as an app would. **Filter controls live on the
  capture-side node** (`control_name`): the sink for outputs, `ar_<slug>_in`
  for inputs. Apps pick inputs from their own list - there is no input-side
  routing, and `sink_map` holds outputs only.
- **Internal streams carry `node.dont-fallback`.** Measured without it: when
  the output a loopback played into vanished, WirePlumber relinked it onto the
  REAL SPEAKERS - for a listen-through, a live mic on the speakers.
- **A restart hands over three kinds of stream** (`Engine._hand_over`): apps
  AND other channels' own streams feeding the sink (an input's listen-through
  was missed at first), recorders of the virtual source, and the new host's own
  loopbacks, which find their source by name and may attach to the old host's
  copy. The "is the node present" check must cover every channel, not
  `sink_map` - inputs restarted on every apply, knob changes included.
- **Never mute a stream to hide a handover blip.** WirePlumber's restore-stream
  saves mute per stream (keyed by `media.name`) and restored it onto the next
  listen stream: listen-through went permanently silent. A stale test entry for
  "Lab mic (listening) output" is left in `~/.local/state/wireplumber/
  stream-properties`. The remaining blip: a listen-through doubles (+6 dB) for
  ~90 ms when the input channel restarts (adding/removing/moving an effect).
- **The login service runs this checkout.** Whatever branch is checked out is
  what `audiorouter.service` loads the next time it restarts.

## Testing audio without hardware

The analog codec on this machine sometimes fails to initialise at boot
(`snd_hda_intel 0000:0f:00.6: Codec #0 probe error`), leaving no output device
at all. Testing does not need one:

```sh
pactl load-module module-null-sink sink_name=ar_testdev \
      sink_properties=device.description=ARTestDevice
paplay --device=ar_test_<slug> tone.wav      # play into the channel
parec --device=ar_testdev.monitor ... out.wav # capture what came out
```

**Stop `parec` with SIGINT, not SIGTERM**, and wait for it: SIGTERM drops its
unwritten buffer, so a short capture comes back empty and reads as silence -
which once looked exactly like a broken channel.

Measure the RMS of the capture against a reference played straight into
`ar_testdev`. That is how `-6 dB` gain and a 3-stage 1 kHz highpass were both
confirmed to within 0.05 dB.

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

## Launcher and login service

`python -m audiorouter launcher` writes `~/.local/share/applications/audiorouter.desktop`;
`python -m audiorouter login on|off|status` (or the window's "Keep routing with
this window closed" box) writes `~/.config/systemd/user/audiorouter.service`,
which runs `watch`. **Nothing is pip-installed**, so both files pin
`sys.executable` and `PYTHONPATH=<checkout>` - moving the checkout means
re-running `launcher` and toggling login off/on.

- **The daemon's record is `routing.daemon`, NOT `daemon.pid`.** Every `*.pid`
  in the runtime dir is taken to be a channel's, so `orphan_slugs()` (called by
  every `status()`) deleted `daemon.pid` within a second of the GUI refreshing.
  Found only by enabling the real service with the real GUI open.
- **`KillMode=process`** - channels are children of whoever started them and
  must survive a daemon restart. Verified: `systemctl --user restart` left both
  channel pids unchanged.
- **The daemon reloads the config on each graph event** (`follow_config=True`),
  because the GUI edits it from another process. The GUI's own router never
  reloads - its panels hold the objects a reload would replace. The GUI does
  not start its own router while `daemon_pid()` finds one.
- **`watch` exits 1 when `pw-dump` dies** (`GraphMonitor.ended`) so systemd
  restarts it. Before this it sat forever routing nothing. Verified by killing
  only the daemon's own `pw-dump` child: restart in 3s, channels untouched.
- **`apply()` restarts a running channel whose sink is missing from the graph.**
  A host process can outlive the PipeWire it was attached to; conf unchanged
  plus pid alive used to count as healthy. Unit-tested only - verifying it live
  means restarting the user's PipeWire.

## Remembering apps (owner ruling 2026-09-12)

**The first "Send to" for an app that no rule covers remembers it** on that
channel (`Engine.send(..., remember_new=True)`). A later "Send to" for an app
that already has a rule is a one-off and changes nothing saved. "Always send
this app here" (`Engine.remember_app`) UPDATES the app's existing rule rather
than appending one - rules are first-match-wins, so a second rule for the same
app would be listed and never take effect.

## Effects (13/09/2026)

Owner chose **LV2 first, VST later**. The window's "Add effect..." opens a
searchable browser: built-in effects, then every usable installed LV2 plugin
grouped by maker (246 of 300 on this box: LSP, Calf, MDA, ZAM). Controls are
generated from each plugin's ports: switches, lists, sliders (logarithmic where
the plugin says so), linear gains shown in dB. CLI: `effects --plugins [search]`,
`effects --plugin URI`, `effect add <slug> lv2 --plugin URI k=v`.

**VST is a FUTURE POSSIBILITY, not planned** (owner, 15 Sep 2026: the LV2 set is rich enough, and anything missing can be built ourselves, as with the noise suppression). It cannot go through filter-chain - PipeWire has no VST
loader. The agreed route is a Carla host per channel (`Carla`, `Carla-vst` in
the Nobara repos): sink -> Carla -> device, VST block at the end of the chain,
each plugin's own editor window, state in a Carla project file.

Plugins with 1 in / 2 out (wideners), several ins/outs, or instruments are
listed as unusable with the reason. Plugin windows (LV2 UIs) are not shown.

## State

Engine, CLI, GUI, routing, auto-router, launcher and login service are built and
verified against live PipeWire. The login service was verified routing a
stream from a rule saved by a different process, with no window involved.
Not yet verified: an actual logout/login cycle.
