import copy
import numpy as np
from sawyer_teleop.quest import QuestValidator, heading_mapping


def packet(seq=1, buttons=0):
    p = {"p": [0, 0, 0], "q": [0, 0, 0, 1]}
    return {"protocol": 3, "session": "test", "seq": seq, "sample_ns": 1_000_000_000+seq,
            "focused": True, "mounted": True, "space": 1, "head_status": 15, "head": p,
            "recenter": 0, "hands": {"r": {"input_ok": True, "tracking_ok": True,
              "status": 15, "pose": p, "grip": .8, "buttons": buttons, "recenter": 0, "device": 2}}}


def test_button_independent_of_tracking_and_edge_triggered():
    validator = QuestValidator(0, 1_000_000)
    p = packet(buttons=2)
    p["hands"]["r"]["tracking_ok"] = False
    value = validator.parse(p, 1_010_000_000)
    assert not value["valid"] and value["events"] == ["discard"]
    assert validator.parse(packet(2, 2), 1_010_000_000)["events"] == []
    assert validator.parse(packet(3, 0), 1_010_000_000)["events"] == []
    assert validator.parse(packet(4, 3), 1_010_000_000)["events"] == ["save"]


def test_stale_or_old_protocol_cannot_trigger():
    validator = QuestValidator(0, 1_000_000)
    assert not validator.parse(packet(buttons=1), 2_000_000_000)["events"]
    p = packet(buttons=1)
    p["protocol"] = 2
    assert not validator.parse(p, 1_010_000_000)["valid"]


def test_coordinate_mapping():
    mapping = heading_mapping([0, 0, -1])
    assert np.allclose(mapping@[0, 0, -1], [1, 0, 0])
    assert np.allclose(mapping@[1, 0, 0], [0, -1, 0])
    assert np.allclose(mapping@[0, 1, 0], [0, 0, 1])
