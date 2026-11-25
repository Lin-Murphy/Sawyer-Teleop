"""评测初始条件的机械臂摆放辅助（A 端，经 RobotClient 连接 B）。

机械臂按初始条件表把夹爪悬停在目标点上方，由人把物体 / 碗放到夹爪正下方；机械臂退回起始位后，可选保存两路相机参考图。
协议见 docs/EVAL_PROTOCOL.md §3。子命令：

  calibrate-bowl  碗定位标定：抽屉在 drawer_ref 和退回约 10 cm 两处，各用点动把夹爪对准碗 home 标记，得到 bowl_calibration.json
  check           只做 IK 规划、不发送运动：检查所选条件能否到达（悬停点和接近点）
  place           按条件逐个摆放：碗（偏移非零时）→ 推回抽屉 → 物体 → 退回 → 图像对照 → 参考图；记录 placement_log.jsonl
                  加 --vision 时打开实时对照窗口（scripts/eval_vision.py），按训练首帧判定 ID / OOD 并保存 diff 图；
                  加 --no-arm 时不连机械臂：碗按尺子量偏移，物体对照窗口手动摆放
  build-vision    由训练录制首帧生成对照参考（训练星中心凸包、覆盖区、背景、单应矩阵）

安全：默认只允许 Gazebo 后端（AGENTS.md 当前只授权仿真）；真机须显式加 --allow-real。
所有目标先检查工作空间框，整段路径先全部求出 IK 并检查关节连续，再开始运动；速度上限 5 cm/s、0.5 rad/s。
Ctrl+C 或任何异常都会让 B hold。
"""
import argparse
import csv
import hashlib
import json
import math
import sys
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DEFAULTS = {
    # 训练抓取姿态（train-distribution-20261125/grasp_orientation.json 的中位数）
    "pitch_deg": 1.0, "roll_deg": 176.48,
    # 训练抓取末端高度中位数 0.594 m（grasp_poses.json）；悬停 +3 cm；接近高度接近起始位 0.77 m
    "grasp_z": 0.594, "hover_dz": 0.03, "approach_z": 0.72,
    "bowl_hover_z": 0.65,       # 标定前的碗悬停初值；标定后用标定点高度
    "bowl_guess_xy": [0.405, 0.365],  # 训练放置点中位数，作为标定起点
    "speed_m_s": 0.05, "ang_speed_rad_s": 0.5, "joint_speed_rad_s": 0.3, "hz": 50,
    "step_m": 0.005, "step_rad": 0.02, "max_waypoint_joint_step_rad": 0.15,
    "settle_pos_m": 0.005, "settle_timeout_s": 3.0, "ik_timeout_s": 0.2,  # B 允许 (0, 0.2] s
    "workspace": {"x": [0.20, 0.75], "y": [0.10, 0.70], "z": [0.50, 0.85]},
    "jog_max_mm": 20.0,
}


class Unreachable(RuntimeError):
    pass


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def hover_quat(yaw_deg, cfg):
    from scipy.spatial.transform import Rotation
    return Rotation.from_euler("ZYX", [yaw_deg, cfg["pitch_deg"], cfg["roll_deg"]], degrees=True).as_quat().tolist()


def in_workspace(p, cfg):
    box = cfg["workspace"]
    return all(box[k][0] <= v <= box[k][1] for k, v in zip("xyz", p))


def bowl_target(calib, dx_cm, dy_cm):
    home, u, v = (np.asarray(calib[k], float) for k in ("bowl_home", "u", "v"))
    xy = home[:2] + (dx_cm / 100.0) * u + (dy_cm / 100.0) * v
    return [float(xy[0]), float(xy[1]), float(home[2])]


def calibration_from_points(a, b, drawer_ref, extra=None):
    """a：抽屉在 drawer_ref 时对准 home 标记的末端位置；b：抽屉退回后对准同一标记的位置。"""
    a, b = np.asarray(a, float), np.asarray(b, float)
    d = a[:2] - b[:2]
    dist = float(np.linalg.norm(d))
    if not 0.03 <= dist <= 0.25:
        raise ValueError(f"两次标定点相距 {dist * 100:.1f} cm，应在 3–25 cm（抽屉是否真的退回了？）")
    u = d / dist
    v = np.array([-u[1], u[0]])  # 面向抽屉（视线 -u）时的右手方向
    out = {"bowl_home": a.round(5).tolist(), "u": u.round(6).tolist(), "v": v.round(6).tolist(),
           "pull_distance_m": round(dist, 4), "z_diff_m": round(float(a[2] - b[2]), 4),
           "drawer_ref": drawer_ref, "point_at_ref": a.round(5).tolist(), "point_retracted": b.round(5).tolist(),
           "convention": "目标 xy = bowl_home + dx*u + dy*v（cm→m）；u 为拉出方向，v 为面向抽屉向右"}
    out.update(extra or {})
    return out


class Mover:
    def __init__(self, host, control_port, ik_port, cfg, allow_real=False, allow_backends=("gazebo",), ik_mode="remote"):
        self.cfg, self.allow_real = cfg, allow_real
        self.allowed = set(allow_backends) | ({"intera"} if allow_real else set())
        self.args = (host, control_port, ik_port)
        self.ik_mode = ik_mode
        self.robot = None
        self.start_q = None

    # ---------------------------------------------------------- connection
    def connect(self):
        from sawyer_teleop.client import RobotClient
        from sawyer_teleop.joint_protection import CAPABILITY, protection_options
        from sawyer_teleop.protocol import ProtocolError
        host, cp, ip = self.args
        deadline = time.monotonic() + 3.0
        while True:  # 上一个会话刚关闭时 B 可能仍在释放（bridge_busy / 连接被重置），短暂重试
            self.robot = RobotClient(host, cp, ip, ik_mode=self.ik_mode)
            try:
                self.robot.connect()
                break
            except (ProtocolError, OSError) as exc:
                self.robot.close()
                self.robot = None
                retry = isinstance(exc, OSError) or "bridge_busy" in str(exc)
                if not retry or time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        backend = self.robot.hello.get("backend")
        if backend not in self.allowed:
            self.close()
            raise PermissionError(f"后端为 {backend}；默认只允许 Gazebo 仿真，真机需加 --allow-real 并确认已获授权")
        self.robot.hold("eval_place_start")
        guard = protection_options(True) if self.robot.hello.get("joint_protection") == CAPABILITY else None
        self.robot.arm(joint_protection=guard)
        self.start_q = list(self.robot.snapshot()["sample"]["q"])
        return backend

    def close(self):
        if self.robot:
            try:
                self.robot.hold("eval_place_stop")
            except Exception:
                pass
            finally:
                self.robot.close()
                self.robot = None

    def state(self):
        s = self.robot.snapshot()
        if not s["armed"] or s["generation"] != self.robot.generation:
            raise RuntimeError("B 已退出跟随（hold / 断线），请检查后重试")
        return s

    def pose(self):
        sample = self.state()["sample"]
        return np.asarray(sample["pose"]["p"], float), np.asarray(sample["pose"]["q"], float), list(sample["q"])

    # ---------------------------------------------------------- planning
    def plan_line(self, p0, q0, p1, q1, seed):
        """直线 + slerp 插值，逐点 IK（种子链式传递），返回关节路点和段时长；任何一点失败抛 Unreachable。"""
        from scipy.spatial.transform import Rotation, Slerp
        cfg = self.cfg
        p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
        if not in_workspace(p1, cfg):
            raise Unreachable(f"目标 {np.round(p1, 3).tolist()} 超出工作空间框 {cfg['workspace']}")
        rots = Rotation.from_quat([q0, q1])
        ang = float((rots[0].inv() * rots[1]).magnitude())
        dist = float(np.linalg.norm(p1 - p0))
        n = max(1, int(math.ceil(max(dist / cfg["step_m"], ang / cfg["step_rad"]))))
        slerp = Slerp([0, 1], rots)
        sample = self.state()["sample"]
        waypoints, prev = [], list(seed)
        for i in range(1, n + 1):
            s = i / n
            target = {"p": (p0 + s * (p1 - p0)).tolist(), "q": slerp([s]).as_quat()[0].tolist()}
            q, last_exc = None, None
            for _attempt in range(2):  # B 会关闭空闲的 IK 连接；客户端在连接错误后自动重连，重试一次
                try:
                    q = self.robot.solve(target, prev, sample["seq"], timeout=cfg["ik_timeout_s"])
                    break
                except (OSError, ConnectionError, TimeoutError) as exc:
                    last_exc = exc
                except Exception as exc:
                    last_exc = exc
                    break
            if q is None:
                raise Unreachable(f"IK 失败于路径 {i}/{n}（{np.round(target['p'], 3).tolist()}）：{last_exc}") from last_exc
            step = float(np.max(np.abs(np.asarray(q) - np.asarray(prev))))
            if step > cfg["max_waypoint_joint_step_rad"]:
                raise Unreachable(f"IK 不连续：路径 {i}/{n} 关节跳变 {step:.3f} rad")
            waypoints.append(list(q))
            prev = list(q)
        duration = max(dist / cfg["speed_m_s"], ang / cfg["ang_speed_rad_s"], 1.0)
        return {"waypoints": waypoints, "duration": duration, "end_p": p1.tolist(), "end_q": list(q1),
                "dist_m": dist, "ang_rad": ang}

    def plan_route(self, legs, seed=None, start=None):
        """legs: [(p, quat), ...]，从当前（或给定 start=(p, quat)）出发依次直线到达。"""
        if start is None:
            p0, q0, cur = self.pose()
        else:
            p0, q0 = start
            cur = None
        seed = list(seed if seed is not None else cur)
        plans = []
        for p, q in legs:
            plan = self.plan_line(p0, q0, p, q, seed)
            plans.append(plan)
            p0, q0, seed = np.asarray(p, float), q, plan["waypoints"][-1]
        return plans

    # ---------------------------------------------------------- execution
    def _send(self, q):
        # 回执队列有上限，不取走会在约 4 s 后 receipt_queue_full 断线；这里只需要 B 的反馈，回执直接丢弃
        self.robot.drain_receipts()
        s = self.state()
        self.robot.target(list(map(float, q)), s["sample"]["seq"])

    def run_waypoints(self, start_q, waypoints, duration):
        path = np.vstack([np.asarray(start_q, float)] + [np.asarray(w, float) for w in waypoints])
        period = 1.0 / self.cfg["hz"]
        t0 = time.monotonic()
        while True:
            t = time.monotonic() - t0
            f = min(1.0, t / duration)
            s = f * f * (3 - 2 * f)  # smoothstep：起止速度为零
            x = s * (len(path) - 1)
            i = min(int(x), len(path) - 2)
            q = path[i] + (x - i) * (path[i + 1] - path[i])
            self._send(q)
            if f >= 1.0:
                break
            time.sleep(max(0.0, t0 + (math.floor(t / period) + 1) * period - time.monotonic()))
        return path[-1]

    def settle(self, p_target=None):
        cfg = self.cfg
        deadline = time.monotonic() + cfg["settle_timeout_s"]
        err = None
        while time.monotonic() < deadline:
            s = self.state()["sample"]
            if p_target is not None:
                err = float(np.linalg.norm(np.asarray(s["pose"]["p"]) - np.asarray(p_target)))
            if np.max(np.abs(s["dq"])) < 0.05 and (err is None or err < cfg["settle_pos_m"]):
                return err
            time.sleep(0.02)
        return err

    def execute(self, plans):
        q = list(self.state()["sample"]["q"])
        err = None
        for plan in plans:
            q = self.run_waypoints(q, plan["waypoints"], plan["duration"])
            err = self.settle(plan["end_p"])
        return err

    def move_joints(self, q_goal):
        q_now = np.asarray(self.state()["sample"]["q"], float)
        q_goal = np.asarray(q_goal, float)
        delta = float(np.max(np.abs(q_goal - q_now)))
        steps = max(1, int(math.ceil(delta / 0.05)))
        waypoints = [q_now + (k / steps) * (q_goal - q_now) for k in range(1, steps + 1)]
        self.run_waypoints(q_now, waypoints, max(delta / self.cfg["joint_speed_rad_s"], 1.0))
        self.settle()

    def go_hover(self, xy_or_p, yaw_deg, z):
        quat = hover_quat(yaw_deg, self.cfg)
        x, y = xy_or_p[0], xy_or_p[1]
        plans = self.plan_route([([x, y, self.cfg["approach_z"]], quat), ([x, y, z], quat)])
        return plans, quat

    def retract_home(self):
        p, q, _ = self.pose()
        if p[2] < self.cfg["approach_z"] - 1e-3:
            self.execute(self.plan_route([([p[0], p[1], self.cfg["approach_z"]], q.tolist())]))
        self.move_joints(self.start_q)

    def jog(self, ask, quat):
        """交互点动：x+/x-/y+/y-/z+/z-，s <mm> 改步长，空行确认，q 放弃。返回确认时的末端位置。"""
        step = 5.0
        while True:
            p, _, _ = self.pose()
            cmd = ask(f"  当前 {np.round(p, 4).tolist()}，步长 {step:.0f} mm。点动 x+/x-/y+/y-/z+/z-，s <mm> 改步长，回车确认，q 放弃：").strip()
            if cmd == "":
                return p.tolist()
            if cmd == "q":
                raise KeyboardInterrupt("jog_aborted")
            if cmd.startswith("s "):
                try:
                    step = min(max(float(cmd[2:]), 0.5), self.cfg["jog_max_mm"])
                except ValueError:
                    pass
                continue
            if len(cmd) != 2 or cmd[0] not in "xyz" or cmd[1] not in "+-":
                continue
            delta = np.zeros(3)
            delta["xyz".index(cmd[0])] = (step if cmd[1] == "+" else -step) / 1000.0
            try:
                self.execute(self.plan_route([((p + delta).tolist(), quat)]))
            except Unreachable as exc:
                print(f"  无法点动：{exc}")


# -------------------------------------------------------------- conditions
def select_conditions(path, take=None, cond_ids=None):
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8-sig")))
    if cond_ids:
        by = {r["cond_id"]: r for r in rows}
        missing = [c for c in cond_ids if c not in by]
        if missing:
            raise SystemExit(f"条件表中没有 {missing}")
        return [by[c] for c in cond_ids]
    if take:
        out = []
        for item in take.split(","):
            level, _, n = item.partition(":")
            picked = [r for r in rows if r["level"] == level.strip()][:int(n)]
            if len(picked) < int(n):
                raise SystemExit(f"档位 {level} 不足 {n} 条")
            out += picked
        return out
    raise SystemExit("需要 --take 或 --cond")


def needs_bowl_arm(row):
    return abs(float(row.get("bowl_dx_cm") or 0)) > 1e-9 or abs(float(row.get("bowl_dy_cm") or 0)) > 1e-9


# -------------------------------------------------------------- cameras
class References:
    def __init__(self, operator_config):
        self.cams = []
        if not operator_config:
            return
        from sawyer_teleop.cameras import Camera
        cfg = json.loads(Path(operator_config).read_text(encoding="utf-8"))
        for c in cfg.get("cameras", []):
            try:
                self.cams.append(Camera(**dict(c, reconnect=True)).start())
            except Exception as exc:
                print(f"相机 {c.get('name')} 打开失败：{exc}", file=sys.stderr)

    def save(self, out_dir, cond_id, wait_s=1.0):
        import cv2
        if not self.cams:
            return {}
        time.sleep(wait_s)
        out_dir.mkdir(parents=True, exist_ok=True)
        saved = {}
        for cam in self.cams:
            frame = cam.latest(max_age=1.0)
            if not frame:
                saved[cam.name] = {"error": "no_frame"}
                continue
            path = out_dir / f"{cond_id}_{cam.name}.png"
            cv2.imwrite(str(path), cv2.cvtColor(frame["rgb"], cv2.COLOR_RGB2BGR))
            saved[cam.name] = {"path": path.name, "sha256": sha256(path)}
        return saved

    def get(self, name):
        return next((c for c in self.cams if c.name == name), None)

    def close(self):
        for cam in self.cams:
            cam.close()


def load_vision_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("eval_vision", Path(__file__).with_name("eval_vision.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# -------------------------------------------------------------- commands
def load_cfg(args):
    cfg = json.loads(json.dumps(DEFAULTS))
    if args.speed is not None:
        cfg["speed_m_s"] = min(args.speed, 0.10)
    return cfg


def connect_args(args):
    if args.operator_config:
        op = json.loads(Path(args.operator_config).read_text(encoding="utf-8"))
        host = args.host or op.get("robot_host")
        return host, args.control_port or op.get("control_port", 8765), args.ik_port or op.get("ik_port", 8766)
    return args.host or "127.0.0.1", args.control_port or 8765, args.ik_port or 8766


def make_mover(args, cfg):
    host, cp, ip = connect_args(args)
    allowed = ("gazebo", "fake") if args.allow_fake else ("gazebo",)
    return Mover(host, cp, ip, cfg, allow_real=args.allow_real, allow_backends=allowed)


def log_line(path, record):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def cmd_calibrate(args, ask=input):
    cfg = load_cfg(args)
    out = Path(args.out)
    if out.exists() and not args.force:
        raise SystemExit(f"{out} 已存在；重新标定请加 --force（旧文件会被覆盖，请先备份）")
    mover = make_mover(args, cfg)
    try:
        backend = mover.connect()
        quat = hover_quat(args.yaw, cfg)
        ask(f"把抽屉拉到固定位置 drawer_ref（{args.drawer_ref}），取出碗，确认 home 标记可见，回车继续：")
        gx, gy = cfg["bowl_guess_xy"]
        mover.execute(mover.go_hover([gx, gy], args.yaw, cfg["bowl_hover_z"])[0])
        print("用点动把夹爪中心对准 home 标记正上方：")
        a = mover.jog(ask, quat)
        mover.execute(mover.plan_route([([a[0], a[1], cfg["approach_z"]], quat)]))
        ask("机械臂已抬起。把抽屉往里推约 10 cm（不要推到底），回车继续：")
        mover.execute(mover.plan_route([([a[0], a[1], a[2]], quat)]))
        print("再次用点动对准同一个 home 标记：")
        b = mover.jog(ask, quat)
        calib = calibration_from_points(a, b, args.drawer_ref, {
            "backend": backend, "yaw_deg": args.yaw, "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "script_sha256": sha256(__file__)})
        mover.retract_home()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(calib, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(calib, ensure_ascii=False, indent=1))
        print(f"已写入 {out}（sha256 {sha256(out)}）。把抽屉推回训练初始状态。")
        return calib
    finally:
        mover.close()


def plan_condition(mover, row, cfg, calib):
    """从起始位姿出发规划物体（和碗）的整段路径，不运动。"""
    yaw = float(row["grasp_yaw_deg"])
    start = (np.asarray(mover.start_pose[0]), mover.start_pose[1])
    obj_z = cfg["grasp_z"] + cfg["hover_dz"]
    obj = [float(row["eef_x_m"]), float(row["eef_y_m"])]
    quat = hover_quat(yaw, cfg)
    result = {"object_target": [obj[0], obj[1], obj_z]}
    result["object_plans"] = mover.plan_route([([obj[0], obj[1], cfg["approach_z"]], quat), ([obj[0], obj[1], obj_z], quat)],
                                              seed=mover.start_q, start=start)
    if needs_bowl_arm(row):
        if calib is None:
            raise Unreachable("需要碗标定文件（--bowl-calibration）")
        bt = bowl_target(calib, float(row["bowl_dx_cm"]), float(row["bowl_dy_cm"]))
        bquat = hover_quat(float(calib.get("yaw_deg", yaw)), cfg)
        result["bowl_target"] = bt
        result["bowl_plans"] = mover.plan_route([([bt[0], bt[1], cfg["approach_z"]], bquat), (bt, bquat)],
                                                seed=mover.start_q, start=start)
    return result


def cmd_check(args):
    cfg = load_cfg(args)
    rows = select_conditions(args.conditions, args.take, args.cond)
    calib = json.loads(Path(args.bowl_calibration).read_text(encoding="utf-8")) if args.bowl_calibration else None
    mover = make_mover(args, cfg)
    report = []
    try:
        mover.connect()
        p, q, _ = mover.pose()
        mover.start_pose = (p, q.tolist())
        for row in rows:
            rec = {"cond_id": row["cond_id"], "level": row["level"]}
            try:
                plan = plan_condition(mover, row, cfg, calib)
                rec.update(status="reachable", object_target=plan["object_target"], bowl_target=plan.get("bowl_target"))
            except Unreachable as exc:
                rec.update(status="unreachable", reason=str(exc))
            report.append(rec)
            print(f"{rec['cond_id']} {rec['level']}: {rec['status']}" + (f"（{rec['reason']}）" if "reason" in rec else ""))
    finally:
        mover.close()
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"conditions": str(args.conditions), "conditions_sha256": sha256(args.conditions),
                                   "bowl_calibration_sha256": sha256(args.bowl_calibration) if args.bowl_calibration else None,
                                   "script_sha256": sha256(__file__), "params": cfg, "results": report},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def cmd_place(args, ask=input, check_fn=None):
    cfg = load_cfg(args)
    rows = select_conditions(args.conditions, args.take, args.cond)
    calib = json.loads(Path(args.bowl_calibration).read_text(encoding="utf-8")) if args.bowl_calibration else None
    if args.no_arm and not (args.vision or check_fn):
        raise SystemExit("--no-arm 需要 --vision 和 --cameras，才能对照图像摆放")
    if not args.no_arm and any(needs_bowl_arm(r) for r in rows) and calib is None:
        raise SystemExit("所选条件含碗偏移，机械臂定位需要 --bowl-calibration（或改用 --no-arm 按尺子摆碗）")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log = out / "placement_log.jsonl"
    refs = References(args.cameras)
    vision_sha = None
    if args.vision and check_fn is None:
        ev = load_vision_module()
        vision = ev.Vision(args.vision)
        vision_sha = vision.sha256
        cam = refs.get(vision.meta["camera"])
        if cam is None:
            refs.close()
            raise SystemExit(f"--cameras 中没有对照用相机 {vision.meta['camera']!r}（需先关闭 A 网页端以释放相机）")

        def check_fn(split, target, title):
            return ev.interactive_check(cam, vision, split, target, title)
    mover = None if args.no_arm else make_mover(args, cfg)
    backend = "manual"
    done = []
    try:
        if mover:
            backend = mover.connect()
            p, q, _ = mover.pose()
            mover.start_pose = (p, q.tolist())
        for k, row in enumerate(rows, 1):
            cid = row["cond_id"]
            split = row.get("split") or ("ID" if row["level"].startswith("id_") else "OOD")
            target = [float(row["eef_x_m"]), float(row["eef_y_m"])]
            rec = OrderedDict(cond_id=cid, level=row["level"], split=split, mode="manual" if args.no_arm else "arm",
                              backend=backend, started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            print(f"\n[{k}/{len(rows)}] {cid} {row['level']}  物体 ({row['eef_x_m']}, {row['eef_y_m']}) 偏航 {row['grasp_yaw_deg']}°"
                  f"  碗偏移 dx={row.get('bowl_dx_cm')} dy={row.get('bowl_dy_cm')} cm")
            if ask("回车开始本条，s 跳过：").strip() == "s":
                rec["status"] = "skipped"
                log_line(log, rec)
                continue
            if mover:
                try:
                    plan = plan_condition(mover, row, cfg, calib)
                except Unreachable as exc:
                    rec.update(status="unreachable", reason=str(exc))
                    print(f"  不可达：{exc}")
                    log_line(log, rec)
                    continue
                try:
                    if "bowl_plans" in plan:
                        ask(f"  把抽屉拉到 drawer_ref（{calib.get('drawer_ref')}），回车后机械臂移到碗目标上方：")
                        rec["bowl_error_m"] = mover.execute(plan["bowl_plans"])
                        ask("  把碗中心放在夹爪正下方，回车后机械臂退回：")
                        mover.retract_home()
                        ask("  把抽屉推回训练初始状态，回车继续：")
                        rec["bowl_target"] = plan["bowl_target"]
                    else:
                        ask("  碗放在 home 标记（训练位置），抽屉保持训练初始状态，回车继续：")
                    rec["object_error_m"] = mover.execute(mover.plan_route(
                        [(pl["end_p"], pl["end_q"]) for pl in plan["object_plans"]]))
                    rec["object_target"] = plan["object_target"]
                    ask(f"  把物体放在两指正中、对齐夹爪朝向（{row['grasp_yaw_deg']}°，36° 周期任一等价朝向），回车后机械臂退回：")
                    mover.retract_home()
                except Unreachable as exc:
                    rec.update(status="failed_midway", reason=str(exc))
                    print(f"  执行中规划失败：{exc}，退回起始位")
                    mover.retract_home()
                    log_line(log, rec)
                    continue
            else:
                if needs_bowl_arm(row):
                    ask(f"  按尺子把碗放到 home 偏移 dx={row['bowl_dx_cm']} cm（沿拉出方向）、dy={row['bowl_dy_cm']} cm"
                        "（面向抽屉向右），再把抽屉推回训练初始状态，回车继续：")
                else:
                    ask("  碗放在 home 标记（训练位置），抽屉保持训练初始状态，回车继续：")
                rec["object_target"] = target
                print("  在对照窗口中摆放物体：品红圈是估计目标（约 ±2 cm，仅作引导），以窗口判定为准。")
            if check_fn:
                if not args.no_arm:
                    print("  对照窗口：确认判定通过；不通过时可直接微调物体，直到通过。")
                result, img, action = check_fn(split, target, f"{cid} {row['level']}")
                rec["vision"], rec["vision_action"], rec["vision_sha256"] = result, action, vision_sha
                if img is not None:
                    import cv2
                    (out / "reference").mkdir(parents=True, exist_ok=True)
                    path = out / "reference" / f"{cid}_diff.png"
                    cv2.imwrite(str(path), img)
                    rec["diff_image"] = {"path": path.name, "sha256": sha256(path)}
                if action == "skip":
                    rec["status"] = "skipped_at_vision"
                    log_line(log, rec)
                    print("  已在对照窗口跳过本条")
                    continue
                if action == "force":
                    print(f"  注意：判定未通过仍强制确认（{(result or {}).get('reason')}），已记录")
            rec["references"] = refs.save(out / "reference", cid)
            rec["status"] = "placed"
            rec["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            log_line(log, rec)
            done.append(cid)
            msg = "  完成"
            if "object_error_m" in rec:
                msg += f"，机械臂到位误差 物体 {rec['object_error_m']}" + (f"、碗 {rec['bowl_error_m']}" if "bowl_error_m" in rec else "")
            if rec.get("vision"):
                msg += f"；图像判定 {'通过' if rec['vision'].get('verdict') else '未通过'}（{rec['vision'].get('reason')}）"
            print(msg)
    except KeyboardInterrupt:
        print("\n已中断" + ("，B 已 hold。" if mover else "。"), file=sys.stderr)
        log_line(log, {"status": "aborted", "after": done})
    finally:
        if mover:
            mover.close()
        refs.close()
    print(f"\n摆放完成 {len(done)} / {len(rows)}，记录：{log}")
    return done


def cmd_build_vision(args):
    ev = load_vision_module()
    out = Path(args.out)
    if (out / "vision.json").exists() and not args.force:
        raise SystemExit(f"{out / 'vision.json'} 已存在；重建请加 --force")
    meta = ev.build(args.data_root, args.grasp_poses, out, args.camera)
    print(json.dumps({k: meta[k] for k in ("camera", "star_diameter_px", "px_per_cm_at_centre", "ood_margin_px", "homography_loo_cm")},
                     ensure_ascii=False, indent=1))
    print(f"已写入 {out}（vision.json sha256 {sha256(out / 'vision.json')}）")
    return meta


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--operator-config", help="读取 robot_host / 端口（与 A 的 operator.json 相同）")
    common.add_argument("--host")
    common.add_argument("--control-port", type=int)
    common.add_argument("--ik-port", type=int)
    common.add_argument("--speed", type=float, help="直线速度 m/s（默认 0.05，上限 0.10）")
    common.add_argument("--allow-real", action="store_true", help="允许真机 Intera 后端（须已获授权）")
    common.add_argument("--allow-fake", action="store_true", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("calibrate-bowl", parents=[common], help="碗定位标定")
    p.add_argument("--out", required=True)
    p.add_argument("--drawer-ref", required=True, help="drawer_ref 的文字描述，如 '拉到底限位'")
    p.add_argument("--yaw", type=float, default=71.7, help="标定时夹爪偏航（度）")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_calibrate)
    for name, func, helptext in (("check", cmd_check, "只规划不运动，检查可达性"), ("place", cmd_place, "逐条摆放")):
        p = sub.add_parser(name, parents=[common], help=helptext)
        p.add_argument("--conditions", required=True)
        p.add_argument("--take", help="如 id_unseen:10,obj_bowl_pos:10")
        p.add_argument("--cond", action="append", help="指定条件 ID（可重复）")
        p.add_argument("--bowl-calibration")
        if name == "check":
            p.add_argument("--out", help="保存检查结果 JSON")
        else:
            p.add_argument("--out", required=True, help="评测目录：写 placement_log.jsonl 和 reference/")
            p.add_argument("--cameras", help="operator.json：从其中 cameras 打开相机保存参考图（需先关闭 A 网页端）")
            p.add_argument("--vision", help="build-vision 生成的目录：打开实时对照窗口判定 ID / OOD 并保存 diff 图")
            p.add_argument("--no-arm", action="store_true", help="不连机械臂：碗按尺子量偏移，物体对照窗口手动摆放")
        p.set_defaults(func=func)
    p = sub.add_parser("build-vision", help="由训练录制首帧生成图像对照参考")
    p.add_argument("--data-root", required=True, help="训练录制根目录（含 batch_* 子目录）")
    p.add_argument("--grasp-poses", required=True, help="train-distribution 的 grasp_poses.json")
    p.add_argument("--out", required=True)
    p.add_argument("--camera", default="realsense")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_build_vision)
    return ap


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    main()
