import csv
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")
HERE = Path(__file__).resolve().parents[1] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ev = _load("eval_vision")
ep = _load("eval_place")
SHAPE = (480, 640, 3)


def to_px(xy):  # 合成相机：线性映射，10 px/cm
    return int(round(1000 * xy[1])), int(round(1000 * xy[0] - 300))


def scene(xy=None, seed=0):
    rng = np.random.default_rng(seed)
    # 带竖条纹的灰色背景（像真实场景一样有纹理，相机平移时背景差会明显变大）
    stripes = (100 + 60 * ((np.arange(SHAPE[1]) // 32) % 2)).astype(np.uint8)
    img = np.broadcast_to(stripes[None, :, None], SHAPE).copy() + rng.integers(0, 3, SHAPE, dtype=np.uint8)
    if xy is not None:
        cv2.circle(img, to_px(xy), 9, (200, 30, 30), -1)
    return img


@pytest.fixture
def vision_dir(tmp_path):
    data, eps = tmp_path / "data", []
    for i, (x, y) in enumerate([(x, y) for x in (0.50, 0.52, 0.54) for y in (0.25, 0.30, 0.35, 0.40)]):
        cam = data / "batch_a" / f"episode_{i:02d}" / "cameras" / "realsense"
        cam.mkdir(parents=True)
        (cam / "info.json").write_text(json.dumps({"shape": list(SHAPE), "dtype": "uint8"}), encoding="utf-8")
        (cam / "frames.rgb").write_bytes(scene((x, y), i).tobytes() * 2)
        eps.append({"batch": "batch_a", "episode": f"episode_{i:02d}", "grasp_pos": [x, y, 0.59], "grasp_yaw_deg": 70.0})
    gp = tmp_path / "grasp_poses.json"
    gp.write_text(json.dumps({"episodes": eps}), encoding="utf-8")
    out = tmp_path / "vision"
    meta = ev.build(data, gp, out)
    assert meta["px_per_cm_at_centre"] == pytest.approx(10, rel=0.02)
    assert meta["homography_loo_cm"]["max"] < 0.2
    assert meta["ood_margin_px"] == pytest.approx(20, rel=0.02)
    return out


def test_verdicts(vision_dir):
    V = ev.Vision(vision_dir)
    r, _, _ = ev.evaluate(scene((0.52, 0.33)), V, "ID", [0.52, 0.33])
    assert r["verdict"] and r["camera_ok"] and r["target_dist_cm_approx"] < 0.3
    r, _, _ = ev.evaluate(scene((0.60, 0.33)), V, "OOD")
    assert r["verdict"] and r["hull_dist_px"] == pytest.approx(60, abs=1.5)
    r, _, _ = ev.evaluate(scene((0.555, 0.33)), V, "OOD")  # 只超出凸包 1.5 cm
    assert not r["verdict"] and "太近" in r["reason"]
    r, _, _ = ev.evaluate(scene((0.60, 0.33)), V, "ID")
    assert not r["verdict"]
    r, _, _ = ev.evaluate(scene(None), V, "ID")
    assert not r["found"] and not r["verdict"]
    moved = np.roll(scene((0.52, 0.33)), 40, axis=1)
    moved[:, :40] = 0
    assert not ev.evaluate(moved, V, "ID")[0]["camera_ok"]


class FakeCam:
    def __init__(self, img):
        self.img = img

    def latest(self, max_age=1.0):
        return {"rgb": self.img}


def test_interactive_keys(vision_dir):
    V = ev.Vision(vision_dir)
    shown = []
    keys = iter([255, 13])
    r, img, action = ev.interactive_check(FakeCam(scene((0.52, 0.33))), V, "ID", [0.52, 0.33], "C001",
                                          show=shown.append, key=lambda: next(keys))
    assert action == "accept" and r["verdict"] and img.ndim == 3 and len(shown) == 2
    # 判定不通过时 Enter 不生效，必须 F 强制或 Q 跳过
    keys = iter([13, 13, ord("f")])
    r, _, action = ev.interactive_check(FakeCam(scene((0.555, 0.33))), V, "OOD", None, "C071",
                                        show=lambda i: None, key=lambda: next(keys))
    assert action == "force" and not r["verdict"]
    keys = iter([ord("q")])
    assert ev.interactive_check(FakeCam(scene(None)), V, "ID", None, "x", show=lambda i: None, key=lambda: next(keys))[2] == "skip"


def test_place_no_arm_logs_vision(vision_dir, tmp_path):
    cond = tmp_path / "c.csv"
    rows = [dict(cond_id="C001", split="ID", level="id_unseen", eef_x_m="0.52", eef_y_m="0.33", grasp_yaw_deg="70", bowl_dx_cm="0", bowl_dy_cm="0"),
            dict(cond_id="C071", split="OOD", level="obj_bowl_pos", eef_x_m="0.60", eef_y_m="0.33", grasp_yaw_deg="90", bowl_dx_cm="3", bowl_dy_cm="-4"),
            dict(cond_id="C072", split="OOD", level="obj_bowl_pos", eef_x_m="0.58", eef_y_m="0.30", grasp_yaw_deg="80", bowl_dx_cm="-2", bowl_dy_cm="5")]
    with open(cond, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    V = ev.Vision(vision_dir)
    live = {"C001": (scene((0.52, 0.33)), 13), "C071": (scene((0.60, 0.33)), 13), "C072": (scene((0.555, 0.33)), ord("q"))}

    def check(split, target, title):
        img, k = live[title.split()[0]]
        return ev.interactive_check(FakeCam(img), V, split, target, title, show=lambda i: None, key=lambda: k)
    prompts = []
    out = tmp_path / "eval"
    args = ep.build_parser().parse_args(["place", "--conditions", str(cond), "--take", "id_unseen:1,obj_bowl_pos:2",
                                         "--out", str(out), "--no-arm"])
    done = ep.cmd_place(args, ask=lambda p: prompts.append(p) or "", check_fn=check)
    assert done == ["C001", "C071"]
    log = {r["cond_id"]: r for r in map(json.loads, (out / "placement_log.jsonl").read_text(encoding="utf-8").splitlines())}
    assert log["C001"]["mode"] == "manual" and log["C001"]["vision"]["verdict"] is True
    assert log["C071"]["vision"]["verdict"] is True and log["C072"]["status"] == "skipped_at_vision"
    assert (out / "reference" / "C071_diff.png").is_file() and log["C071"]["diff_image"]["sha256"]
    assert any("dx=3" in p and "尺子" in p for p in prompts)


def test_no_arm_requires_vision(tmp_path):
    args = ep.build_parser().parse_args(["place", "--conditions", "x.csv", "--cond", "C001", "--out", str(tmp_path), "--no-arm"])
    with pytest.raises(SystemExit):
        ep.select_conditions = lambda *a: [{"cond_id": "C001"}]
        ep.cmd_place(args)
