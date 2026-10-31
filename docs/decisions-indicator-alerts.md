# Indicator alerts: decisions to make

For `docs/designs/indicator-alerts.md`. Each has a recommended default; nothing is built.

- **D1 Worker priority of alert jobs.** Default: a level between chart requests and the
  save-time checks (charts first, alerts next, checks last). Alternative: alerts ahead of
  charts, since the server exists to alert when no chart is open; a slow script would then
  delay every chart at each bar close.
- **D2 Instance removed from its chart.** Default: the alert is stopped with a reason and
  kept. Alternative: deleted with the instance, as line alerts are with their line.
- **D3 Timeframes.** Default: 1m and above only; 1s to 30s bars come from live ticks and a
  missed period cannot be replayed. Revisit with the 5-second candles milestone.
- **D4 Replay of a script flagged for lookahead.** Default: one computation, as for clean
  scripts, with the warning saying replay may differ. Alternative: one computation per
  replayed bar (exact, up to thousands of worker jobs after a long sleep).
- **D5 A bar replaced by the broker's candle after it was evaluated.** Default: not
  evaluated again. Alternative: delay each evaluation until the broker's minute arrives
  (alerts up to about a minute later).
- **D6 Custom file edited while an alert uses it.** Default: re-arm when the output still
  exists, stop otherwise. Alternative: always stop and ask the owner to re-enable.
- **D7 Wording and limits.** Default: "behind" after 2 bars (at least 5 min); catch-up
  cap 7 days, as for lines.
