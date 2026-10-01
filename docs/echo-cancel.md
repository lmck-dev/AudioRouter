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

## A lone WirePlumber restart breaks it (1 Oct 2026)

Measured on the owner's desk: `systemctl --user restart wireplumber` alone
leaves every canceller counting ~190 errors/s (pw-top ERR on `ar_<slug>_ec`),
because PipeWire then runs the mic one cycle late (pw-top WAIT 5.3 ms, healthy
~0.4 ms). Node props are identical before and after; links are correct.
**Restarting the channel, suspending the card, and toggling the card profile
off and on all left it broken** (the profile toggle also destroyed the
cancellers outright: their capture is dont-fallback, see CLAUDE.md, and
`apply` had to restart them). Only restarting PipeWire cures it. A private
PipeWire + WirePlumber (`wireplumber -p policy`, null devices, two clocks)
did NOT reproduce it: test on real hardware.

So `session.py` notices instead (owner ruling: notify with a Fix button,
never restart on its own): WirePlumber started > 10 s after PipeWire (from
`/proc/<pid>/stat`, no state kept) while an enabled input has echo cancel.
The login service sends one notification per WirePlumber pid (`notify-send
--action`, waiting on its own thread); the window shows a banner; `status`
prints a warning. The fix runs in a transient `systemd-run` unit, because our
service is `PartOf=pipewire` and would be killed mid-fix, then re-applies the
channels if the service is not running to do it.

Measured end to end on the desk (0.8.0, 1 Oct 2026): a lone WirePlumber
restart at 12:11:44 -> cancellers at ~190 errors/s -> the login service's
notification -> its button ran `audiorouter-sound-restart-*` at 12:11:51 ->
all three cancellers flat again (ERR 3). A notification left alone expires
without pressing its button ("Wait timeout expired" on stdout, which is not
"fix"), so nothing restarts unless someone clicks. No false alarm while healthy.
