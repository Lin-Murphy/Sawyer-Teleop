import csv
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_success.py"
spec = importlib.util.spec_from_file_location("eval_success", SCRIPT)
ev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ev)


def test_wilson_known_values():
    lo, hi = ev.wilson(9, 30)
    assert lo == pytest.approx(0.16664, abs=1e-4) and hi == pytest.approx(0.47875, abs=1e-4)
    assert ev.wilson(0, 10)[0] == pytest.approx(0.0, abs=1e-12)
    assert ev.wilson(10, 10)[1] == pytest.approx(1.0, abs=1e-12)
    assert ev.wilson(0, 0) is None


def test_exact_tests_match_scipy():
    stats = pytest.importorskip("scipy.stats")
    for b, c in [(0, 0), (1, 7), (3, 3), (0, 9), (12, 4)]:
        ref = 1.0 if b + c == 0 else stats.binomtest(min(b, c), b + c, 0.5).pvalue
        assert ev.mcnemar_exact(b, c) == pytest.approx(ref, rel=1e-9)
    for table in [(9, 21, 22, 8), (0, 15, 5, 10), (15, 0, 15, 0), (3, 12, 7, 8)]:
        a, b, c, d = table
        assert ev.fisher_exact(a, b, c, d) == pytest.approx(stats.fisher_exact([[a, b], [c, d]])[1], rel=1e-9)


def test_holm():
    assert ev.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])


def _conditions(path):
    rows = []
    for i in range(4):
        rows.append(dict(cond_id=f"C{i + 1:03d}", split="ID", axis="none", level="id_unseen", priority="P1", paired_ref=""))
    for i, level in enumerate(["obj_pos", "obj_pos", "bowl_pos", "bowl_pos"]):
        rows.append(dict(cond_id=f"C{i + 5:03d}", split="OOD", axis="position", level=level, priority="P1", paired_ref=""))
    rows.append(dict(cond_id="C009", split="ID", axis="none", level="id_seen", priority="P2", paired_ref=""))
    for r in rows:
        r.update(eef_x_m="0.55", eef_y_m="0.40", grasp_yaw_deg="70", bowl_dx_cm="0", bowl_dy_cm="0",
                 drawer="as_train", requires_check="")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def test_run_order_and_analyze(tmp_path):
    cond = tmp_path / "cond.csv"
    _conditions(cond)
    trials = tmp_path / "trials.csv"
    order = ["run-order", "--conditions", str(cond), "--methods", "A,B", "--seed", "7", "--perturb-n", "2"]
    ev.main(order + ["--out", str(trials)])
    rows = ev.read_csv(trials)
    # P1: 8 条件 + 3 种抗干扰 × 前 2 个 ID = 14 单元 × 2 方法
    assert len(rows) == 28
    assert {r["level"] for r in rows if r["axis"] == "perturb"} == {"pull_drawer", "displace_object", "move_bowl"}
    assert all(r["paired_ref"] in ("C001", "C002") for r in rows if r["axis"] == "perturb")
    params = {(r["level"], r["paired_ref"]): r["perturb_param"] for r in rows if r["axis"] == "perturb"}
    assert params[("pull_drawer", "C001")] == "drawer_pull_cm=+5" and params[("pull_drawer", "C002")] == "drawer_pull_cm=+10"
    assert params[("move_bowl", "C002")] == "bowl_dy_cm=-5"
    # 同一单元的两个方法相邻执行
    for i in range(0, 28, 2):
        assert ev.unit_key(rows[i]) == ev.unit_key(rows[i + 1]) and rows[i]["method"] != rows[i + 1]["method"]
    again = tmp_path / "again.csv"
    ev.main(order + ["--out", str(again)])
    assert trials.read_bytes() == again.read_bytes()
    with pytest.raises(SystemExit):
        ev.main(order + ["--out", str(trials)])

    for r in rows:
        ok = r["method"] == "A" or r["cond_id"] in ("C001", "C003")
        r.update(success="1" if ok else "0", stage_reached="3" if ok else "2",
                 failure_mode="" if ok else "grasp_drop", duration_s="25", perturb_applied="1" if r["axis"] == "perturb" else "")
    rows[0].update(excluded="1", exclude_reason="camera_drop")
    rows.append(dict(rows[0], trial_id="T9999", excluded="", exclude_reason="", rerun_of=rows[0]["trial_id"]))
    filled = tmp_path / "filled.csv"
    with open(filled, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    out = tmp_path / "res"
    ev.main(["analyze", "--trials", str(filled), "--out", str(out)])
    res = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert res["counts"] == {"rows": 29, "valid": 28, "pending": 0, "excluded": 1}
    assert res["table"]["ID/none/id_unseen"]["A"]["k"] == 4
    assert res["table"]["ID/none/id_unseen"]["B"]["k"] == 2
    assert "conditional" in res["table"]["PERTURB/perturb/move_bowl"]["A"]
    assert res["table"]["PERTURB/perturb/move_bowl"]["B"]["k"] == 1  # 只有 C001 成功
    tests = {t["test"] for t in res["vs_id"]}
    assert tests == {"mcnemar_exact", "fisher_exact"}
    assert all(0 <= t["p"] <= t["p_holm"] <= 1 for t in res["between_methods"] + res["vs_id"])
    assert (out / "results.md").read_text(encoding="utf-8").startswith("# 评测结果")

    rows[1]["stage_reached"] = "1" if rows[1]["success"] == "1" else "3"
    with open(filled, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with pytest.raises(SystemExit):
        ev.main(["analyze", "--trials", str(filled), "--out", str(out)])


def test_run_order_take_subset(tmp_path):
    cond = tmp_path / "cond.csv"
    _conditions(cond)
    trials = tmp_path / "pilot.csv"
    ev.main(["run-order", "--conditions", str(cond), "--methods", "A", "--seed", "3", "--perturb-n", "2",
             "--take", "id_unseen:3,bowl_pos:1", "--out", str(trials)])
    rows = ev.read_csv(trials)
    base = sorted(r["cond_id"] for r in rows if r["axis"] != "perturb")
    assert base == ["C001", "C002", "C003", "C007"]  # 各档位按表中顺序取前 N 条
    assert len(rows) == 4 + 3 * 2 and {r["paired_ref"] for r in rows if r["axis"] == "perturb"} == {"C001", "C002"}
    with pytest.raises(SystemExit):
        ev.main(["run-order", "--conditions", str(cond), "--methods", "A", "--seed", "3", "--perturb-n", "4",
                 "--take", "id_unseen:3", "--out", str(tmp_path / "bad.csv")])


def test_evidence_from_synthetic_episode(tmp_path):
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    pytest.importorskip("PIL")
    ep = tmp_path / "episode_x"
    start = 1_000_000_000
    (ep / "cameras").mkdir(parents=True)
    (ep / "manifest.json").write_text(json.dumps({"started_ns": start}), encoding="utf-8")
    for name, n, step in [("realsense", 12, 50_000_000), ("wrist", 10, 60_000_000)]:
        d = ep / "cameras" / name
        d.mkdir()
        (d / "info.json").write_text(json.dumps({"shape": [48, 64, 3], "dtype": "uint8"}), encoding="utf-8")
        frames = np.zeros((n, 48, 64, 3), dtype=np.uint8)
        frames[:, :, :, 0] = np.arange(n, dtype=np.uint8)[:, None, None] * 20
        (d / "frames.rgb").write_bytes(frames.tobytes())
        (d / "frames.jsonl").write_text("".join(json.dumps({"ns": start + i * step}) + "\n" for i in range(n)), encoding="utf-8")
    cmds = [{"ns": start + 200_000_000, "gripper": -1, "gripper_valid": True},
            {"ns": start + 300_000_000, "gripper": 1, "gripper_valid": True},
            {"ns": start + 500_000_000, "gripper": -1, "gripper_valid": True}]
    (ep / "commands.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cmds), encoding="utf-8")
    out = tmp_path / "evidence"
    ev.main(["evidence", "--out", str(out), "--trial", "T0001", "--episode", str(ep), "--gripper-events"])
    res = json.loads((out / "T0001" / "evidence.json").read_text(encoding="utf-8"))
    # 中间时刻 0.275 s 早于 0.3 s 闭爪，各列按时间排列
    assert [u["label"] for u in res["frames_used"]["realsense"]] == ["first", "mid", "close", "open", "last"]
    assert res["frames_used"]["realsense"][4]["index"] == 11 and res["frames_used"]["wrist"][4]["index"] == 9
    assert res["frames_used"]["realsense"][2]["index"] == 6
    assert (out / "T0001" / "contact_sheet.jpg").stat().st_size > 0
    assert res["overview_frames"] == 12 and (out / "T0001" / "overview.mp4").stat().st_size > 0
    raw_files = sorted(p.name for p in ep.rglob("*") if p.is_file())
    assert "contact_sheet.jpg" not in raw_files and "overview.mp4" not in raw_files

    ev.main(["evidence", "--out", str(out), "--trial", "T0002", "--video", str(out / "T0001" / "overview.mp4")])
    vres = json.loads((out / "T0002" / "evidence.json").read_text(encoding="utf-8"))
    assert [u["label"] for u in vres["frames_used"]] == ["first", "mid", "last"] and all(u["ok"] for u in vres["frames_used"])
