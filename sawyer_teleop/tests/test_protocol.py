import json
import struct
import pytest
from sawyer_teleop.protocol import Decoder, ProtocolError, encode, vector


def test_fragmented_and_coalesced():
    blob = encode({"type": "one"})+encode({"type": "two"})
    decoder = Decoder()
    result = []
    for byte in blob:
        result.extend(decoder.feed(bytes([byte])))
    assert result == [{"type": "one"}, {"type": "two"}]
    assert Decoder().feed(blob) == result


@pytest.mark.parametrize("body", [b'{"a":NaN}', b'{"a":1,"a":2}', b'[]', b'null'])
def test_invalid_json(body):
    with pytest.raises(ProtocolError):
        Decoder().feed(struct.pack("!I", len(body))+body)


def test_oversized_and_nonfinite():
    with pytest.raises(ProtocolError):
        Decoder().feed(struct.pack("!I", 65537))
    for q in ([float("nan")]*7, [True]*7, [0]*6):
        with pytest.raises(ProtocolError):
            vector(q, 7)
