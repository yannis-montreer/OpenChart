"""Frames between the server and the indicator worker.

A frame is a 4-byte little-endian length, a JSON header of that length, then the raw
float64 arrays the header lists in `"arrays": [[name, count], ...]`, in that order. The
same frames go over pipes (Windows, a child process) and over the sidecar's Unix socket
(Docker). Nothing else is ever written to the worker's stdout.

Sizes are capped and anything malformed raises `ProtocolError`, on both sides: the server
must not trust what comes back from code it does not control, and the worker must not
choke on a truncated request.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np

MAX_HEADER = 4 * 2**20  # bytes: a script source (256 KB) plus a traceback fits easily
MAX_ARRAYS = 256 * 2**20  # bytes in all: 5,000 bars x 6 columns is 240 KB
MAX_ARRAY_COUNT = 64


class ProtocolError(ValueError):
    """A frame that is too large or not in the format above."""


def encode(header: Mapping[str, Any], arrays: Mapping[str, Any] | None = None) -> bytes:
    arrays = arrays or {}
    if len(arrays) > MAX_ARRAY_COUNT:
        raise ProtocolError(f"{len(arrays)} arrays; at most {MAX_ARRAY_COUNT}")
    data = {k: np.ascontiguousarray(v, dtype="<f8").reshape(-1) for k, v in arrays.items()}
    body = json.dumps(
        {**header, "arrays": [[k, len(v)] for k, v in data.items()]},
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    if len(body) > MAX_HEADER:
        raise ProtocolError(f"header of {len(body):,} bytes; at most {MAX_HEADER:,}")
    if sum(v.nbytes for v in data.values()) > MAX_ARRAYS:
        raise ProtocolError(f"arrays larger than {MAX_ARRAYS:,} bytes")
    return b"".join([struct.pack("<I", len(body)), body, *(v.tobytes() for v in data.values())])


def parse_header(raw: bytes) -> tuple[dict[str, Any], list[tuple[str, int]]]:
    try:
        header = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"header is not JSON: {exc}") from None
    except RecursionError:  # nested too deep for the parser (S2): not a header either
        raise ProtocolError("header is nested too deeply") from None
    if not isinstance(header, dict):
        raise ProtocolError("header is not a JSON object")
    listed = header.pop("arrays", [])
    if not isinstance(listed, list) or len(listed) > MAX_ARRAY_COUNT:
        raise ProtocolError("bad array list")
    out: list[tuple[str, int]] = []
    total = 0
    for item in listed:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], int)
            or isinstance(item[1], bool)
            or item[1] < 0
        ):
            raise ProtocolError(f"bad array entry {item!r:.80}")
        total += 8 * item[1]
        if total > MAX_ARRAYS:
            raise ProtocolError(f"arrays larger than {MAX_ARRAYS:,} bytes")
        out.append((item[0], item[1]))
    return header, out


def frame_size(prefix: bytes) -> int:
    (size,) = struct.unpack("<I", prefix)
    if size == 0 or size > MAX_HEADER:
        raise ProtocolError(f"header length {size:,}; at most {MAX_HEADER:,}")
    return size


SKIP_CHUNK = 2**20  # bytes read at a time from an array that is skipped


def read_frame(
    read_exact: Callable[[int], bytes], max_count: int | Callable[[], int] | None = None
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """One frame, from a function that returns exactly n bytes or raises EOFError.

    With `max_count` (the job's bar count, S4; a function is asked only once the header has
    arrived, since the reader waits for a frame before the job it answers is even sent), an
    array listed with more values is read and dropped a megabyte at a time, never held: in
    its place comes a stand-in with no values of its own (stride 0) but the length the
    worker claimed, so the server's check
    (`Indicator.check_wire`) refuses it with the usual message and the stream stays in step.
    """
    header, listed = parse_header(read_exact(frame_size(read_exact(4))))
    if callable(max_count):
        max_count = max_count()
    arrays: dict[str, np.ndarray] = {}
    for name, n in listed:
        if max_count is not None and n > max_count:
            left = 8 * n
            while left:
                left -= len(read_exact(min(left, SKIP_CHUNK)))
            arrays[name] = np.lib.stride_tricks.as_strided(np.zeros(1), shape=(n,), strides=(0,))
        else:
            arrays[name] = np.frombuffer(read_exact(8 * n), dtype="<f8")
    return header, arrays


def exact_reader(f) -> Callable[[int], bytes]:
    """`read_exact` over a binary file object (a pipe or a socket file)."""

    def read_exact(n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            chunk = f.read(n - len(out))
            if not chunk:
                raise EOFError
            out += chunk
        return bytes(out)

    return read_exact
