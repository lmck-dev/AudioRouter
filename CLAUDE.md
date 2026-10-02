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

## The user guide

`audiorouter/guide/user_guide.md` (+ `signal-flow.png`) is the guide for people
USING the app, opened by the window's **User Guide** button (`gui/guide.py`) and
linked from the README. It ships in the package (`pyproject` package-data), so
the installed app shows the installed version's guide. **A change to what a
control does or says must update the guide in the same commit.** The owner also
keeps a Claude Docs copy (1 Oct 2026); the repo file is the source of truth.

## Feature notes live in `docs/` — read the one you are touching

| Working on | Read first |
|---|---|
| Effects, LV2 plugins, bypass/crossfade switches, RNNoise | `docs/effects.md` |
| Anything in `audiorouter/gui/`, level meters | `docs/gui.md` |
| `install.py`, launcher, login service, `packaging/` | `docs/packaging.md` |
| Echo cancellation | `docs/echo-cancel.md` |

These rules come from those files, and they apply even when you haven't opened them:
- **The werman RNNoise LV2 is broken under filter-chain.** Use our own `urn:audiorouter:rnnoise`.
- **Bump `lv2.CACHE_VERSION`** when `Plugin`/`Control` change shape or the hiding rules change.
- **GUI colours come from `gui/theme.py`**, never hardcoded. Render the window and look at it (`QWidget.grab()` offscreen).
- **The monitor thread never touches a widget.**
- **Test the RPM in a fresh `fedora:44` container**, never by installing it on this box.
- **A tapped chain ends in copy nodes**: a graph output port that also feeds a level tap makes the channel pass silence. See `docs/gui.md`.

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
- **ALSA apps (Audacity) are all named `PipeWire ALSA [<binary>]`.** The
  brackets made a remembered rule a glob that never matched its own app.
  `Node.app_name` unwraps it (`audacity.bin` -> `audacity`) and `Rule.matches`
  accepts an exact name before trying a glob.
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
- **A test `Engine.apply()` in the real runtime dir STOPS the user's channels**:
  every channel not in the test config is an orphan. Live experiments must set
  `XDG_RUNTIME_DIR=/run/user/1000/arlab PIPEWIRE_RUNTIME_DIR=/run/user/1000
  PULSE_RUNTIME_PATH=/run/user/1000/pulse`.
- **Never run `init` in the lab config.** It adds a Speakers channel with the
  same slug as the owner's, so the lab host takes the same node name and
  `apply` moves the owner's playing apps onto it; stopping it drops them onto
  the raw device. Done 14 Sep 2026 (Zen, sent back by hand). Add only
  `lab*` channels.
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
  for inputs. Recording apps are pointed at a channel's recording source by
  `Engine.send` (2 Oct 2026; `source_map`, rules with `record: true`, listed in
  Playing now as "Recording"); `sink_map` holds outputs only.
- **Every input channel has a hidden COMPANION output** (`<slug>_mix`,
  `Channel.companion_of`, made and kept in step by `Config.ensure_companions`;
  1 Oct 2026). The mic channel's filter-chain no longer publishes the virtual
  mic: its playback is a passive, dont-fallback STREAM into the companion's
  sink, and the companion (recordable, playing NOWHERE or into the `listen`
  channel) publishes the virtual mic under the OLD name `ar_<input slug>`, so
  apps keep their choice. Apps can be sent into a mic via the companion. An
  input's own node (`sink_node`, `_await_sink`, its trim volume) is now its
  CAPTURE `ar_<slug>_in`. Companions start before their mic (`start_order`)
  and are never strips or list entries. Lab (1 Oct): levels exact, 0 ms extra
  delay, the mic still sleeps until something records, and an upgrading mic
  channel hands its recorders to the companion with no gap. **The link must
  be passive on BOTH sides** or the mic runs all the time (measured).
- **Internal streams carry `node.dont-fallback`.** Measured without it: when
  the output a loopback played into vanished, WirePlumber relinked it onto the
  REAL SPEAKERS - for a listen-through, a live mic on the speakers.
- **`node.dont-fallback` on a channel's MAIN playback destroys the channel
  when its target is missing.** Tried for groups (1 Oct 2026): with the group
  off, filter-chain logged "defined target not found" and destroyed the
  member's sink, so its apps fell to the default output anyway. It is safe on
  internal loopbacks (listen-through), which is where it belongs.
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
- **On this box the login service runs the INSTALLED RPM (0.2.0), not this
  checkout** (`/usr/lib/systemd/user/audiorouter.service` -> `/usr/bin/audiorouter
  watch`). A code change is not live until you rebuild with
  `packaging/build-rpm.sh` and reinstall. Without the package, the service
  would run whatever branch is checked out. See `docs/packaging.md`.
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

## Remembering apps (owner ruling 2026-09-12)

**The first "Send to" for an app that no rule covers remembers it** on that
channel (`Engine.send(..., remember_new=True)`). A later "Send to" for an app
that already has a rule is a one-off and changes nothing saved. "Always send
this app here" (`Engine.remember_app`) UPDATES the app's existing rule rather
than appending one - rules are first-match-wins, so a second rule for the same
app would be listed and never take effect.


## State

Engine, CLI, GUI, routing, auto-router, launcher, login service, effects, input
channels, meters, echo cancellation and the RPM are built and verified against
live PipeWire. Login persistence across a real logout/login passed 15 Sep 2026.
VST is a future possibility only (see `docs/effects.md`).

## Done means
A change is not finished until all of these hold. Report any you could not do:
- `python -m unittest discover -s tests -t .` passes
- Audio changes are MEASURED in the lab runtime dir (see Traps), never judged by
  `pw-dump` readback or by ear alone
- GUI changes were rendered and looked at
- If the owner is to try it: the RPM was rebuilt and reinstalled, because the
  login service runs the package
