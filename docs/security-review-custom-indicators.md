# Security review: the custom-indicator path

Read-only review, 2026-10-16, of the code at `11e3d6a` (branch `ci-3-chart-indicators`:
main with CI-1, CI-2 (a) to (c) and the CI-3 backend). No code was changed for it,
except where the finding was in CI-3's own diff (noted). Each open finding has a test in
`tests/test_security_review.py` marked `xfail(strict=True)`: it fails today, and the
suite goes red as soon as the finding is fixed, so the marker and this entry go then.

**Scope:** the worker's launch and protocol, the D2 editing gate, the API (catalog,
values, charts' indicators, the import, where-used and delete), file handling (the
indicators folder, `.history`, `.deleted`).
**Not covered:** the AppContainer and the Docker sidecar at run time (not available on
this PC; CI runs their tests), Linux and macOS, the browser editor (CI-4, not built), CPU
and memory exhaustion inside the worker (bounded by the job object and `RLIMIT_*`, tested
in CI-1).

**Severity:** *medium*: lets someone do what the design says they cannot, under
conditions that occur in normal use; *low*: a failure that is bounded (a 500, a timeout,
memory), or needs another finding first; *info*: not exploitable today, but something
the next step (CI-4) must not make worse.

| | Severity | Finding | Proof |
|---|---|---|---|
| S1 | medium | `auto` editing trusts any loopback peer without proxy headers | **fixed** (hardening-1) |
| S2 | low | a deeply nested header from the worker kills the server's reader thread | **fixed** (hardening-1) |
| S3 | low | non-finite or huge numbers in `params` are a 500 | **fixed** (hardening-1) |
| S4 | low | the server reads up to 256 MB of arrays per answer before checking them | documented (for CI-5) |
| S5 | info | `$` in the id patterns accepts a trailing newline | **fixed** (hardening-1) |
| S6 | low | a script can write frames to the protocol stream; the next request gets them | documented (for CI-4); test xfail |
| S7 | info | definition files are read through links; a syntax error echoes a line | documented (for CI-4) |
| S8 | info | no probe that the worker cannot write into the indicators folder | documented |
| S9 | info | Windows: `mine` finds `Mine.py` | documented (for CI-4) |
| C1 to C5 | fixed | found in CI-3's own diff (races, lax types, the import, the NaN 500) | tests |

## Findings

### S1 (medium): `auto` trusts any loopback peer that carries no proxy header

`api/editing.py` (`decide`, line 82) allows editing under `auto` when the TCP peer is
loopback and none of `Forwarded`, `X-Forwarded-For`, `X-Real-IP`,
`Tailscale-User-Login` is present. A local forwarder that adds none of them makes a
remote browser's request arrive from 127.0.0.1: an SSH tunnel (`ssh -L 8000:...`),
`socat`, `netsh interface portproxy`, some tunnel agents. Whether `tailscale serve`
adds `X-Forwarded-For`, and what Funnel requests carry, was **not verified** (no
network requests in this review); the gate relies on `Tailscale-User-Login`, which
Funnel requests from the internet do not have.

- **Impact:** a remote logged-in browser, or a stolen session cookie, can edit and run
  custom code. The code still runs in the worker, and editing needs the minimum
  isolation (AppContainer or sidecar), so the damage is bounded by the sandbox. This is
  the case the gate exists for, hence medium.
- **Proof:** `test_s1_auto_refuses_a_loopback_peer_that_names_another_host`: peer
  `127.0.0.1`, no proxy header, `Host: myhost.tail1234.ts.net` (or an IP, or another
  name): allowed today.
- **Suggested fix:** under `auto`, also require the `Host` header to name the loopback
  (`127.0.0.1`, `localhost`, `[::1]`, any port). A forwarder keeps the Host the remote
  browser asked for. It also closes DNS rebinding for these routes (a rebinding page
  sends its own name as Host), which today only the session cookie stops. Document that
  `auto` assumes no local forwarder, and that `behind-proxy on` is the switch.

### S2 (low): a deeply nested header from the worker kills the reader thread

`protocol.parse_header` (line 51) catches `UnicodeDecodeError` and `ValueError`; for a
header like `[[[[...]]]]` 100,000 deep (200 KB, under `MAX_HEADER`), `json.loads`
raises `RecursionError`, which is neither. The runner's reader thread
(`runner._read_frames`, line 587) does not catch it either, so the thread ends without
queuing its end-of-stream marker: the job waits for its deadline and is reported as a
**timeout**, which counts toward the definition's quarantine, and the worker is
restarted. Any script can send such a header through S6.

- **Impact:** a delay of one deadline (5 s by default) per job, and a worker restart; the
  definition that does it is quarantined after 3. Bounded.
- **Proof:** `test_s2_a_deeply_nested_header_is_a_protocol_error` (RecursionError) and
  `test_s2_a_worker_sending_a_deep_header_fails_the_job_at_once` (fake worker
  `DEEPHEADER`: today `timed out after 3 s`).
- **Suggested fix:** catch `RecursionError` in `parse_header` and raise `ProtocolError`
  (the reader then kills the worker at once, as for any bad frame), or refuse headers
  nested deeper than the protocol needs (2 levels).

### S3 (low): non-finite or huge numbers in `params` are a 500

`GET /api/indicators/{id}?params=...` parses `params` with `json.loads`, which accepts
`NaN`, `Infinity` and integers of thousands of digits. `Param.check` (`_base.py`)
converts with `int(value)` (line 102: `OverflowError` for an infinity) and
`float(value)` (line 108: `OverflowError` for `10**400`); `normalize` and the route
(`routes_indicators.py` line 101) catch `ValueError` only, so the request is a 500
instead of a 400. The worker's own `normalize` has the same gap (reported there as the
script's error).

- **Impact:** a 500 for a malformed request from a logged-in client; nothing else.
- **Proof:** `test_s3_infinity_in_the_values_params_is_a_400`,
  `test_s3_a_huge_number_for_a_float_input_is_refused_with_a_message`.
- **Suggested fix:** in `Param.check`, refuse non-finite numbers for `int` and `width`,
  and catch `OverflowError` as "must be a number"; or parse `params` with
  `parse_constant` that refuses `NaN`/`Infinity`.
- **Fixed in CI-3's diff (C5):** the same class for JSON bodies: FastAPI's 422 echoes
  the input, and a NaN in it could not be encoded, so a refused body holding one was a
  500 on every route (drawings, alerts, layouts, charts).

### S4 (low): the server reads up to 256 MB of arrays per answer before checking them

`protocol.MAX_ARRAYS` is 256 MiB for all arrays of one frame (line 23). The server's
reader takes the counts from the worker's header and reads them all
(`read_frame`: `np.frombuffer(read_exact(8 * n))`); `exact_reader` collects into a
`bytearray` and copies it to `bytes`, so about twice that is held for a moment. Only
then does `Indicator.check_wire` refuse arrays of the wrong length. A script returning
20 million values (160 MB, which fits in the worker's 512 MB with its copies) makes the
server hold about 320 MB for that request (estimated from the code, not measured); on
a forming bar that is a new run every second (the error cache keys on the bars).

- **Impact:** memory pressure on the owner's PC while such a script is on a chart.
- **Proof:** code reading (the lines above). The fake worker's `BAD huge` (3 million
  values, 24 MB) shows the path; a 256 MB variant was not run.
- **Suggested fix:** the runner knows the request's bar count and the definition's
  output count: refuse a header whose listed arrays are not exactly those (`n` values
  each) before reading any of them, and read into a preallocated array.

### S5 (info): `$` in the id patterns accepts a trailing newline

`_custom.ID` (line 51) and `_registry._ID` (line 25) are `^[a-z][a-z0-9_]{0,39}$`, used
with `match`: in Python `$` also matches before a final newline, so `"mine\n"` is an
id. A file `mine\n.py` cannot exist on Windows; on Linux it would be listed with the id
`mine\n`, and `CustomIndicators.get("mine\n")` would look for it.

- **Proof:** `test_s5_a_custom_id_with_a_trailing_newline_is_not_an_id`.
- **Suggested fix:** `fullmatch` with the pattern without anchors (as CI-3's delete
  route does), or `\Z`.

### S6 (low): a script can write frames to the protocol stream

The worker sends its answers on stdout and points `sys.stdout` at stderr so `print()`
cannot corrupt them (`child.py` line 84), but `sys.__stdout__`, and file descriptor 1
through `os.write`, still reach the protocol stream; the audit hook does not cover
them. A script can write a well-formed frame during its own job: the server reads it as
that job's answer, the real answer then waits in the pipe, and **the next request gets
it**, whichever script that request is for. The frames carry no request id.

- **Impact:** one script can answer for another script's request (its values, a
  `describe`, a warmup) in the same worker, and cause S2. The server still checks every
  answer against the definition it holds, so nothing reaches past the worker. Scripts
  in one worker share a process anyway (one can patch `numpy`, `pandas` or the helpers
  for the others), so separation between scripts is not a boundary today; still, a
  chart can show another indicator's values with no error. Low.
- **Proof:** `test_s6_a_script_cannot_answer_for_the_next_request`, with the real
  worker (restricted token on this PC): script B gets script A's closes instead of its
  own doubled closes.
- **Suggested fix:** a request id in every request, echoed in the answer and checked by
  the runner (a mismatch is a protocol error: kill and restart); and before arming,
  move the protocol to a duplicated descriptor and point descriptor 1 at stderr
  (`os.dup2`), so a script's writes to stdout land in the log.

### S7 (info): definition files are read through links, and a syntax error echoes a line

`CustomIndicators._files` lists `*.py` in the folder and `_update` reads each with
`read_bytes`, following symbolic links (and hard links are the file itself). The
source goes to the worker; if it is not Python, the `SyntaxError` comes back with the
offending line (`child.script_error`, line 267: `exc.text`), shown in the catalog's
`reason` and in the values' 422. So a link `x.py` to another file the **server** can
read (for example `secrets.enc`, or a `.env`) would show a line of it in the browser.

- **Precondition:** writing into the indicators folder, which today means access to the
  server's disk, more than the gate protects. Hence info.
- **Proof:** code reading (`_custom.py` lines 177 and 222, `child.py` line 267).
- **For CI-4:** Import, New, Duplicate and Restore must create regular files only
  (exclusive create, no link following), and listing should skip links and files with
  more than one link, or at least never send their content.

### S8 (info): no probe that the worker cannot write into the indicators folder

`isolation.REQUIRED` checks reading the database, `secrets.enc` and the key file,
listing the data folder, opening the server process and the network. Writing into
`<data dir>/indicators/` (and `.history`, `.deleted`) is not probed. The AppContainer
has no grant on the data folder, so it is very likely refused; the sidecar does not
mount it. But the minimum is not proven for the one folder CI-4 turns into the
browser's code store, where a written file would be run by every later worker and shown
to the owner as theirs.

- **Proof:** code reading (`isolation.py` lines 358 to 391 and `REQUIRED`).
- **Suggested fix:** a write probe into the indicators folder (a file name that cannot
  be an id, removed by the server if it appears), required for the minimum.

### S9 (info): on Windows, the id `mine` finds `Mine.py`

File names are case-insensitive on Windows. The catalog lists `Mine.py` as `Mine`
(status `error`: not an id), but `CustomIndicators.get("mine")` checks
`folder / "mine.py"`, which exists, and describes and runs `Mine.py` as `mine`; a chart
instance or a delete of `mine` reaches it too. No privilege is gained (it is the
owner's file); the catalog and the charts disagree on what exists.

- **Proof:** code reading (`_custom.py` lines 189 to 197, `Path.is_file` on NTFS).
- **For CI-4:** create files with lowercase names only, and have `get` compare the
  listed name exactly.

## Found in CI-3's own diff, fixed there

Each has its test in `tests/test_chart_indicators.py` (section "Found in the review of
this change"), shown failing on `43ead31` first; details in `docs/decisions.md`.

- **C1** a layout saved during a delete kept the deleted definition (layout writes now
  take the charts' lock).
- **C2** two deletes at once: the second was a 500 and left an empty folder in
  `.deleted` (the file is looked up again under the lock).
- **C3** `revision: true` was read as 1; `visible: "no"` as false (strict types).
- **C4** the import refused a whole old list for one entry outside the bounds (now
  entry by entry, the first 30 kept).
- **C5** a refused body holding NaN was a 500 on every route (the 422 handler sends
  such numbers as text).

## Reviewed without a finding

- **Worker launch (Windows):** the process is created suspended, put in its job
  (one process, memory, CPU rate, UI limits, kill on close) and only then resumed; only
  its three pipe handles are inherited (`PROC_THREAD_ATTRIBUTE_HANDLE_LIST`); the
  environment is built from nothing (no `OPENCHART_*`, no tokens); the command line names
  no data path. A service gets the AppContainer or nothing, never a fallback (tested in
  CI-1). The AppContainer's own folder is writable and shared by the scripts of one
  worker (CI-1's B1: emptied at each start, capped at 50 MB); the worker runs with `-I`
  or as the frozen build, so nothing left there is imported.
- **Probes** run before any custom code is sent, so a script cannot fake them.
- **The editing gate,** apart from S1: an unreadable setting counts as `off`; below the
  minimum nothing turns it on; no route changes the setting; usage and delete check the
  gate before looking at the file (so a 404 says nothing to a request that may not edit).
- **The chart routes and the import:** login and the same-origin check; values bounded
  (30 instances, 64 values, 8 KB, finite numbers, 1000-character text); stored values
  are never run by the server and reach the worker only through the values route's own
  `normalize`; the import goes only onto a chart whose list was never set.
- **Delete:** ids by `fullmatch`; the file is moved, never erased; `.deleted` and
  `.history` must be plain folders inside the indicators folder (a file, a link or a
  junction elsewhere is a 409 with no change); instances are removed under the lock
  before the files move. Residual: a change to those folders between the check and the
  move (the owner's own folder; info). `.deleted` is not size-capped and is never loaded
  (the listing is not recursive).
- **The browser:** no `innerHTML` in `frontend/src`, so a script's error text is shown
  as text.

## After the review: what was fixed, what is documented only (2026-10-17)

The owner's decisions on the plan. **Fixed** in branch `hardening-1`, each with a test that
failed first (`tests/test_hardening.py`, `tests/test_editing.py`, and the plain tests above
that were `xfail` before):

- **S1:** `auto` never allows browser editing; `on`, set on the server's machine with
  `openchart settings set indicator-editing on`, is required, and still needs the minimum
  isolation. Where a request comes from no longer plays a part. The catalog's `editing`
  says so, with the command. `behind-proxy` is kept but changes nothing.
- **S2:** a header nested too deeply is a protocol error; the reader kills the worker at
  once and the job fails at once as `protocol` (no wait for the deadline). It counts toward
  the quarantine only when no other script had run in that worker, so a script that breaks
  the stream after its own answer, during another script's job, cannot get that other
  definition quarantined; one that breaks it in its own job is quarantined after three, as
  before (each bad frame replaces the worker, so its next try runs alone).
- **S3:** NaN, infinities and integers too large for a float are refused by the input with
  its message (400), not a 500. With Session A's finding: `from`, `to` (at most 2**53 either
  way) and `limit` (at most 2**31) out of range are a 422 on `/api/candles` and the values
  route, not a 500.
- **S5:** the id patterns end in `\Z`, so a trailing newline is not part of an id.

**Documented only**, with the reason:

- **S4 (memory per answer):** the fix belongs with CI-5's runner work: refuse a header whose
  arrays are not exactly the request's bars times its outputs, before reading any of them.
- **S6 (frames written by a script), in plain words: your custom indicators are not
  isolated from each other.** They run in one worker process; one of them can change what
  another shows, slow it down or make it fail. The isolation is between them and your data,
  tokens and network. Request ids on the protocol (CI-4) will at least make a forged answer
  fail loudly; separate workers per indicator would cost a worker each and are not planned.
- **S7 (links), S9 (case):** constraints for CI-4's write endpoints, listed under CI-4 in
  `docs/roadmap.md`: plain files only, created from the id in lowercase, compared exactly.
- **S8 (no write probe on the indicators folder):** the AppContainer has no grant there and
  the sidecar does not mount it; a probe needs the AppContainer and sidecar CI runs to
  prove it, so it goes with CI-4, when the folder becomes the editor's store.
- **Session A's finding 4, the change stamp `(mtime_ns, size)`:** two edits of the same
  size within the file system's timestamp resolution are not seen until the next change.
  NTFS (100 ns) and ext4 (1 ns) make that practically impossible; FAT and exFAT (2 s) do
  not. **A Docker bind mount of a Windows folder** (Docker Desktop, WSL 2) is expected to
  pass NTFS's times through, but this is **not verified** (it needs an image pull, which
  the owner declined). Saves from the browser editor (CI-4) go through the server, which can
  drop its cached copy directly, so the gap is only for edits made in a text editor on the
  Windows side; a content hash on every catalog read would close it at the cost of reading
  each file every time.
- **Session A's review of #33, the non-atomic delete,** and **the empty-import rule** were
  fixed in `hardening-1` too: the delete moves the files first and puts everything back if
  a move or the database step fails; an import that brings nothing does not use up the
  chart's one import.
