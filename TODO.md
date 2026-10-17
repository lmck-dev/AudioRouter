# Audio Router — ideas

Maybe-features, none started. Add new ideas here.

## Saved profiles

Built 16 Oct 2026 (1.4.0): whole-setup profiles and saved effect chains.
Still open: switching profiles from the phone remote.

## To discuss: a real web server for the phone API (10 Oct 2026)

The phone API (`remote.py`) runs on the stdlib `http.server`, which Python's
own docs don't recommend for production. Suggested: move it to an open-source,
lightweight web server instead. Weigh the gain (hardening, TLS support) against
adding the first runtime dependency to the login service and the RPM. Decide
together with the planned TLS step.
