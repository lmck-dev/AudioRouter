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
| `effects.py`  | effect catalogue + rendering into filter-graph fragments   |
| `channels.py` | conf generation, process lifecycle, pid files              |
| `pwgraph.py`  | read-only `pw-dump` model + live monitor                   |
| `routing.py`  | rules, planning, and the actual move                       |
| `config.py`   | atomic JSON persistence                                    |
| `engine.py`   | **policy**: reconcile config against reality; auto-router  |
| `cli.py`      | argument parsing and printing only - no logic              |

The GUI (not written yet) drives `Engine` directly. Anything that needs thinking
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
- **No LV2 loader on this box.** `/usr/lib64/spa-0.2/filter-graph` has only
  `builtin`, `ebur128` and `ladspa`. The compressor and limiter therefore cannot
  run until `pipewire-module-filter-chain-lv2` is installed (or they are ported
  to the LADSPA build of LSP, whose loader *is* present). `plugins.py` probes
  this and the engine degrades with an installable message instead of a crash.
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
- **Changing an effect needs the channel restarted.** `Engine.needs_restart()`
  compares the rendered conf against the copy written beside the pid file, so
  `apply` restarts exactly the channels that changed. Live parameter updates
  would be a real feature, not a tweak.

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

Measure the RMS of the capture against a reference played straight into
`ar_testdev`. That is how `-6 dB` gain and a 3-stage 1 kHz highpass were both
confirmed to within 0.05 dB.

## State

Engine, CLI, routing and the auto-router are built and verified against live
PipeWire. **No GUI yet, and no toolkit chosen.** Nothing is installed as a
service; `watch` is the daemon and runs in the foreground.
