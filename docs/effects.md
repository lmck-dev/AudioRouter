# Effects, LV2 plugins and noise suppression

Moved out of `CLAUDE.md` on 25 Sep 2026 so it loads only when the work touches it.
Read this file before changing the code it describes.

## Effect and plugin traps

- **The LV2 loader is a separate package** (`pipewire-module-filter-chain-lv2`,
  installed on this box 13/09/2026). Without it no LV2 effect can run;
  `plugins.py` probes for it and the engine degrades with an installable message
  instead of a crash.
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

## Plugin folders and LADSPA (2 Oct 2026)

Owner: "the ability to add more effects in the LV2 and LADSPA formats" (no VST).
`Config.plugin_folders` (config.json, because the login service starts the
hosts) -> `plugins.set_extra_folders`, called by every `Engine` on load and
reload. Each folder is walked to depth 5 (at most 5000 `.so`): directories
holding `*.lv2` bundles join `lv2.search_path()`, and every other `.so` (never
one inside a bundle) is a LADSPA candidate. A channel host gets `LV2_PATH` =
the whole search path only when the user's folders add something
(`lv2.host_lv2_path`); LADSPA needs nothing, because filter-chain loads an
absolute `plugin` path as given.

- **LADSPA plugins live in the same catalogue as LV2**, as `lv2.Plugin`s with
  uri `ladspa:<file>#<label>` and `maker`; effect kind stays `"lv2"` (the
  plugin kind) and `_lv2_node` renders `{"type": "ladspa", "plugin": <file>,
  "label": ...}`. **Controls are keyed by PORT NAME** - that is how
  filter-chain addresses LADSPA controls.
- **Reading a LADSPA file runs its code** (`ladspa_descriptor`), so it happens
  in a child (`python -m audiorouter.ladspa FILE...`). A batch that crashes is
  retried file by file and the bad one listed in `unreadable`.
- `LADSPA_PATH`, when set, REPLACES the standard folders (as `LV2_PATH` does);
  tests set both to keep this machine's plugins out.
- The same LADSPA plugin in two folders is listed once (label + name, first
  wins), like LV2's first-on-the-path rule.
- Measured in the lab (2 Oct 2026): a hand-built LV2 gain (`urn:lab:gain`) and
  a copy of LADSPA `amp.so`, both only in a user folder, both at 0.5: each
  channel measured -6.02 dB against the reference.

Plugins with 1 in / 2 out (wideners), several ins/outs, or instruments are
listed as unusable with the reason. Plugin windows (LV2 UIs) are not shown.
