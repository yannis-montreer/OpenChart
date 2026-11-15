# Decision: the Telegram "alert engine started" message

**Before:** sent at every server start (SPEC section 9, `docs/decisions.md` watchdog line).
A manual restart after a clean stop told the owner nothing they did not know.

**Now:** the setting `telegram-start-message` (`auto` default, `always`, `never`).
It is read at start, from Settings (Telegram) or `openchart settings set telegram-start-message <value>`.
The same parser serves both; in Settings it is part of `PUT /api/settings`, so it needs the
login and the same-origin check like the other settings there. A value stored by hand that is
none of the three reads as `auto`.

- `always`: the message exactly as before.
- `never`: nothing.
- `auto`: one message, with the reason in it, when
  1. the previous run did not stop cleanly ("the previous run did not stop cleanly (a crash,
     a power cut or a forced stop)"), or
  2. the engine was down for **more than 5 minutes** ("the engine was down for 20 min (from
     ... to ...)").
  A normal manual restart after a clean stop, a first start and the first start after this
  change (no marker yet: nothing is known, so nothing is claimed) send nothing.

## The marker: `run-state.json` in the data folder, not the database

`{"state": "running" | "clean", "ts": ...}`. At start the server reads it and writes
`running` (so the marker is cleared at start: a file left as `running` means no stop was
noted); `Server.stop` writes `clean` with the time the stop began, as its last step before
the data-folder lock is released. A file beside the database needs no migration and can be
written when the database itself is the problem. It is written only by the process that holds
the data-folder lock, so a second server refused by the lock never touches it.

I wrote `running` at start instead of deleting a `clean` file. Deleting would make "no file"
mean a crash, and then the first start after this upgrade (and any first start) would be called
a crash.

## Downtime: which time is used

Not `alerts_last_eval` (the engine's per-instrument last evaluated tick): it only moves with
ticks, so after every weekend or holiday it is days old and every Monday start would report a
"gap". The engine keeps no heartbeat. The clean-stop time in the marker is exactly when the
engine went down, so down time = start time - that time. After an unclean stop the downtime is
not known (the marker only has the start of that run), so the message does not give one.

## Stop paths

Ctrl+C (what the installer's NSSM `AppStopMethodConsole` sends), SIGTERM (`docker stop`) and
the desktop viewer's stop all end in uvicorn's graceful shutdown, which runs the app's
lifespan exit, `Server.stop`. `tests/test_start_message.py` starts `openchart run` as a real
process on a scratch folder (no credentials, so no broker or Telegram request), stops it with a
console Ctrl+C on Windows (as NSSM does, from a helper that attaches to its console) or SIGTERM
elsewhere, and checks the marker says `clean`; a hard kill leaves `running`. A kill during the
service's 20 s grace (NSSM then ends the process) is a forced stop and is reported next start.

## Not changed

Catch-up messages (late fires), freeze, watchdog (stalled, recovered), expiry and indicator
alert system messages, and the delivery of alerts: no code of theirs was touched.
`tests/test_start_message.py` runs a line alert and an indicator alert under all three
settings and compares what Telegram and the event log got.

## For the owner to fold in

- `docs/decisions.md` watchdog line: "engine started" on every start -> as above.
- `SPEC.md` section 9: "On server start, send an 'engine started' message."
- `docs/roadmap.md`: "Telegram start message only when it says something (PR description)".
