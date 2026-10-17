# Decisions: the built-in indicators (2026-10-17)

SMA, RSI, MACD, Bollinger Bands, ATR, Stochastic and Volume as files in
`src/openchart/indicators/`, written with the `ta` library (#19) and the output kinds (#21).
No frontend change: the menu lists whatever the registry holds, and the chart draws the kinds
(PR for step (d) of CI-2 is separate).

- **Ids and files:** `sma`, `rsi`, `macd`, `bollinger`, `atr`, `stochastic`, `volume`. A file
  of the owner's with one of these ids is a separate (custom) indicator, as for `ema`.
- **Inputs and defaults are TradingView's:** SMA 9; RSI 14; MACD 12/26/9; Bollinger 20 and
  2.0; ATR 14 with RMA; Stochastic 14/1/3 with levels 80 and 20; RSI levels 70 and 30. The
  levels are `Float` inputs (0 to 100), so they can be changed.
- **Left out on purpose:** the moving-average type of the MACD and the Bollinger basis
  (EMA and SMA only), the Bollinger `offset`, the volume's moving average, the middle level
  (50 on RSI, Stochastic). Each can be added later without changing an id.
- **ATR has a `smoothing` choice** (RMA, SMA, EMA, WMA), as TradingView's indicator; RMA is
  the default and is what `ta.atr` does.
- **Volume is tick volume:** OANDA gives no real volume. The output label is "Tick volume"
  and the title says "(tick volume)". A histogram has one colour in this version (a colour
  by direction needs per-bar colours: "Not supported yet" in `docs/custom-indicators.md`).
- **Maximum lengths:** 5000 for every length, as the EMA. The warmup grows with it
  (about 21 lengths for RSI and ATR with RMA), which is what a correct first value needs.
- **Warmups are declared per smoothing/lengths** and checked by the save-time check, run in
  the tests on 4,000 bars for every indicator and for non-default settings; the check runs both
  runs (neither is skipped) and finds nothing.
- **Reference tests:** plain loops over Python floats, from TradingView's definitions and not
  through `ta`. The test bars open away from the previous close, because with bars that open
  on it the true range is always `high - low` and an ATR that ignored gaps would pass.
- **Mutation check:** 21 mutants (warmup 0 or too short, a shifted output, a changed default,
  swapped inputs or outputs, a wrong fill, a level wired to the wrong input, a plain range
  instead of the true range, an unsmoothed %K, a volume label without "tick", ...): all
  killed by `tests/test_builtin_indicators.py`. The first run of the ATR mutant survived: the
  test bars had no gaps; fixed by `gapped()` in the tests, then killed.
- **Not checked in TradingView:** the values were compared with references written from its
  documentation, not with a TradingView chart. Worth one look at RSI and ATR on a flat
  stretch and at the Stochastic when high equals low (a gap here; TradingView shows 0 or NaN).
