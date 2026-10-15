"""The frames between the server and the indicator worker: a round trip, the size caps,
and garbage refused on both sides."""

import io
import json
import struct

import numpy as np
import pytest

from openchart.indicators.worker import protocol
from openchart.indicators.worker.protocol import ProtocolError, encode, exact_reader, read_frame


def roundtrip(data: bytes):
    return read_frame(exact_reader(io.BytesIO(data)))


def test_a_frame_round_trips_with_its_arrays():
    bars = {"c": np.array([1.5, np.nan, -2.0]), "ts": np.arange(3, dtype="f8")}
    head, arrays = roundtrip(encode({"cmd": "compute", "params": {"length": 9}}, bars))
    assert head == {"cmd": "compute", "params": {"length": 9}}
    assert list(arrays) == ["c", "ts"]
    np.testing.assert_array_equal(arrays["c"], bars["c"])
    np.testing.assert_array_equal(arrays["ts"], [0.0, 1.0, 2.0])


def test_two_frames_in_a_row():
    f = io.BytesIO(encode({"a": 1}) + encode({"b": 2}, {"x": [1.0]}))
    read = exact_reader(f)
    assert read_frame(read)[0] == {"a": 1}
    head, arrays = read_frame(read)
    assert head == {"b": 2} and arrays["x"].tolist() == [1.0]
    with pytest.raises(EOFError):
        read_frame(read)


def raw(header: bytes, tail: bytes = b"") -> bytes:
    return struct.pack("<I", len(header)) + header + tail


@pytest.mark.parametrize(
    ("data", "why"),
    [
        (struct.pack("<I", 0), "header length"),
        (struct.pack("<I", protocol.MAX_HEADER + 1), "header length"),
        (raw(b"\xff\xfe not json"), "not JSON"),
        (raw(b"[1, 2]"), "not a JSON object"),
        (raw(b'{"arrays": "x"}'), "bad array list"),
        (raw(b'{"arrays": [["c", -1]]}'), "bad array entry"),
        (raw(b'{"arrays": [["c", 1.5]]}'), "bad array entry"),
        (raw(b'{"arrays": [["c", true]]}'), "bad array entry"),
        (raw(json.dumps({"arrays": [["c", protocol.MAX_ARRAYS]]}).encode()), "larger than"),
    ],
)
def test_garbage_is_refused(data, why):
    with pytest.raises(ProtocolError, match=why):
        roundtrip(data)


def test_a_truncated_frame_is_the_end_of_the_stream():
    data = encode({"cmd": "compute"}, {"c": np.ones(10)})
    with pytest.raises(EOFError):
        roundtrip(data[:-4])


def test_sending_more_than_the_caps_is_refused():
    with pytest.raises(ProtocolError, match="header"):
        encode({"source": "x" * (protocol.MAX_HEADER + 1)})
    with pytest.raises(ProtocolError, match="arrays"):
        encode({}, {str(i): [0.0] for i in range(protocol.MAX_ARRAY_COUNT + 1)})


def test_headers_never_carry_nan():
    with pytest.raises(ValueError):
        encode({"value": float("nan")})
