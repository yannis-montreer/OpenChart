# Decisions: the feed survives OANDA rejecting the credentials for a while

Branch `feed-auth-retry`. Code: `market/rejections.py`, `Server._run_feed`, `_rejected`,
`_on_stream_state`, `AlertService.watch`. Tests: `tests/test_feed_rejections.py`,
`frontend/src/feedStatus.test.ts`.

## What happened

On 15 November 2026 the feed stopped with "OANDA rejected the credentials: Insufficient
authorization to perform request" while the token was valid: OANDA itself was down. A
401/403 raised `AuthError`, and `Server._run_feed` returned for good on it (other errors
are retried every 30 s). No alert was evaluated until the server was restarted by hand.

## Decisions

1. **A rejection is first taken as possibly passing.** The feed retries after 30 s, doubling
   to 10 min (30, 60, 120, 240, 480, then 600 s). One delay per attempt; an attempt stops
   at the first refused request (two or three requests), so this stays far under OANDA's
   rate limits (120 requests a second, 2 new connections a second) and never hammers it.
2. **Jitter: each delay is shortened by up to a fifth, at random, never lengthened.** So no
   delay is over 10 minutes and none under 24 s, and two servers never retry in step.
3. **Bound: 6 hours from the first rejection.** OANDA's maintenance windows and outages
   have lasted minutes to a few hours; 6 hours covers them, and a really revoked token is
   still reported the same day (40 to 49 attempts in all, depending on the jitter). Then the feed stops as before,
   with the same error in the status (`error`).
4. **Revoked or not: OANDA does not tell.** Its 401 answer is the same text for a revoked,
   expired or wrong token and for its own outage (the 15 November answer was exactly
   that). So only what is certain stops at once: `AuthError(certain=True)` for a missing
   token or account, or a token that cannot be one (spaces, control characters: checked
   before any request). These stop as before, with no Telegram message, as before.
5. **Telegram: one message down, one up, none per retry.** "⚠️ Price feed down" at the first
   rejection (says the token may still be valid and that the server keeps retrying);
   "✅ Price feed recovered" when the stream connects again (with how long it lasted);
   or, after 6 hours, "⛔ Price feed stopped" (it says to check the token in Settings).
   The stop message is new: the owner was told it keeps retrying, so is told when it stops.
6. **Recovery is the stream connecting** (`StreamState.CONNECTED`), the same signal that
   already clears `error`. The history refill, the repair and the alert catch-up are the
   ones every outage already gets: the next tick reveals the gap, the missed minutes are
   repaired, and line, level and indicator alerts replay them, fires marked late
   (`reconnect`). Nothing in the alert engines changed. The test runs the same outage
   once as rejected credentials and once as a network failure (the unchanged path) and
   gets the same events, field for field.
7. **The engine and the watchdog keep running.** The watchdog's stall message ("no XAU_USD
   price for ...") is not sent while the feed is known to be down for this reason: it
   would repeat the down message once per instrument. Its timers restart instead, so
   prices get the usual 5 minutes once the feed is back. Indicator alert messages
   (`behind`, errors) are unchanged.
8. **One outage across a settings change.** Saving a new token rebuilds the feed and keeps
   the outage (its start, its 6 hours): the recovered message still comes when the new
   feed connects. Removing the credentials ends it silently (nothing to recover).
9. **Status, no new screen.** `/api/status` has `retry` (`reason`, `since`, `attempts`,
   `next_attempt`, `next_attempt_in`, `gives_up_at`; null otherwise) and `provider_state`
   `retrying` while waiting. The connection dot shows "Feed down, retrying" in red, and its
   tooltip the error, the next attempt and when it stops.
