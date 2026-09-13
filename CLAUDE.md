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
- **Knob changes are live; shape changes restart.** `Channel.control_changes()`
  compares the rendered conf with the one beside the pid file *with every
  `control` block removed*. Same shape -> `Channel.set_controls()` sends one
  `pw-cli set-param <sink id> Props '{"params":["node:control",v,...]}'` and
  rewrites the conf; different shape (effect added/removed/moved/switched off,
  a filter's steepness) -> restart. Measured: a live change lands in ~28 ms with
  the same pid. Anything that changes a node's `config` is a shape change, which
  is why the delay's `max-delay` is sized for the knob's whole range.
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
- `~/.lv2/LV2` on this box is an empty directory; lilv logs a harmless
  "failed to open .../manifest.ttl" for it in every channel log.
- **A test `Engine.apply()` in the real runtime dir STOPS the user's channels**:
  every channel not in the test config is an orphan. Live experiments must set
  `XDG_RUNTIME_DIR=/run/user/1000/arlab PIPEWIRE_RUNTIME_DIR=/run/user/1000
  PULSE_RUNTIME_PATH=/run/user/1000/pulse`.
- **EasyEffects and Audio Router cannot run together.** EasyEffects relinks
  every app stream onto `easyeffects_sink`, so a "Send to" silently snaps back
  and the stream reads "not routed" (owner hit this 13/09/2026). Detected by
  that sink in the graph (`Graph.conflicting_routers()`); the window shows a
  banner and `status` prints a warning first.
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
- **`Engine.apply()` blocks the GUI thread** while it starts processes and waits
  for each sink (up to 8s in the worst case, ~0.4s in practice). It runs under a
  busy cursor. Moving it to a worker thread is the obvious next robustness step.
- **The device list is hardware only.** `Graph.devices()` filters on `device.id`,
  so a virtual sink a channel targets is not in the list; the panel still shows
  it and asks `device_present` before calling anything "not connected".
- The monitor thread must never touch a widget: `gui/monitor.py` turns each
  graph callback into a Qt signal and coalesces bursts into one refresh.

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

**VST is phase 2 and cannot go through filter-chain** - PipeWire has no VST
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
