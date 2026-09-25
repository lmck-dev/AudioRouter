# Echo cancellation

Moved out of `CLAUDE.md` on 25 Sep 2026 so it loads only when the work touches it.
Read this file before changing the code it describes.

## Echo cancellation (0.2.0, 18 Sep 2026)

`Channel.echo_cancel` (inputs only; outputs are forced off): the channel's conf
gains `libpipewire-module-echo-cancel` as a SECOND module in the same process,
so it lives and dies with the channel and toggling it is an ordinary shape
change (restart). mic -> `ar_<slug>_ec_mic` (capture) -> WebRTC AEC ->
`ar_<slug>_ec` (source) -> the filter-chain's capture -> effects.

- **`monitor.mode = true`**: the reference is the DEFAULT OUTPUT's monitor
  (`ar_<slug>_ec_ref`), so no sink is created and nothing is re-routed. If the
  default output is itself one of our channels, only that channel is cancelled.
- NS and AGC are OFF in `AEC_ARGS`: the channel's own effects (RNNoise, a
  compressor) do that; doubling up fights them.
- `ar_<slug>_ec` has `priority.session = 1`. On the dev box the stored default
  source was a dead `easyeffects_source`, so any new source could have become
  the default mic; apps must pick the channel's own mic, which has the effects.
- All three canceller nodes carry the owner stamp (`is_app_stream` excludes them).
- The WebRTC library is in `pipewire-libs`: no new package dependency.
- Measured live 18 Sep: "Marvin" + speech from the speakers scored 0.99 for a
  wake-word detector on the raw mic and 0.00 on the echo-cancelled channel;
  whisper heard "[inaudible]" raw and "[Silence]" through the channel.
- Owner-tested on a real call with a remote listener on 20 Sep 2026: "very good".
