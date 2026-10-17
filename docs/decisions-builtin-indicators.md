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
- **Left out on purpose (first version):** the Bollinger `offset`, and a histogram coloured
  by sign. The MA types, the middle levels and the volume average were left out at first and
  added at the owner's request: see "Second round" below.
- **ATR has a `smoothing` choice** (RMA, SMA, EMA, WMA), as TradingView's indicator; RMA is
  the default and is what `ta.atr` does.
- **Volume is tick volume:** OANDA gives no real volume. The output label is "Tick volume"
  and the title says "(tick volume)". A histogram has one colour in this version (a colour
  by direction needs per-bar colours: "Not supported yet" in `docs/custom-indicators.md`).
- **Maximum lengths:** first 5000 for every length, as the EMA; see "Adversarial review" for
  why RSI, MACD, Bollinger and ATR now stop at 900.
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

## Second round (2026-10-17): options the first version left out

At the owner's request, as further commits on the same branch. **Defaults leave every value
unchanged:** `tests/test_builtin_defaults.py` compares every output of every built-in with default
inputs to values written from the first version (`tests/golden/builtin_defaults.json`, 300 gapped
bars, to 1e-12 relative: numpy's summation order may differ by a bit between platforms, so not
bit for bit). The golden file was written before any option existed.

- **MACD:** `osc_ma` and `signal_ma`, each EMA (default) or SMA, as TradingView's "oscillator MA
  type" and "signal line MA type". Only these two: they are the types TradingView offers there.
- **Bollinger:** `ma_type` SMA (default), EMA, RMA, WMA or VWMA. **Offered: all five**, because
  `ta.sma`, `ta.ema`, `ta.rma`, `ta.wma` and `ta.vwma` all exist. VWMA weights by tick volume
  and is a gap where a whole window has no volume. The band width is the population deviation
  about the window's simple mean whatever the basis, as TradingView's `ta.bb`/Bollinger does.
- **Middle level:** RSI and Stochastic get a `middle` input (50), a third dashed level, declared
  **last** among the outputs so the ids of the existing levels do not move.
- **Volume average:** `show_ma` (off) and `ma_length` (20), a simple average of the tick volume
  in a second output, "Tick volume MA". Outputs are static, so the line is always declared; with
  `show_ma` off it is all gaps and the warmup is 0. A missing volume is a gap in the average, not
  a zero.
- **Warmups for the options:** MACD `11 * slow` for an EMA oscillator (the window for SMA) plus
  `11 * signal` for an EMA signal (the window for SMA); Bollinger `n` for SMA/WMA/VWMA,
  `11 * n + 10` for EMA, `21 * n + 10` for RMA; volume `ma_length` when shown. The save-time
  checks run for each new setting (every MA type, both MACD types, the average on and with a
  200-bar length).
- **Mutation check:** 29 mutants on the options (types ignored or swapped, defaults changed, a
  warmup that ignores the fade, a level wired or styled wrong, the middle level placed first, an
  unimplemented type offered, a lookahead in VWMA, a 1e-7 error in the band width, ...): all
  killed.

## Adversarial review of the seven (2026-10-17)

Probed with: empty, 1, 2 and 3 bars; NaN-only prices; constant prices; high equal to low; zero and
NaN volume; integer columns; infinities and 1e200 prices; zero prices; high below low; a length
longer than the data; lengths of 1; the longest settings; a gap in the middle (it heals); timing on
25,000 bars. Tests for all of it are in `tests/test_builtin_indicators.py`.

- **Finding 1 (fixed, test first):** RSI, MACD, Bollinger (EMA/RMA) and ATR (RMA/EMA) accepted
  lengths up to 5000, whose settled warmup is 55,000 to 110,000 bars, but the values route
  fetches at most `MAX_WARMUP` (20,000) bars before the first one shown and cuts the declared
  warmup to it without a word: a long RSI started unsettled at the left edge. The test
  `test_no_declared_warmup_is_beyond_the_fetch_cap` failed for four indicators; the lengths of
  RSI, MACD (fast, slow, signal), Bollinger and ATR now stop at 900 (`11 * (900 + 900)` and
  `21 * 900 + 10` fit). SMA keeps 5000: its warmup is its window. **Open question:** TradingView
  allows 5000; a longer RSI is not useful in practice, and the alternative (a notice when the
  route cuts a warmup) is a change to the route.
- **Same cause, same result in speed:** a 5000-bar VWMA took 0.7 s on 25,000 bars; at 900 it is
  about a tenth of that.
- **Not mine, noted:** the built-in EMA (not one of the seven) has the same unreachable warmup
  above length 1999 (`10 * n + 10`); a prices array with an infinity or 1e200 makes
  `ta.stdev` raise numpy `RuntimeWarning`s (the values come out as gaps or infinities, which the
  result check treats as gaps); OANDA sends neither.
- **Found sound:** no indicator raised on any input above; empty or too-short input gives gaps of
  the right length; constant prices give RSI 100 (no loss), a 0 ATR and bands that close on the
  basis; a flat Stochastic window is a gap; zero volume gives a zero histogram and no VWMA;
  integer columns give the same values.
- **Known and left:** bars with `high < low` can put %K outside 0 to 100 (no such bars from
  OANDA); TradingView does the same.
- **Mutation check of the review:** 11 mutants (gaps filled with 0 or 50 in five indicators, the
  maximums put back to 5000 in five places, a volume average that reads a missing volume as
  zero): all killed; the last one survived at first and got its own test.
