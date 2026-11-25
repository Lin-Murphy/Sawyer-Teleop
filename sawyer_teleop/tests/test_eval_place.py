import csv
import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("scipy")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_place.py"
spec = importlib.util.spec_from_file_location("eval_place", SCRIPT)
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)

START = {"p": (0.25, 0.34, 0.77), "rpy_deg": (176.5, 1.0, 72.0)}


def test_calibration_math_and_bowl_target():
    calib = ep.calibration_from_points([0.40, 0.36, 0.65], [0.30, 0.36, 0.64], "拉到底")
    assert calib["u"] == pytest.approx([1.0, 0.0]) and calib["v"] == pytest.approx([0.0, 1.0])
    assert calib["pull_distance_m"] == pytest.approx(0.10)
    assert ep.bowl_target(calib, 2.0, -3.0) == pytest.approx([0.42, 0.33, 0.65])
    with pytest.raises(ValueError):
        ep.calibration_from_points([0.40, 0.36, 0.65], [0.39, 0.36, 0.65], "x")  # 只差 1 cm


def test_hover_quat_matches_training_convention():
    from scipy.spatial.transform import Rotation
    q = ep.hover_quat(71.7, ep.DEFAULTS)
    yaw, pitch, roll = Rotation.from_quat(q).as_euler("ZYX", degrees=True)
    assert (yaw, pitch, roll) == pytest.approx((71.7, 1.0, 176.48), abs=1e-6)
    x, y, z, w = q  # 与 extract_grasp_poses.py 的偏航公式一致
    assert math.degrees(math.atan2(2 * (x * y + w * z), 1 - 2 * (y * y + z * z))) == pytest.approx(71.7)


@pytest.fixture
def bridge():
    from sawyer_teleop.backends import FakeBackend
    from sawyer_teleop.server import BridgeServer
    backend = FakeBackend()
    p, rpy = START["p"], [math.radians(a) for a in START["rpy_deg"]]
    q = [p[0] - 0.5, p[1], p[2] - 0.3, 0.0, *rpy]  # FakeBackend：p = (0.5+q0, q1, 0.3+q2)，q4..6 = roll/pitch/yaw
    backend.q, backend.target = list(q), list(q)
    server = BridgeServer(backend, control_port=0, ik_port=0).start()
    yield server, backend
    server.close()


def _conds(path):
    rows = [
        dict(cond_id="C001", level="id_unseen", eef_x_m="0.55", eef_y_m="0.40", grasp_yaw_deg="65", bowl_dx_cm="0", bowl_dy_cm="0"),
        dict(cond_id="C071", level="obj_bowl_pos", eef_x_m="0.60", eef_y_m="0.30", grasp_yaw_deg="90", bowl_dx_cm="3", bowl_dy_cm="-4"),
        dict(cond_id="C099", level="obj_pos", eef_x_m="0.95", eef_y_m="0.30", grasp_yaw_deg="70", bowl_dx_cm="0", bowl_dy_cm="0"),
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def _common(server):
    return ["--host", "127.0.0.1", "--control-port", str(server.control_port), "--ik-port", str(server.ik_port),
            "--allow-fake", "--speed", "0.1"]


def test_fake_backend_rejected_without_flag(bridge, tmp_path):
    server, _ = bridge
    cond = tmp_path / "c.csv"
    _conds(cond)
    with pytest.raises(PermissionError):
        ep.main(["check", "--conditions", str(cond), "--cond", "C001", "--host", "127.0.0.1",
                 "--control-port", str(server.control_port), "--ik-port", str(server.ik_port)])


def test_check_and_place_on_fake_bridge(bridge, tmp_path):
    server, backend = bridge
    q_before = list(backend.q)
    cond = tmp_path / "c.csv"
    _conds(cond)
    calib =ep.calibration_from_points([0.42, 0.36, 0.64], [0.32, 0.36, 0.64], "拉到底", {"yaw_deg": 71.7})
    cal_path = tmp_path / "bowl_calibration.json"
    cal_path.write_text(json.dumps(calib), encoding="utf-8")
    report = ep.main(["check", "--conditions", str(cond), "--cond", "C001", "--cond", "C071", "--cond", "C099",
                      "--bowl-calibration", str(cal_path), "--out", str(tmp_path / "check.json")] + _common(server))
    status = {r["cond_id"]: r["status"] for r in report}
    assert status == {"C001": "reachable", "C071": "reachable", "C099": "unreachable"}
    assert "工作空间" in report[2]["reason"]
    # check 只求 IK：B 在 arm / hold 时只会重发当前位置，关节不应变化
    assert np.allclose(backend.q, q_before, atol=1e-9) and np.allclose(backend.target, q_before, atol=1e-9)

    import time
    deadline = time.monotonic() + 3  # 实际使用时 check 与 place 是两次独立运行；这里等 B 释放上一个会话
    while server.core.session is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    answers = []
    out = tmp_path / "eval"
    args = ep.build_parser().parse_args(["place", "--conditions", str(cond), "--cond", "C001", "--cond", "C071",
                                         "--cond", "C099", "--bowl-calibration", str(cal_path), "--out", str(out)]
                                        + _common(server))
    done = ep.cmd_place(args, ask=lambda prompt: answers.append(prompt) or "")
    assert done == ["C001", "C071"]
    log = [json.loads(l) for l in (out / "placement_log.jsonl").read_text(encoding="utf-8").splitlines()]
    by = {r["cond_id"]: r for r in log}
    assert by["C099"]["status"] == "unreachable"
    for cid in ("C001", "C071"):
        assert by[cid]["status"] == "placed" and by[cid]["object_error_m"] < 0.005
    assert by["C071"]["bowl_target"] == pytest.approx([0.45, 0.32, 0.64])
    assert by["C071"]["bowl_error_m"] < 0.005
    assert any("drawer_ref" in a for a in answers) and any("推回" in a for a in answers)
    # 每条摆放后都回到起始位姿
    start_q = [START["p"][0] - 0.5, START["p"][1], START["p"][2] - 0.3]
    assert np.allclose(backend.q[:3], start_q, atol=1e-3)
