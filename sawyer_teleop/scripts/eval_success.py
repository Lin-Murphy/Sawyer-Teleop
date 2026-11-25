"""抽屉放物体（五角星入碗）策略评测：执行表生成、过程证据（视频 / 检查图）与成功率统计。

协议见 docs/EVAL_PROTOCOL.md。三个子命令：

  run-order  由初始条件表生成随机交错的执行表 trials.csv（不覆盖已有文件）
  evidence   为每次试验生成两路相机并排的 overview.mp4 和首 / 中 / 尾帧检查图 contact_sheet.jpg
  analyze    读取填好的 trials.csv，输出 results.json 和 results.md（Wilson CI、McNemar、Fisher、Holm）

统计函数只用标准库；evidence 需要 numpy、opencv-python 和 Pillow，有 ffmpeg 时视频编码为 H.264。
"""
import argparse
import csv
import hashlib
import json
import math
import random
import shutil
import subprocess
import sys
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

# 抗干扰：在 ID 前 perturb_n 个条件上各跑一次；参数按序号交替，事先写入执行表
PERTURB = OrderedDict([
    ("pull_drawer", {"trigger": "arm_moving_before_drawer_contact",
                     "params": ["drawer_pull_cm=+5", "drawer_pull_cm=+10"]}),
    ("displace_object", {"trigger": "stage1_drawer_open_before_gripper_close",
                         "params": ["object_dy_cm=+5", "object_dy_cm=-5"]}),
    ("move_bowl", {"trigger": "stage2_grasped_before_release",
                   "params": ["bowl_dy_cm=+5", "bowl_dy_cm=-5"]}),
])
PREFILLED = ["trial_id", "run_index", "method", "cond_id", "split", "axis", "level", "priority", "paired_ref",
             "perturb_type", "perturb_trigger", "perturb_param", "eef_x_m", "eef_y_m", "grasp_yaw_deg",
             "bowl_dx_cm", "bowl_dy_cm", "drawer", "requires_check"]
FILLED = ["datetime", "operator", "judge", "checkpoint_sha256", "code_commit", "config_sha256", "recording_id",
          "success", "stage_reached", "failure_mode", "duration_s", "intervention", "perturb_applied",
          "perturb_t_s", "recovery_time_s", "excluded", "exclude_reason", "rerun_of", "video_path",
          "photo_path", "notes"]
FAILURE_MODES = {"drawer_fail", "grasp_miss", "grasp_drop", "place_miss", "collision", "stall", "other"}
Z95 = 1.959963984540054


# ---------------------------------------------------------------- statistics
def wilson(k, n, z=Z95):
    if n == 0:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def mcnemar_exact(b, c):
    """两侧精确 McNemar：b、c 为不一致对数（A 成功 B 失败、A 失败 B 成功）。"""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def fisher_exact(a, b, c, d):
    """两侧 Fisher 精确检验，表 [[a, b], [c, d]]（行=组，列=成功 / 失败）。"""
    r1, c1, n = a + b, a + c, a + b + c + d
    lo, hi = max(0, c1 - (n - r1)), min(r1, c1)
    total = math.comb(n, c1)

    def prob(x):
        return math.comb(r1, x) * math.comb(n - r1, c1 - x) / total
    p_obs = prob(a)
    return min(1.0, sum(prob(x) for x in range(lo, hi + 1) if prob(x) <= p_obs * (1 + 1e-7)))


def holm(pvalues):
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted, running = [0.0] * m, 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * pvalues[i]))
        adjusted[i] = running
    return adjusted


# ---------------------------------------------------------------- helpers
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def group_key(row):
    return "{}/{}/{}".format(row["split"], row["axis"], row["level"])


def unit_key(row):
    return (row["cond_id"], row["axis"], row["level"])


# ---------------------------------------------------------------- run-order
def run_order(args):
    out = Path(args.out)
    if out.exists():
        sys.exit(f"{out} 已存在；执行表只追加不覆盖，请换文件名")
    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    if len(set(methods)) != len(methods) or not methods:
        sys.exit("--methods 需为不重复的方法名列表")
    conds = read_csv(args.conditions)
    if args.take:
        # 试跑：只取指定档位，各取表中顺序的前 N 条（与完整评测共用同一批条件）
        take = OrderedDict()
        for item in args.take.split(","):
            level, _, n = item.partition(":")
            take[level.strip()] = int(n)
        picked = []
        for level, n in take.items():
            rows_l = [r for r in conds if r["level"] == level][:n]
            if len(rows_l) < n:
                sys.exit(f"档位 {level} 只有 {len(rows_l)} 条，少于 {n}")
            picked += rows_l
        if "id_unseen" in take and args.perturb_n > take["id_unseen"]:
            sys.exit("--perturb-n 不能超过所取的 id_unseen 条数")
        selected = picked
    else:
        keep = (lambda r: r["priority"] == "P1") if args.priority == "P1" else (lambda r: True)
        selected = [r for r in conds if keep(r)]
    units = [dict(r, perturb_type="none", perturb_trigger="", perturb_param="") for r in selected]
    refs = [r for r in conds if r["level"] == "id_unseen"][:args.perturb_n]
    if len(refs) < args.perturb_n:
        sys.exit(f"id_unseen 只有 {len(refs)} 条，少于 --perturb-n {args.perturb_n}")
    types = list(PERTURB) if args.perturb is None else [t for t in args.perturb.split(",") if t]
    for t in types:
        if t not in PERTURB:
            sys.exit(f"未知扰动类型 {t}")
        params = PERTURB[t]["params"]
        for i, base in enumerate(refs):
            units.append(dict(base, split="PERTURB", axis="perturb", level=t, paired_ref=base["cond_id"], priority="P1",
                              perturb_type=t, perturb_trigger=PERTURB[t]["trigger"], perturb_param=params[i % len(params)]))
    rng = random.Random(args.seed)
    rng.shuffle(units)
    rows = []
    for u in units:
        for m in rng.sample(methods, len(methods)):
            row = {k: u.get(k, "") for k in PREFILLED}
            row.update(method=m, run_index=str(len(rows) + 1), trial_id="T{:04d}".format(len(rows) + 1))
            row.update({k: "" for k in FILLED})
            rows.append(row)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=PREFILLED + FILLED)
        w.writeheader()
        w.writerows(rows)
    meta = {"seed": args.seed, "methods": methods, "priority": args.priority, "perturb": types, "perturb_n": args.perturb_n, "take": args.take,
            "conditions": str(args.conditions), "conditions_sha256": sha256(args.conditions),
            "units": len(units), "trials": len(rows), "trials_sha256": sha256(out),
            "counts": dict(Counter(group_key(u) for u in units))}
    out.with_name(out.stem + "_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- evidence
def _episode_cameras(episode):
    import numpy as np
    cams = OrderedDict()
    for folder in sorted((episode / "cameras").iterdir()):
        info_path, rgb = folder / "info.json", folder / "frames.rgb"
        if not (info_path.is_file() and rgb.is_file()):
            continue
        info = json.loads(info_path.read_text(encoding="utf-8"))
        shape = tuple(info["shape"])
        ns = [json.loads(line)["ns"] for line in (folder / "frames.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        n = rgb.stat().st_size // int(np.prod(shape))
        if n == 0:
            continue
        cams[folder.name] = {"frames": np.memmap(rgb, dtype=info.get("dtype", "uint8"), mode="r", shape=(n,) + shape),
                             "ns": np.asarray(ns[:n], dtype=np.int64), "folder": folder}
    return cams


def _gripper_events(episode):
    path = episode / "commands.jsonl"
    if not path.is_file():
        return []
    events, prev = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        g = r.get("gripper")
        if r.get("gripper_valid") and g in (-1, 1) and g != prev:
            if prev is not None or g == 1:
                events.append(("close" if g == 1 else "open", int(r["ns"])))
            prev = g
    return events


def _font(size):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _sheet(tiles, header, out, tile_w=320):
    """tiles: [(row_name, [(label, rgb ndarray or None), ...]), ...]"""
    from PIL import Image, ImageDraw
    cols = max(len(c) for _, c in tiles)
    tile_h = int(tile_w * 3 / 4)
    pad, head, lab = 6, 30, 20
    W = pad + cols * (tile_w + pad)
    H = head + len(tiles) * (tile_h + lab + pad) + pad
    sheet = Image.new("RGB", (W, H), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    draw.text((pad, 7), header, fill=(240, 240, 240), font=_font(16))
    for r, (row_name, cells) in enumerate(tiles):
        y = head + r * (tile_h + lab + pad)
        for c, (label, frame) in enumerate(cells):
            x = pad + c * (tile_w + pad)
            draw.text((x, y + 2), "{} | {}".format(row_name, label), fill=(220, 220, 120), font=_font(13))
            if frame is not None:
                img = Image.fromarray(frame).convert("RGB")
                img.thumbnail((tile_w, tile_h))
                sheet.paste(img, (x, y + lab))
    sheet.save(out, quality=90)


def _open_writer(path, w, h, fps):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
               "-r", f"{fps:.3f}", "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23", str(path)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        return "h264", (lambda f: proc.stdin.write(f.tobytes())), (lambda: (proc.stdin.close(), proc.wait())[1])
    import cv2
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    if not writer.isOpened():
        raise RuntimeError("video_writer_failed")
    return "mp4v", (lambda f: writer.write(cv2.cvtColor(f, cv2.COLOR_RGB2BGR))), (lambda: writer.release() or 0)


def evidence_from_episode(episode, out_dir, title, gripper_events=False, video=True):
    import cv2
    import numpy as np
    episode = Path(episode)
    manifest = json.loads((episode / "manifest.json").read_text(encoding="utf-8"))
    start = int(manifest["started_ns"])
    cams = _episode_cameras(episode)
    if not cams:
        raise RuntimeError(f"{episode} 无可读相机帧")
    t0 = min(int(c["ns"][0]) for c in cams.values())
    t1 = max(int(c["ns"][-1]) for c in cams.values())
    picks = [("first", t0), ("mid", (t0 + t1) // 2), ("last", t1)]
    if gripper_events:
        picks += [(name, t) for name, t in _gripper_events(episode)]
        picks.sort(key=lambda p: p[1])  # 检查图各列按时间先后排列
    used, tiles = {}, []
    for name, cam in cams.items():
        cells = []
        for label, t in picks:
            if label in ("first", "last"):
                i = 0 if label == "first" else len(cam["ns"]) - 1
            else:
                i = int(np.argmin(np.abs(cam["ns"] - t)))
            ts = (int(cam["ns"][i]) - start) / 1e9
            cells.append(("{} t={:.1f}s".format(label, ts), np.asarray(cam["frames"][i])))
            used.setdefault(name, []).append({"label": label, "index": i, "t_s": round(ts, 3)})
        tiles.append((name, cells))
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet = out_dir / "contact_sheet.jpg"
    _sheet(tiles, "{}   episode {}   {:.1f}s".format(title, episode.name, (t1 - t0) / 1e9), sheet)
    result = {"source": "episode", "episode": str(episode), "frames_used": used,
              "contact_sheet": sheet.name, "contact_sheet_sha256": sha256(sheet),
              "inputs_sha256": {p.relative_to(episode).as_posix(): sha256(p) for p in
                                [episode / "manifest.json"] + [c["folder"] / "frames.jsonl" for c in cams.values()]
                                + [c["folder"] / "video.mp4" for c in cams.values() if (c["folder"] / "video.mp4").is_file()]},
              "camera_videos": [str((c["folder"] / "video.mp4")) for c in cams.values() if (c["folder"] / "video.mp4").is_file()]}
    if video:
        master_name = max(cams, key=lambda k: len(cams[k]["ns"]))
        master = cams[master_name]
        fps = (len(master["ns"]) - 1) * 1e9 / max(1, int(master["ns"][-1] - master["ns"][0]))
        h = max(c["frames"].shape[1] for c in cams.values())
        widths = [int(c["frames"].shape[2] * h / c["frames"].shape[1]) for c in cams.values()]
        W = sum(widths)
        W += W % 2
        path = out_dir / "overview.mp4"
        codec, write, close = _open_writer(path, W, h, fps)
        cursors = {k: 0 for k in cams}
        for t in master["ns"]:
            canvas = np.zeros((h, W, 3), dtype=np.uint8)
            x = 0
            for (name, cam), w in zip(cams.items(), widths):
                j = cursors[name]
                while j + 1 < len(cam["ns"]) and abs(int(cam["ns"][j + 1]) - int(t)) <= abs(int(cam["ns"][j]) - int(t)):
                    j += 1
                cursors[name] = j
                frame = np.asarray(cam["frames"][j])
                if frame.shape[0] != h:
                    frame = cv2.resize(frame, (w, h))
                canvas[:, x:x + w] = frame
                cv2.putText(canvas, name, (x + 8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
                x += w
            cv2.putText(canvas, "{}  t={:.2f}s".format(title, (int(t) - start) / 1e9), (8, h - 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            write(canvas)
        if close() != 0:
            raise RuntimeError("video encoder failed")
        result.update(overview=path.name, overview_codec=codec, overview_fps=round(fps, 3),
                      overview_frames=len(master["ns"]), overview_sha256=sha256(path))
    return result


def evidence_from_video(video, out_dir, title):
    import cv2
    cap = cv2.VideoCapture(str(video))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    if n <= 0:
        raise RuntimeError(f"{video} 无法读取帧数")
    cells, used = [], []
    for label, i in [("first", 0), ("mid", n // 2), ("last", n - 1)]:
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, frame = cap.read()
        if not ok and label == "last":  # 部分容器末帧不可寻址，向前回退
            for back in range(1, 30):
                cap.set(cv2.CAP_PROP_POS_FRAMES, i - back)
                ok, frame = cap.read()
                if ok:
                    i -= back
                    break
        ts = i / fps if fps else float("nan")
        cells.append(("{} t={:.1f}s".format(label, ts), cv2.cvtColor(frame, cv2.COLOR_BGR2RGB) if ok else None))
        used.append({"label": label, "index": i, "t_s": round(ts, 3), "ok": bool(ok)})
    cap.release()
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet = out_dir / "contact_sheet.jpg"
    _sheet([("video", cells)], "{}   {}".format(title, Path(video).name), sheet)
    return {"source": "video", "video": str(video), "video_sha256": sha256(video), "frames_used": used,
            "contact_sheet": sheet.name, "contact_sheet_sha256": sha256(sheet)}


def find_episode(data_root, recording_id):
    p = Path(recording_id)
    if p.is_dir():
        return p
    hits = [h for h in Path(data_root).glob("**/" + recording_id) if (h / "manifest.json").is_file()]
    if len(hits) != 1:
        raise RuntimeError(f"recording_id {recording_id} 在 {data_root} 下找到 {len(hits)} 个目录")
    return hits[0]


def evidence(args):
    out_root = Path(args.out)
    jobs = []
    if args.trials:
        for r in read_csv(args.trials):
            if args.trial and r["trial_id"] not in args.trial:
                continue
            if not (r.get("recording_id") or r.get("video_path")):
                continue
            if (out_root / r["trial_id"] / "evidence.json").is_file() and not args.force:
                continue
            title = "{} {} {}".format(r["trial_id"], r["cond_id"], r["level"])
            if args.show_method:
                title += " " + r["method"]
            jobs.append((r["trial_id"], title, r.get("recording_id"), r.get("video_path")))
    else:
        if not args.trial or len(args.trial) != 1 or not (args.episode or args.video):
            sys.exit("单条模式需要 --trial 一个 ID，以及 --episode 或 --video")
        jobs.append((args.trial[0], args.trial[0], args.episode, args.video))
    done = []
    for trial_id, title, rec, vid in jobs:
        out_dir = out_root / trial_id
        try:
            if rec:
                res = evidence_from_episode(find_episode(args.data_root, rec) if args.trials else Path(rec),
                                            out_dir, title, args.gripper_events, not args.no_video)
            else:
                res = evidence_from_video(Path(vid), out_dir, title)
        except Exception as exc:
            print(f"{trial_id}: 失败 {exc}", file=sys.stderr)
            continue
        res.update(trial_id=trial_id, script_sha256=sha256(__file__))
        (out_dir / "evidence.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        done.append(trial_id)
        print(f"{trial_id}: {out_dir / res['contact_sheet']}" + (f" , {out_dir / res['overview']}" if res.get("overview") else ""))
    print(f"完成 {len(done)} / {len(jobs)}")


# ---------------------------------------------------------------- analyze
def _int(v):
    return int(v) if str(v).strip() not in ("", "nan") else None


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def _rate(rows):
    k, n = sum(r["_s"] for r in rows), len(rows)
    ci = wilson(k, n)
    return {"k": k, "n": n, "rate": round(k / n, 4) if n else None,
            "ci95": [round(ci[0], 4), round(ci[1], 4)] if ci else None}


def analyze(args):
    trials_path = Path(args.trials)
    raw = read_csv(trials_path)
    problems, valid, pending, excluded = [], [], [], []
    for r in raw:
        if str(r.get("excluded", "")).strip() == "1":
            excluded.append(r)
            continue
        s = str(r.get("success", "")).strip()
        if s == "":
            pending.append(r["trial_id"])
            continue
        if s not in ("0", "1"):
            problems.append(f"{r['trial_id']}: success={s!r}")
            continue
        fm = r.get("failure_mode", "").strip()
        if s == "0" and fm not in FAILURE_MODES:
            problems.append(f"{r['trial_id']}: 失败但 failure_mode={fm!r}")
        st = _int(r.get("stage_reached"))
        if st is None or not 0 <= st <= 3 or (s == "1") != (st == 3):
            problems.append(f"{r['trial_id']}: stage_reached={r.get('stage_reached')!r} 与 success={s} 不一致")
        valid.append(dict(r, _s=int(s)))
    seen = defaultdict(list)
    for r in valid:
        seen[(r["method"],) + unit_key(r)].append(r["trial_id"])
    dups = {"/".join(k): v for k, v in seen.items() if len(v) > 1}
    if dups:
        problems.append(f"同一方法同一条件有多条有效试验：{dups}")
    if problems and not args.allow_problems:
        sys.exit("数据检查未通过：\n  " + "\n  ".join(problems))

    methods = sorted({r["method"] for r in raw})
    groups = sorted({group_key(r) for r in raw}, key=lambda g: (g.split("/")[0] != "ID", g))
    cell = defaultdict(list)
    for r in valid:
        cell[(r["method"], group_key(r))].append(r)
    table = OrderedDict()
    for g in groups:
        table[g] = OrderedDict()
        for m in methods:
            rows = cell[(m, g)]
            entry = _rate(rows)
            entry["stage_reach"] = {str(s): round(sum((_int(r["stage_reached"]) or 0) >= s for r in rows) / len(rows), 4)
                                    if rows else None for s in (1, 2, 3)}
            entry["failure_modes"] = dict(Counter(r["failure_mode"] for r in rows if not r["_s"]))
            entry["median_duration_success_s"] = _median([d for d in (_float(r["duration_s"]) for r in rows if r["_s"]) if d is not None])
            if g.startswith("PERTURB/"):
                applied = [r for r in rows if str(r.get("perturb_applied", "")).strip() == "1"]
                entry["intention_to_treat"] = _rate(rows)
                entry["conditional"] = _rate(applied)
                entry["median_recovery_s"] = _median([d for d in (_float(r["recovery_time_s"]) for r in applied if r["_s"]) if d is not None])
            table[g][m] = entry

    # 方法之间：同条件配对 McNemar
    between = []
    for g in groups:
        for i, a in enumerate(methods):
            for b in methods[i + 1:]:
                ra = {r["cond_id"]: r["_s"] for r in cell[(a, g)]}
                rb = {r["cond_id"]: r["_s"] for r in cell[(b, g)]}
                common = sorted(set(ra) & set(rb))
                if not common:
                    continue
                n10 = sum(ra[c] == 1 and rb[c] == 0 for c in common)
                n01 = sum(ra[c] == 0 and rb[c] == 1 for c in common)
                between.append({"group": g, "a": a, "b": b, "pairs": len(common), "a_only": n10, "b_only": n01,
                                "test": "mcnemar_exact", "p": mcnemar_exact(n10, n01)})
    # 方法内：与 ID 比较
    vs_id = []
    for m in methods:
        id_rows = {r["cond_id"]: r["_s"] for r in cell[(m, "ID/none/id_unseen")]}
        for g in groups:
            if g.startswith("ID/"):
                continue
            rows = cell[(m, g)]
            if not rows or not id_rows:
                continue
            if all(r["paired_ref"] for r in rows):
                pairs = [(id_rows[r["paired_ref"]], r["_s"]) for r in rows if r["paired_ref"] in id_rows]
                if not pairs:
                    continue
                n10 = sum(x == 1 and y == 0 for x, y in pairs)
                n01 = sum(x == 0 and y == 1 for x, y in pairs)
                vs_id.append({"method": m, "group": g, "test": "mcnemar_exact", "pairs": len(pairs),
                              "id_only": n10, "group_only": n01, "p": mcnemar_exact(n10, n01)})
            else:
                k1, n1 = sum(id_rows.values()), len(id_rows)
                k2, n2 = sum(r["_s"] for r in rows), len(rows)
                vs_id.append({"method": m, "group": g, "test": "fisher_exact", "id": [k1, n1], "group_kn": [k2, n2],
                              "p": fisher_exact(k1, n1 - k1, k2, n2 - k2)})
    for family in (between, vs_id):
        for t, adj in zip(family, holm([t["p"] for t in family])):
            t["p"] = round(t["p"], 6)
            t["p_holm"] = round(adj, 6)

    evidence_missing = None
    if args.evidence:
        ev = Path(args.evidence)
        evidence_missing = [r["trial_id"] for r in valid if not (ev / r["trial_id"] / "contact_sheet.jpg").is_file()]
    inputs = {"trials": str(trials_path), "trials_sha256": sha256(trials_path)}
    if args.protocol:
        inputs.update(protocol=str(args.protocol), protocol_sha256=sha256(args.protocol))
    result = {"inputs": inputs, "script_sha256": sha256(__file__), "methods": methods,
              "counts": {"rows": len(raw), "valid": len(valid), "pending": len(pending), "excluded": len(excluded)},
              "pending": pending,
              "exclusions": [{"method": m, "reason": why, "n": n} for (m, why), n in
                             sorted(Counter((r["method"], r.get("exclude_reason", "")) for r in excluded).items())],
              "problems": problems, "table": table, "between_methods": between, "vs_id": vs_id,
              "evidence_missing": evidence_missing}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "results.md").write_text(render_md(result), encoding="utf-8")
    print(render_md(result))


def _fmt(e):
    if not e or not e["n"]:
        return "–"
    lo, hi = e["ci95"]
    return "{}/{} ({:.0f}%) [{:.0f}, {:.0f}]".format(e["k"], e["n"], 100 * e["rate"], 100 * lo, 100 * hi)


def render_md(res):
    ms = res["methods"]
    lines = ["# 评测结果", "",
             "trials `{}` · sha256 `{}` · 有效 {} / 待跑 {} / 排除 {}".format(
                 res["inputs"]["trials"], res["inputs"]["trials_sha256"][:12], res["counts"]["valid"],
                 res["counts"]["pending"], res["counts"]["excluded"]), "",
             "## 成功率（k/n，Wilson 95% CI，%）", "", "| 组 | " + " | ".join(ms) + " |", "|---|" + "---|" * len(ms)]
    for g, row in res["table"].items():
        lines.append("| {} | {} |".format(g, " | ".join(_fmt(row[m]) for m in ms)))
        if g.startswith("PERTURB/"):
            lines.append("| ↳ 条件成功率（已施加） | {} |".format(" | ".join(_fmt(row[m].get("conditional")) for m in ms)))
    lines += ["", "## 方法之间（配对 McNemar，Holm 校正）", "", "| 组 | A | B | 配对 | 仅A成 | 仅B成 | p | p_holm |", "|---|---|---|---|---|---|---|---|"]
    lines += ["| {group} | {a} | {b} | {pairs} | {a_only} | {b_only} | {p:.4g} | {p_holm:.4g} |".format(**t) for t in res["between_methods"]]
    lines += ["", "## 与 ID 比较（抗干扰组配对 McNemar，OOD 组 Fisher，Holm 校正）", "", "| 方法 | 组 | 检验 | p | p_holm |", "|---|---|---|---|---|"]
    lines += ["| {method} | {group} | {test} | {p:.4g} | {p_holm:.4g} |".format(**t) for t in res["vs_id"]]
    lines += ["", "## 阶段到达率与失败模式", "", "| 组 | 方法 | ≥1 抽屉开 | ≥2 已抓起 | 3 已放入 | 失败模式 | 成功耗时中位 s |", "|---|---|---|---|---|---|---|"]
    for g, row in res["table"].items():
        for m in ms:
            e = row[m]
            if not e["n"]:
                continue
            sr = e["stage_reach"]
            lines.append("| {} | {} | {:.0%} | {:.0%} | {:.0%} | {} | {} |".format(
                g, m, sr["1"], sr["2"], sr["3"], ", ".join("{} {}".format(k, v) for k, v in sorted(e["failure_modes"].items())) or "–",
                "–" if e["median_duration_success_s"] is None else "{:.1f}".format(e["median_duration_success_s"])))
    if res["exclusions"]:
        lines += ["", "## 排除", ""] + ["- {method}：{reason}（{n}）".format(**x) for x in res["exclusions"]]
    if res["evidence_missing"]:
        lines += ["", "缺少检查图的有效试验：" + ", ".join(res["evidence_missing"])]
    if res["problems"]:
        lines += ["", "## 数据问题", ""] + ["- " + p for p in res["problems"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Windows 控制台默认 cp1252，-I 下 PYTHONIOENCODING 也不生效
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run-order", help="生成执行表")
    p.add_argument("--conditions", required=True)
    p.add_argument("--methods", required=True, help="逗号分隔，如 pi0_ft,diffusion")
    p.add_argument("--priority", choices=["P1", "all"], default="P1")
    p.add_argument("--perturb", default=None, help="逗号分隔；默认 pull_drawer,displace_object,move_bowl；空串表示不跑抗干扰")
    p.add_argument("--perturb-n", type=int, default=15, help="抗干扰使用的 ID 条件数（取 id_unseen 前 N 条）")
    p.add_argument("--take", default=None, help="试跑：只取这些档位的前 N 条，如 id_unseen:10,obj_bowl_pos:10（忽略 --priority）")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=run_order)
    p = sub.add_parser("evidence", help="生成 overview.mp4 与首 / 中 / 尾帧检查图")
    p.add_argument("--out", required=True, help="证据根目录，每次试验一个 <trial_id>/ 子目录")
    p.add_argument("--trials", help="批量模式：按 trials.csv 中的 recording_id / video_path 处理")
    p.add_argument("--data-root", default=".", help="批量模式下查找 recording_id 的录制根目录")
    p.add_argument("--trial", action="append", help="只处理这些 trial_id（可重复）；单条模式必填")
    p.add_argument("--episode", help="单条模式：项目录制目录")
    p.add_argument("--video", help="单条模式：外部视频文件（录屏 / 外置相机）")
    p.add_argument("--gripper-events", action="store_true", help="检查图额外加入闭爪 / 松爪时刻")
    p.add_argument("--no-video", action="store_true", help="只生成检查图")
    p.add_argument("--show-method", action="store_true", help="标题写方法名（默认不写，便于盲评复判）")
    p.add_argument("--force", action="store_true", help="重做已有证据")
    p.set_defaults(func=evidence)
    p = sub.add_parser("analyze", help="统计成功率")
    p.add_argument("--trials", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--protocol", help="定稿协议文件，记录其哈希")
    p.add_argument("--evidence", help="证据根目录，用于检查每条有效试验是否有检查图")
    p.add_argument("--allow-problems", action="store_true", help="数据检查有问题时仍输出（问题会写入结果）")
    p.set_defaults(func=analyze)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
