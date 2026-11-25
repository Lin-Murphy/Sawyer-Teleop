"""评测摆放的图像对照：用训练录制首帧判断当前摆放是 ID 还是 OOD，并生成 diff 图。

依据（2026-11-25 用训练数据验证）：
- 60 条训练录制的 RealSense 首帧里，红色五角星都能稳定分割（单个连通域，约 450–570 像素）。
- 用"闭爪末端 xy ↔ 星像素中心"拟合桌面单应矩阵，留一法反算误差中位数约 2.5 cm，最大约 11 cm。
  原因是抓取点相对星中心的偏差不固定，而且图像里 1 cm 只有 3–4 像素。所以单应矩阵只用于画约 ±2 cm 的引导圈和估计坐标，
  ID / OOD 判定一律在像素空间完成：
    ID：实时星中心落在训练星中心的像素凸包内；
    OOD：实时星中心在凸包外，且距凸包 ≥ ood_margin_px（默认约 2 cm 对应的像素）。
  与训练星覆盖区的重叠比例和到最近训练星的距离只作为辅助显示。
- 背景：60 帧首帧的逐像素中位数（星的位置各不相同，中位数会把星去掉）。用实时画面与背景在星区域外的平均差判断相机是否移动。

build 输出目录内容：vision.json（参数、训练星中心、单应矩阵及误差、输入哈希）、background.png、coverage.png、frames/<episode>.jpg。
"""
import hashlib
import json
from pathlib import Path

import numpy as np

STAR_HSV = {"hue_low_max": 8, "hue_high_min": 170, "sat_min": 120, "val_min": 60, "min_area_px": 150}
THRESHOLDS = {"ood_margin_cm": 2.0, "id_tolerance_px": 1.0, "bg_diff_warn": 12.0, "guide_radius_cm": 2.0}
PANEL_SCALE = 0.75
ZOOM_PAD = 45


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def star_mask(rgb, params=STAR_HSV):
    """返回最大红色连通域的 (mask uint8 0/1, 中心 [x, y], 面积)；找不到返回 (None, None, 0)。"""
    import cv2
    hsv = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    m = (((h < params["hue_low_max"]) | (h > params["hue_high_min"])) & (s > params["sat_min"]) & (v > params["val_min"]))
    n, lab, st, cen = cv2.connectedComponentsWithStats(m.astype(np.uint8))
    if n <= 1:
        return None, None, 0
    i = 1 + int(np.argmax(st[1:, cv2.CC_STAT_AREA]))
    area = int(st[i, cv2.CC_STAT_AREA])
    if area < params["min_area_px"]:
        return None, None, 0
    return (lab == i).astype(np.uint8), [float(cen[i][0]), float(cen[i][1])], area


def convex_hull(points):
    import cv2
    return cv2.convexHull(np.asarray(points, np.float32)).reshape(-1, 2)


def hull_signed_dist(hull, p):
    """像素凸包的有符号距离：外正内负。"""
    import cv2
    return -float(cv2.pointPolygonTest(hull.reshape(-1, 1, 2).astype(np.float32), (float(p[0]), float(p[1])), True))


def project(H, pts):
    import cv2
    return cv2.perspectiveTransform(np.asarray(pts, np.float64).reshape(-1, 1, 2), np.asarray(H, np.float64)).reshape(-1, 2)


def px_per_cm(H, xy):
    """单应矩阵在 xy 处的局部尺度（取两个方向中较小者，偏保守）。"""
    base = np.asarray(xy, float)
    pts = project(H, [base, base + [0.01, 0], base + [0, 0.01]])
    return float(min(np.linalg.norm(pts[1] - pts[0]), np.linalg.norm(pts[2] - pts[0])))


def _first_frame(episode, camera):
    folder = episode / "cameras" / camera
    info_raw = (folder / "info.json").read_bytes()
    shape = tuple(json.loads(info_raw)["shape"])
    n = int(np.prod(shape))
    with open(folder / "frames.rgb", "rb") as f:
        raw = f.read(n)
    return np.frombuffer(raw, np.uint8).reshape(shape), sha256_bytes(info_raw), sha256_bytes(raw)


def build(data_root, grasp_poses, out_dir, camera="realsense"):
    import cv2
    data_root, out_dir = Path(data_root), Path(out_dir)
    gp_raw = Path(grasp_poses).read_bytes()
    gp = json.loads(gp_raw)
    (out_dir / "frames").mkdir(parents=True, exist_ok=True)
    frames, eps, hashes, coverage = [], [], {}, None
    for e in gp["episodes"]:
        ep = data_root / e["batch"] / e["episode"]
        rgb, h_info, h_frame = _first_frame(ep, camera)
        hashes[f"{e['batch']}/{e['episode']}/cameras/{camera}/frame0"] = h_frame
        hashes[f"{e['batch']}/{e['episode']}/cameras/{camera}/info.json"] = h_info
        mask, c, area = star_mask(rgb)
        if mask is None:
            raise RuntimeError(f"{e['episode']} 首帧未找到红色物体")
        coverage = mask.astype(np.uint16) if coverage is None else coverage + mask
        frames.append(rgb)
        cv2.imwrite(str(out_dir / "frames" / f"{e['episode']}.jpg"), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
                    [cv2.IMWRITE_JPEG_QUALITY, 92])
        eps.append({"batch": e["batch"], "episode": e["episode"], "grasp_xy": e["grasp_pos"][:2],
                    "grasp_yaw_deg": e["grasp_yaw_deg"], "centroid_px": [round(c[0], 2), round(c[1], 2)], "area_px": area})
    background = np.median(np.stack(frames), axis=0).astype(np.uint8)
    cv2.imwrite(str(out_dir / "background.png"), cv2.cvtColor(background, cv2.COLOR_RGB2BGR))
    cv2.imwrite(str(out_dir / "coverage.png"), np.clip(coverage, 0, 255).astype(np.uint8))
    xy = np.array([e["grasp_xy"] for e in eps])
    px = np.array([e["centroid_px"] for e in eps])
    H, _ = cv2.findHomography(xy, px, 0)
    loo = []
    for i in range(len(eps)):
        keep = np.arange(len(eps)) != i
        Hi, _ = cv2.findHomography(xy[keep], px[keep], 0)
        loo.append(float(np.linalg.norm(project(np.linalg.inv(Hi), px[i:i + 1])[0] - xy[i]) * 100))
    hull = convex_hull(px)
    areas = [e["area_px"] for e in eps]
    centre_xy = np.median(xy, axis=0)
    scale = px_per_cm(H, centre_xy)
    vis = {
        "camera": camera, "shape": list(frames[0].shape), "star_hsv": STAR_HSV, "thresholds": THRESHOLDS,
        "episodes": eps, "hull_px": hull.round(2).tolist(),
        "star_area_px_median": float(np.median(areas)), "star_diameter_px": float(2 * np.sqrt(np.median(areas) / np.pi)),
        "homography_xy_to_px": H.tolist(), "px_per_cm_at_centre": round(scale, 3),
        "ood_margin_px": round(THRESHOLDS["ood_margin_cm"] * scale, 2),
        "homography_loo_cm": {"median": round(float(np.median(loo)), 3), "p90": round(float(np.percentile(loo, 90)), 3),
                              "max": round(float(max(loo)), 3)},
        "note": "单应矩阵精度约 2 cm，只用于引导圈和估计坐标；ID/OOD 判定基于像素凸包",
        "input": {"grasp_poses": str(grasp_poses), "grasp_poses_sha256": sha256_bytes(gp_raw), "frames_sha256": hashes},
        "script_sha256": sha256_bytes(Path(__file__).read_bytes()),
    }
    vis["outputs_sha256"] = {p.name: sha256_bytes(p.read_bytes()) for p in (out_dir / "background.png", out_dir / "coverage.png")}
    (out_dir / "vision.json").write_text(json.dumps(vis, ensure_ascii=False, indent=1), encoding="utf-8")
    return vis


class Vision:
    def __init__(self, directory):
        import cv2
        self.dir = Path(directory)
        self.meta = json.loads((self.dir / "vision.json").read_text(encoding="utf-8"))
        self.background = cv2.cvtColor(cv2.imread(str(self.dir / "background.png")), cv2.COLOR_BGR2RGB)
        self.coverage = cv2.imread(str(self.dir / "coverage.png"), cv2.IMREAD_GRAYSCALE) > 0
        self.hull = np.asarray(self.meta["hull_px"], np.float32)
        self.H = np.asarray(self.meta["homography_xy_to_px"], float)
        self.centroids = np.asarray([e["centroid_px"] for e in self.meta["episodes"]], float)
        self.sha256 = sha256_bytes((self.dir / "vision.json").read_bytes())
        self._frames = {}

    def frame(self, episode):
        import cv2
        if episode not in self._frames:
            self._frames[episode] = cv2.cvtColor(cv2.imread(str(self.dir / "frames" / f"{episode}.jpg")), cv2.COLOR_BGR2RGB)
        return self._frames[episode]


def evaluate(rgb, vision, split, target_xy=None):
    """计算 ID / OOD 判定和辅助量，不画图。split 为 'ID' 或 'OOD'。"""
    import cv2
    meta = vision.meta
    mask, c, area = star_mask(rgb, meta["star_hsv"])
    out = {"split": split, "found": mask is not None, "area_px": area}
    if target_xy is not None:
        out["target_px"] = project(vision.H, [target_xy])[0].round(1).tolist()
    # 相机是否移动：星区域（训练覆盖区 + 当前星）外的平均差
    excl = vision.coverage.copy()
    if mask is not None:
        excl |= mask.astype(bool)
    excl = cv2.dilate(excl.astype(np.uint8), np.ones((15, 15), np.uint8)).astype(bool)
    diff = np.abs(rgb.astype(np.int16) - vision.background.astype(np.int16)).mean(axis=2)
    out["bg_diff"] = round(float(diff[~excl].mean()), 2)
    out["camera_ok"] = out["bg_diff"] <= meta["thresholds"]["bg_diff_warn"]
    if mask is None:
        out.update(verdict=False, reason="未检测到红色物体")
        return out, mask, diff
    d_hull = hull_signed_dist(vision.hull, c)
    dists = np.linalg.norm(vision.centroids - np.asarray(c), axis=1)
    k = int(np.argmin(dists))
    scale = meta["px_per_cm_at_centre"]
    est = project(np.linalg.inv(vision.H), [c])[0]
    out.update(centroid_px=[round(c[0], 1), round(c[1], 1)], hull_dist_px=round(d_hull, 2),
               hull_dist_cm_approx=round(d_hull / scale, 2), nearest_episode=meta["episodes"][k]["episode"],
               nearest_px=round(float(dists[k]), 1), nearest_cm_approx=round(float(dists[k]) / scale, 2),
               coverage_overlap=round(float((mask.astype(bool) & vision.coverage).sum()) / area, 3),
               est_xy_approx=[round(float(est[0]), 4), round(float(est[1]), 4)])
    if target_xy is not None:
        out["target_dist_cm_approx"] = round(float(np.linalg.norm(est - np.asarray(target_xy)) * 100), 2)
    if split == "ID":
        ok = d_hull <= meta["thresholds"].get("id_tolerance_px", 1.0)  # 训练凸包顶点本身属于 ID，留浮点容差
        reason = "中心在训练凸包内" if ok else f"中心在训练凸包外 {d_hull:.1f}px（约 {d_hull / scale:.1f} cm）"
    else:
        margin = meta["ood_margin_px"]
        ok = d_hull >= margin
        reason = (f"中心距训练凸包 {d_hull:.1f}px ≥ {margin:.1f}px" if ok
                  else f"中心距训练凸包 {d_hull:.1f}px < {margin:.1f}px（约 {meta['thresholds']['ood_margin_cm']} cm），与训练位置太近")
    out.update(verdict=bool(ok), reason=reason)
    return out, mask, diff


def panel(rgb, vision, result, mask, diff, title):
    """左：实时画面 + 训练凸包 / 训练星中心 / 覆盖区轮廓 / 引导圈 / 当前星；中：与最近训练首帧 50% 叠加；右：与背景的差异热图。"""
    import cv2
    live = cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2BGR)
    cov = vision.coverage.astype(np.uint8)
    contours, _ = cv2.findContours(cov, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(live, contours, -1, (255, 200, 0), 1)
    cv2.polylines(live, [vision.hull.astype(np.int32).reshape(-1, 1, 2)], True, (255, 255, 0), 2)
    for c in vision.centroids:
        cv2.circle(live, tuple(int(round(v)) for v in c), 2, (255, 255, 0), -1)
    if "target_px" in result:
        t = tuple(int(round(v)) for v in result["target_px"])
        r = int(round(vision.meta["thresholds"]["guide_radius_cm"] * vision.meta["px_per_cm_at_centre"]))
        cv2.circle(live, t, r, (255, 0, 255), 2)
        cv2.drawMarker(live, t, (255, 0, 255), cv2.MARKER_CROSS, 14, 2)
    if mask is not None:
        cs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(live, cs, -1, (0, 255, 255), 2)
        cv2.circle(live, tuple(int(round(v)) for v in result["centroid_px"]), 4, (0, 255, 255), -1)
    near = result.get("nearest_episode") or vision.meta["episodes"][0]["episode"]
    blend = cv2.addWeighted(live, 0.5, cv2.cvtColor(vision.frame(near), cv2.COLOR_RGB2BGR), 0.5, 0)
    heat = cv2.applyColorMap(np.clip(diff * 3, 0, 255).astype(np.uint8), cv2.COLORMAP_JET)
    h, w = live.shape[:2]
    size = (int(w * PANEL_SCALE), int(h * PANEL_SCALE))
    top = np.hstack([cv2.resize(x, size) for x in (live, heat)])
    # 放大视图：训练凸包外扩 ZOOM_PAD 像素的区域（含引导圈和当前星），左实时、右与最近训练首帧叠加
    pts = [vision.hull]
    if "target_px" in result:
        pts.append(np.asarray([result["target_px"]], np.float32))
    if result.get("centroid_px"):
        pts.append(np.asarray([result["centroid_px"]], np.float32))
    allp = np.vstack(pts)
    x0, y0 = np.maximum(allp.min(0) - ZOOM_PAD, 0).astype(int)
    x1, y1 = np.minimum(allp.max(0) + ZOOM_PAD, [w - 1, h - 1]).astype(int)
    zw = top.shape[1] // 2
    zh = int(round(zw * (y1 - y0) / max(1, x1 - x0)))
    zoom = np.hstack([cv2.resize(x[y0:y1, x0:x1], (zw, zh), interpolation=cv2.INTER_CUBIC) for x in (live, blend)])
    row = np.vstack([top, zoom])
    ok = result.get("verdict")
    colour = (0, 200, 0) if ok else (0, 0, 255)
    lines = [
        f"{title}  [{result['split']}]  {'PASS' if ok else 'FAIL'}" + ("" if result.get("camera_ok", True) else "   CAMERA MOVED? bg_diff=%.1f" % result["bg_diff"]),
        "hull_dist %+.1fpx (~%+.1fcm, OOD needs >= %.1fpx)  nearest train %s %.1fpx  overlap %.0f%%" % (
            result.get("hull_dist_px", float("nan")), result.get("hull_dist_cm_approx", float("nan")),
            vision.meta["ood_margin_px"], str(result.get("nearest_episode", "-"))[8:23], result.get("nearest_px", float("nan")),
            100 * result.get("coverage_overlap", 0)),
        "est_xy ~%s (+-2cm)%s   bg_diff %.1f" % (
            result.get("est_xy_approx"),
            ("  to_target ~%.1fcm" % result["target_dist_cm_approx"]) if "target_dist_cm_approx" in result else "",
            result.get("bg_diff", float("nan"))),
        "Enter/Space accept | F force | Q/Esc skip    top: live, diff vs bg    bottom zoom: live, 50% nearest train",
    ]
    bar = np.full((22 * len(lines) + 8, row.shape[1], 3), 24, np.uint8)
    for i, text in enumerate(lines):
        cv2.putText(bar, text, (8, 20 + 22 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.47, colour if i == 0 else (230, 230, 230), 1, cv2.LINE_AA)
    return np.vstack([bar, row])


def interactive_check(camera, vision, split, target_xy, title, show=None, key=None, max_age=1.0):
    """循环显示实时对照窗口，直到确认 / 强制确认 / 跳过。返回 (结果, 面板图, 动作)。"""
    import cv2
    show = show or (lambda img: cv2.imshow("eval-place", img))
    key = key or (lambda: cv2.waitKey(100) & 0xFF)
    result = img = None
    try:
        while True:
            frame = camera.latest(max_age=max_age)
            if frame:
                result, mask, diff = evaluate(frame["rgb"], vision, split, target_xy)
                img = panel(frame["rgb"], vision, result, mask, diff, title)
                show(img)
            k = key()
            if k in (13, 10, 32) and result is not None and result["verdict"]:
                return result, img, "accept"
            if k in (ord("f"), ord("F")) and result is not None:
                return result, img, "force"
            if k in (ord("q"), ord("Q"), 27):
                return result, img, "skip"
    finally:
        try:
            cv2.destroyWindow("eval-place")
        except Exception:
            pass
