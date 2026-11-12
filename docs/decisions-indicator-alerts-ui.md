# Indicator alerts, step IA-4: the frontend (decisions)

Follows `docs/decisions-indicator-alert-gaps.md` (#67). Frontend only: the engine, the API
contract and the migrations are as #67 left them; nothing in this step needed a backend change.
Tests: the pure logic in `frontend/src/alerts/` (`indicatorAlerts.ts`, `confirmations.ts`), the
screens in `frontend/src/ui/` (component tests). Mutation check:
`node tools/mutate_indicator_alerts_ui.mjs` (39 mutants: 38 killed, 1 equivalent, explained in
the tool). The roadmap line is in the PR description.

## The dialog

- **One dialog for the three conditions**, opened from the bell on an indicator's legend row and
  from the Alerts panel's `+` (which first asks which of the chart's indicators). Output choices
  follow the condition (a crossing offers lines, histograms and areas; a signal offers markers
  and backgrounds; the second output also offers levels), so a person cannot pick a pair the
  server would refuse. The same rules are checked again before sending (`validateForm`), in
  words, and the server's answer is shown in words (`problemText`): a 409 says the indicator
  cannot be read now and keeps the server's reason.
- **"Closed bars" is on the dialog** ("Evaluated on closed 15m bars only: a signal arrives after
  the bar closes, never during it"), with the alert's timeframe. A chart on seconds uses 1m, as
  D3 says, and the dialog's chip shows it.
- **Warnings are shown, never blocking.** The server returns them with the saved alert, so a new
  alert's dialog stays open once with the warnings and "Done"; an alert that is edited shows its
  warnings when it opens. The panel shows each as a "warning" chip (the words are the tooltip).
- **Trigger offers "Once only" and "Every time it happens"** (`once` and `once_per_bar`: on closed
  bars the second is every qualifying bar). `repeating` with a cooldown is not offered: with one
  evaluation per closed bar it would only be a slower `once_per_bar`.
- **The message follows the condition.** The server never renames an alert on an edit, so an
  alert whose name the server made kept saying "crossing down 30" after the number became 35.
  When the name is still the one the server would have made (or empty), the dialog sends a new one
  made the same way (`defaultName`, `nameToSend`); a name the person wrote stays. A new alert
  with no message is left to the server.

## The panel and the legend

- **States:** Armed; Arming (before the first evaluation, which never fires); Stopped, with the
  server's `stop_reason` ("stopped by you", "the indicator was removed from the chart", "the layout
  ... does not have its indicator", "its output ... no longer exists"); Error, with `state.error`;
  Triggered; Expired. An error is shown over armed; a stopped alert shows its stop, not an old
  error. "Behind" and warnings are flags next to the state. Late fires are labelled in the log
  with the cause, `worker` included ("after the indicator could not be computed").
- **Stop, restart, edit, delete** as for line alerts. A refused call (restart of an alert whose
  instance is gone is a 400) is a toast with the server's words; it was a browser alert box.
- **The legend bell** shows how many alerts the instance has (a count badge, lit while one is
  active, dimmed when none is). It is outside the legend's hover-only actions so the count stays
  visible. With alerts, the bell opens a short list (each alert and its state) and "New alert on
  this indicator..."; without, it opens the dialog. The count follows the panel's list, which the
  websocket keeps current.

## Confirmations

- **Applying a layout** asks the server first (`GET .../apply-preview`, which changes nothing),
  before the layout is taken or the chart is touched. Nothing stopping: no question. Some: a
  dialog lists them ("stops 2 indicator alerts; they stay in the Alerts panel, stopped, with their
  settings"), with Cancel focused and the button saying "Apply and stop 2 alerts". Cancel leaves the
  chart, the preferences and the alerts as they were. The ids sent as `stop_alerts` are exactly
  the previewed ones; if the alerts changed meanwhile, the server's 409 is shown as the failure
  (the chart's own list is read again), and loading the layout again asks again.
- **Deleting a definition** lists the alerts on it (any status, with the instrument), says how
  many running ones stop, and names every listed alert in the delete (`alerts=`). If an alert
  appeared in between, the 409 carries a fresh list: it is shown and asked again, nothing is
  deleted on the old answer. The button says "Delete and stop N alerts". This is what "needs them
  named" means in the screen: the list the person saw is the list sent.
- **Removing an indicator from a chart** has no server question (a chart write stops the alerts
  without one, D2), so the dialog is built from the panel's list, which the websocket keeps
  current: it names the running alerts on that instance and asks only when there are some. A removal
  from another browser, or a layout, is covered by the server's own answers above and by the
  alert's stop reason afterwards.
- **"Delete data..."** already counted the indicator alerts in `alerts` and in its token (#62
  and step 1); the dialog now says how many of them are on indicators ("5 alerts (2 on
  indicators)"), before and after the box is ticked.

## Telegram and the log

- The panel's log shows an indicator fire's `message` as the server sent it (what Telegram got,
  with its late line), and a correction note when the broker's candles later contradicted it. The
  toast on a fire uses the same text. Line and level events are unchanged. Delivery is not
  touched.

## Tooling

- **Component tests use happy-dom** (`happy-dom` 21.0.4, a dev dependency, pinned exactly like the
  others). Only the files that need a DOM say so (`// @vitest-environment happy-dom`); every
  other test still runs in the node environment. It is a devDependency only: nothing is added
  to the build.
- **`request()` takes a list** for a parameter (one `k=v` per item), for `stop_alerts` and
  `alerts`.

## Not done, or not verified

- A late fire and an alert with a lookahead warning are shown from scratch data (an event row
  and a state put into the scratch database); the engine's own late fires are tested by the
  backend suite, not by this pass. The error state is the server's own: a stored condition
  naming an output the indicator does not have, found by the server on start.
- The layout dialog asks before `takeLayout`, so a layout that is loaded at start (the default
  layout) is not asked: it does not apply to the chart, only to the preferences.
- Nothing here changes how an edit of the indicator's settings re-arms its alerts (D6): the
  legend does not warn before a settings change that re-arms.
