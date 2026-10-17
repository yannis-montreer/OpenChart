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

## The cut warmup is said (2026-10-17, third round)

At the owner's request. Before: the values route cut a declared warmup to `MAX_WARMUP` (20,000
bars) without a word, so the values could be computed on too little history unnoticed.

- **The route change is small:** `needed` is taken before the cut; if it is above the limit the
  answer gets `notice = {kind: "warmup_cut", needed, limit, message}` (one 12-line helper,
  `_warmup_cut`, and 6 lines in the handler). Custom files also get `checks.warmup_cut`, added to
  the answer's *copy* of `checks` (the results stored for the file's version are untouched; a test
  pins that). Absent when nothing is cut, so existing answers are unchanged. Contract: `docs/api.md`,
  "Warmup notice".
- **"Which length would be needed":** read as the number of bars the settings need (`needed`), for
  built-ins and files alike. A length that would fit is not computed: it would need the indicator's
  own formula (a search over its inputs, a worker call per probe for a file of yours), which is not
  a small change. The message says "use a shorter length".
- **The legend** shows a small "Warmup cut" warning (the message as its tip) beside the row's name,
  unless the row is in error (`chart/notice.ts`, taken from the first page of a full load; a tail
  refresh does not change it). This is the one frontend change of the PR.
- **Covers:** the built-in EMA above length 1999 (`10 * n + 10 > 20,000`), a custom file with a
  large `warmup`, and any built-in whose limits are raised later.
- **Tests:** `tests/test_warmup_notice.py` (3 failed first: the notice, the custom one, the stored
  checks), `frontend/src/chart/notice.test.ts` (failed first: no module). Mutation checks: 8 route
  mutants (boundary `<`, twice the cap, never set, not in `checks`, stored `checks` mutated in
  place, `needed` showing the cap, a message without the need, `needed` read after the cut) all
  killed; 6 on the frontend helper, 5 killed and one equivalent (an empty message is also dropped
  by the final check).

**Should the 900 cap go back toward 5000? Recommendation: keep 900 for now.** Measured on 135,000
synthetic gapped bars (the 20,000-bar warmup of the route against the full history), the largest
difference over the 5,000 bars shown: RSI length 900 `4e-10`, length 2000 `3e-5`, length 5000
`0.011` RSI points; ATR 5000 `5e-5` price units. So the cut is cheap in accuracy, and the case for
5000 is TradingView parity, not correctness. Reasons to stay at 900 anyway:
1. Up to 900 the values equal a full history to 1e-9 and the warning never shows; above about 950
   it shows on every row, for differences nobody can see (3e-5 at 2000), which teaches people to
   ignore it. The notice is for what we did not foresee (EMA, files), not a licence.
2. A length of more than 900 is not used in practice for these (a 900-bar RSI on M5 is three days).
3. Cost: a 5000-bar VWMA or WMA takes 0.7 s on 25,000 bars.
4. Raising a maximum later breaks nothing; lowering one later breaks saved charts.
If TradingView parity matters more, **raise to 2000** (not 5000): difference `3e-5`, and the
warning appears only above 952. Open for the owner.
