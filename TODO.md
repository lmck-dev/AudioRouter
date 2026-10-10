# Audio Router — ideas

Maybe-features, none started. Add new ideas here.

## Saved profiles (10 Oct 2026)

- **Per channel:** save a channel's effect chain and settings (inserts, their
  parameters, fader, pan, bypass) under a name, and load it onto any channel.
  E.g. a "Streaming voice" mic chain reused across mics.
- **Overall:** save the whole setup (channels, routing, groups, outputs,
  remembered apps) as a named profile and switch between them, e.g. "Gaming",
  "Teams call", "Recording".
- Open questions: what happens to a profile that names a device or app that
  is not present; whether the phone remote can switch profiles.

## To discuss: a real web server for the phone API (10 Oct 2026)

The phone API (`remote.py`) runs on the stdlib `http.server`, which Python's
own docs don't recommend for production. Suggested: move it to an open-source,
lightweight web server instead. Weigh the gain (hardening, TLS support) against
adding the first runtime dependency to the login service and the RPM. Decide
together with the planned TLS step.
