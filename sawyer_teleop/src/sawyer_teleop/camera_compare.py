"""Standalone pre-inference RGB comparison with no robot command interface."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import shlex
import struct
import subprocess
import sys
import tarfile
import threading
import time

import cv2
import numpy as np
from PIL import Image

ASSETS = Path(__file__).with_name('camera_compare_assets')
WINDOWS = sys.platform == 'win32'
HPC_PROJECT = '/scratch/prj/eng_demt_robot_learning'
DATASET = HPC_PROJECT + '/sawyer_finray_pi05_state7_alltrain_v1_2026-11-12/train'
DATASETS = {
    DATASET: 'VLA State7 · Alltrain (11-12)',
    HPC_PROJECT + '/sawyer_finray_pi05_eef_language_v2_2026-11-12/train': 'VLA EEF Language v2 · Train (11-12)',
    HPC_PROJECT + '/sawyer_finray_pi05_eef_v1_2026-11-11/train': 'VLA EEF v1 · Train (11-11)',
}
FRAME_MODES = {'pose': '按实测姿态匹配', 'first': '首帧', 'last': '末帧'}
ALL_DEMOS = '全部示范'
MAIN = '/dev/v4l/by-id/usb-Intel_R__RealSense_TM__Depth_Camera_435_Intel_R__RealSense_TM__Depth_Camera_435_345423020748-video-index0'
WRIST = '/dev/v4l/by-id/usb-0c45_USB20_Camera-video-index0'
WINDOWS_MAIN = 'Intel(R) RealSense(TM) Depth Camera 435 with RGB Module RGB'
WINDOWS_WRIST = 'sn9c20x'
RESIZE_SHA = 'd48b4bd7f44e79fe6db8a8e07c9161144fa250be686e1245014a8b47e6171977'


def write_rgb(path, rgb):
    # OpenCV's filename API cannot reliably open Unicode paths on Windows.
    ok, encoded = cv2.imencode('.png', cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    if not ok:
        raise OSError('Cannot save ' + str(path))
    path.write_bytes(encoded.tobytes())


def pose_distance(states, pose):
    """Euclidean position + geodesic SO(3), never subtract rotation-vector components."""
    from scipy.spatial.transform import Rotation
    states = np.asarray(states, dtype=float)
    p = pose['position']; q = pose['orientation']
    xyz = np.array([p[k] for k in ('x', 'y', 'z')])
    rotation = Rotation.from_quat([q[k] for k in ('x', 'y', 'z', 'w')])
    dp = np.linalg.norm(states[:, :3] - xyz, axis=1)
    da = (Rotation.from_rotvec(states[:, 3:6]) * rotation.inv()).magnitude()
    score = (dp / .020) ** 2 + (da / np.deg2rad(5)) ** 2
    return dp, np.rad2deg(da), score


def pixel_metrics(reference, current):
    if reference.shape != (224, 224, 3) or current.shape != reference.shape:
        raise ValueError('Expected RGB uint8 224x224')
    # Mask known geometric padding only. Real black objects are retained.
    delta = current[28:196].astype(np.float64) - reference[28:196].astype(np.float64)
    absolute = np.abs(delta)
    return {'mae': float(absolute.mean()), 'rmse': float(np.sqrt(np.mean(delta ** 2))),
            'reference_gray_mean': float(cv2.cvtColor(reference[28:196], cv2.COLOR_RGB2GRAY).mean()),
            'current_gray_mean': float(cv2.cvtColor(current[28:196], cv2.COLOR_RGB2GRAY).mean()),
            'mae_rgb': absolute.mean((0, 1)).tolist(),
            'rmse_rgb': np.sqrt((delta ** 2).mean((0, 1))).tolist()}, absolute.mean(2)


def feature_displacement(reference, current, static_main=False):
    """Mutual SIFT/Lowe + unique locations + RANSAC; no image warp is performed."""
    a, b = reference[28:196], current[28:196]
    mask = np.zeros(a.shape[:2], np.uint8) if static_main else None
    if static_main:
        mask[:, 130:220] = 255  # cabinet side and wall; excludes arm, object and drawer
    sift = cv2.SIFT_create(nfeatures=1500, contrastThreshold=.01)
    k1, d1 = sift.detectAndCompute(cv2.cvtColor(a, cv2.COLOR_RGB2GRAY), mask)
    k2, d2 = sift.detectAndCompute(cv2.cvtColor(b, cv2.COLOR_RGB2GRAY), mask)
    result = dict(keypoints=[len(k1), len(k2)], mutual_matches=0, unique_matches=0,
                  inliers=0, confidence='insufficient', scope='static right cabinet/wall' if static_main else 'full content')
    if d1 is None or d2 is None or min(len(d1), len(d2)) < 2:
        return result
    matcher = cv2.BFMatcher()
    def ratios(x, y):
        return {pair[0].queryIdx: pair[0] for pair in matcher.knnMatch(x, y, k=2)
                if len(pair) == 2 and pair[0].distance < .7 * pair[1].distance}
    forward, reverse = ratios(d1, d2), ratios(d2, d1)
    matches = [m for m in forward.values() if m.trainIdx in reverse and reverse[m.trainIdx].trainIdx == m.queryIdx]
    result['mutual_matches'] = len(matches)
    # SIFT can assign several orientations to the same location; count it once.
    unique = []
    for m in sorted(matches, key=lambda x: x.distance):
        if all(np.linalg.norm(np.array(k1[m.queryIdx].pt) - k1[n.queryIdx].pt) >= 1.5
               and np.linalg.norm(np.array(k2[m.trainIdx].pt) - k2[n.trainIdx].pt) >= 1.5 for n in unique):
            unique.append(m)
    result['unique_matches'] = len(unique)
    if len(unique) < 4:
        return result
    p = np.float32([k1[m.queryIdx].pt for m in unique])
    q = np.float32([k2[m.trainIdx].pt for m in unique])
    cv2.setRNGSeed(0)
    matrix, inliers = cv2.estimateAffinePartial2D(p, q, method=cv2.RANSAC, ransacReprojThreshold=2,
                                               maxIters=5000, confidence=.999, refineIters=20)
    if matrix is None:
        return result
    ii = inliers.ravel().astype(bool)
    coverage = cv2.contourArea(cv2.convexHull(p[ii])) / (224 * 168) if ii.sum() >= 3 else 0
    residual = np.linalg.norm(p[ii] @ matrix[:, :2].T + matrix[:, 2] - q[ii], axis=1)
    confidence = 'medium' if ii.sum() >= 10 and ii.mean() >= .65 and coverage >= .08 else 'low'
    # A local cluster is not a reliable global displacement, even at low residual.
    result.update(inliers=int(ii.sum()), inlier_ratio=float(ii.mean()), hull_coverage=float(coverage),
                  median_residual_px=float(np.median(residual)), confidence=confidence,
                  candidate_median_xy_px=np.median(q[ii] - p[ii], axis=0).tolist(),
                  displacement_xy_px=np.median(q[ii] - p[ii], axis=0).tolist() if confidence == 'medium' else None,
                  image_plane_rotation_deg=float(np.degrees(np.arctan2(matrix[1, 0], matrix[0, 0]))),
                  points=[{'reference_xy': (x + [0, 28]).tolist(), 'current_xy': (y + [0, 28]).tolist(), 'inlier': bool(i)}
                          for x, y, i in zip(p, q, ii)])
    return result


class References:
    def __init__(self, cache, host, python, dataset):
        self.host, self.python, self.dataset = host, python, dataset
        self.cache = cache if dataset == DATASET else cache / hashlib.sha256(dataset.encode()).hexdigest()[:24]
        self.cache.mkdir(parents=True, exist_ok=True)
        self.process_lock = threading.Lock()
        self.active_process = None
        self.closing = False
        self.loaded = False

    def fetch(self, action, *args):
        helper = (ASSETS / 'reference.py').read_bytes()
        command = shlex.join([self.python, '-', action, self.dataset, *map(str, args)])
        with self.process_lock:
            if self.closing: raise RuntimeError('Reference reader closed')
            try:
                run = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=12', self.host, command],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except FileNotFoundError as exc:
                raise RuntimeError('OpenSSH client (ssh) is required to fetch uncached training references') from exc
            self.active_process = run
        try:
            output, errors = run.communicate(input=helper, timeout=100)
        except subprocess.TimeoutExpired:
            run.kill(); run.communicate()
            raise RuntimeError('Read-only reference fetch timed out')
        finally:
            with self.process_lock: self.active_process = None
        if run.returncode:
            raise RuntimeError(errors.decode(errors='replace')[-2000:])
        with tarfile.open(fileobj=io.BytesIO(output)) as archive:
            for member in archive.getmembers():
                if not member.isfile() or Path(member.name).is_absolute() or '..' in Path(member.name).parts:
                    raise ValueError('Unsafe reference archive')
                target = self.cache / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())

    def close(self):
        with self.process_lock:
            self.closing = True
            if self.active_process is not None and self.active_process.poll() is None:
                self.active_process.terminate()

    def load_index(self):
        if not (self.cache / 'pose_index.json').exists():
            self.fetch('index')
        index = json.loads((self.cache / 'pose_index.json').read_text(encoding='utf-8'))
        if index['dataset'] != self.dataset:
            self.fetch('index')
            index = json.loads((self.cache / 'pose_index.json').read_text(encoding='utf-8'))
        if index['dataset'] != self.dataset:
            raise ValueError('Reference cache belongs to a different dataset')
        self.resolved_dataset = index.get('resolved_dataset', self.dataset)
        self.rows = index['rows']
        info = json.loads((self.cache / 'info.json').read_text(encoding='utf-8'))
        contract = json.loads((self.cache / 'contract.json').read_text(encoding='utf-8'))
        if (not contract.get('pose_frame', '').startswith('base/right_hand')
                or any(info['features'][key]['shape'] != [224, 224, 3] for key in ('image', 'wrist_image'))
                or 'openpi_client.image_tools.resize_with_pad' not in contract.get('image_processing', '')):
            raise ValueError('Dataset needs Sawyer base/right_hand pose and OpenPI RGB224 images')
        names = info['features']['state']['names']
        if not self.rows:
            raise ValueError('Dataset pose index is empty')
        state = np.asarray([row['state'] for row in self.rows], dtype=float)
        xyz = state[:, [names.index(f'right_hand_{axis}_m') for axis in 'xyz']]
        rotvec = [f'right_hand_rotvec_{axis}_rad' for axis in 'xyz']
        if all(name in names for name in rotvec):
            rotation = state[:, [names.index(name) for name in rotvec]]
        else:
            from scipy.spatial.transform import Rotation
            quaternion = state[:, [names.index(name) for name in ('qx', 'qy', 'qz', 'qw')]]
            rotation = Rotation.from_quat(quaternion).as_rotvec()
        self.states = np.column_stack((xyz, rotation))
        if not np.isfinite(self.states).all():
            raise ValueError('Dataset pose index is invalid')
        mapping = json.loads((self.cache / 'source_mapping.json').read_text(encoding='utf-8'))
        self.demos = {number: member['episode'] for number, member in enumerate(contract['members'], 1)}
        numbers = {source: number for number, source in self.demos.items()}
        segments = {segment['episode_index']: segment for segment in mapping['segments']}
        self.demo_rows = {number: [] for number in self.demos}
        self.pose_by_frame = {}
        self.demo_by_frame = {}
        for row_index, (row, pose_state) in enumerate(zip(self.rows, self.states)):
            episode, frame = row['episode_index'], row['frame_index']
            segment = segments[episode]
            if row['source_episode_index'] != segment['source_index']:
                raise ValueError(f'Source demonstration mismatch for segment {episode}')
            demo = numbers[segment['source_episode']]
            order = segment['numeric_start'] + frame
            self.demo_rows[demo].append((order, row_index))
            self.pose_by_frame[episode, frame] = pose_state.tolist()
            self.demo_by_frame[episode, frame] = demo
        for demo, rows in self.demo_rows.items():
            self.demo_rows[demo] = np.array([index for _, index in sorted(rows)], dtype=int)
        source = self.cache / 'pinned_image_tools.py'
        if hashlib.sha256(source.read_bytes()).hexdigest() != RESIZE_SHA:
            raise ValueError('Training resize implementation hash mismatch')
        spec = importlib.util.spec_from_file_location('_camera_compare_pinned_resize', source)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.resize = module.resize_with_pad
        self.loaded = True

    def select(self, mode, demo, pose):
        candidates = self.demo_rows[demo] if demo is not None else None
        if candidates is not None and not candidates.size:
            return None
        if mode != 'pose':
            index = candidates[0 if mode == 'first' else -1]
        else:
            states = self.states if candidates is None else self.states[candidates]
            _, _, score = pose_distance(states, pose)
            nearest = np.argmin(score)
            index = nearest if candidates is None else candidates[nearest]
        row = self.rows[index]
        return row['episode_index'], row['frame_index']

    def images(self, episode, frame):
        prefix = 'ep%03d_frame%04d' % (episode, frame)
        if not (self.cache / (prefix + '.json')).exists():
            self.fetch('frame', episode, frame)
        meta = json.loads((self.cache / (prefix + '.json')).read_text(encoding='utf-8'))
        if (meta['dataset'] not in (self.dataset, self.resolved_dataset)
                or (meta['episode_index'], meta['frame_index']) != (episode, frame)):
            raise ValueError('Reference metadata does not match requested frame')
        meta.update(dataset=self.dataset, pose_state_xyz_rotvec=self.pose_by_frame[episode, frame])
        demo = self.demo_by_frame[episode, frame]
        meta.update(demo_number=demo, source_episode=self.demos[demo])
        result = {}
        for role, key in [('main', 'image'), ('wrist', 'wrist_image')]:
            path = self.cache / (prefix + '_' + key + '.png')
            if hashlib.sha256(path.read_bytes()).hexdigest() != meta['images'][key]['sha256']:
                raise ValueError('Reference PNG hash mismatch')
            rgb = np.array(Image.open(path).convert('RGB'))
            if rgb.shape != (224, 224, 3) or rgb[:28].any() or rgb[196:].any():
                raise ValueError('Training PNG differs from expected RGB224 letterbox')
            result[role] = rgb
        return result, meta


class RosReadOnly:
    def __init__(self, main_topic=None, wrist_topic=None):
        import rospy
        from sensor_msgs.msg import JointState, Image as RosImage
        from intera_core_msgs.msg import EndpointState
        self.rospy = rospy
        rospy.init_node('camera_compare_readonly', anonymous=True, disable_signals=True)
        self.records, self.images, self.lock = {}, {}, threading.Lock()
        self.subscribers = []
        def plain(v):
            if hasattr(v, '__slots__'): return {k: plain(getattr(v, k)) for k in v.__slots__}
            if isinstance(v, (list, tuple)): return [plain(x) for x in v]
            return v
        def receive(msg, key):
            with self.lock:
                self.records[key] = {'received_monotonic_ns': time.monotonic_ns(), 'received_utc_ns': time.time_ns(), 'message': plain(msg)}
        for topic, cls, key in [('/robot/limb/right/endpoint_state', EndpointState, 'endpoint'),
                                ('/robot/joint_states', JointState, 'joints')]:
            self.subscribers.append(rospy.Subscriber(topic, cls, receive, callback_args=key, queue_size=1))
        def image(msg, role):
            if msg.encoding not in ('rgb8', 'bgr8'):
                return  # no silent guessing about image encoding
            rgb = np.frombuffer(msg.data, np.uint8).reshape(msg.height, msg.step)[:, :msg.width * 3].reshape(msg.height, msg.width, 3).copy()
            if msg.encoding == 'bgr8': rgb = rgb[:, :, ::-1].copy()
            meta = {'received_monotonic_ns': time.monotonic_ns(), 'received_utc_ns': time.time_ns(),
                    'ros_header': plain(msg.header), 'topic': main_topic if role == 'main' else wrist_topic,
                    'encoding': msg.encoding, 'sensor_exposure_time_known': False}
            with self.lock: self.images[role] = (rgb, meta)
        for role, topic in [('main', main_topic), ('wrist', wrist_topic)]:
            if topic:
                self.subscribers.append(rospy.Subscriber(topic, RosImage, image, callback_args=role,
                                                         queue_size=1, buff_size=2000000))

    def snapshot(self):
        with self.lock:
            return dict(self.records), dict(self.images)

    def close(self):
        for subscriber in self.subscribers: subscriber.unregister()
        self.rospy.signal_shutdown('comparison closed')


def check_camera_owner(device):
    """Check visible owners before the driver's exclusive streaming check."""
    identity = os.stat(device)
    for process in Path('/proc').iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            for handle in (process / 'fd').iterdir():
                try:
                    candidate = handle.stat()
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
                if (identity.st_dev, identity.st_ino) == (candidate.st_dev, candidate.st_ino):
                    raise RuntimeError(f'Camera {device} is already open by PID {process.name}')
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue


class NativeCamera:
    def __init__(self, device, directory, executable):
        check_camera_owner(device)
        self.directory = directory; directory.mkdir()
        self.output = (directory / 'capture.log').open('w')
        self.process = subprocess.Popen([str(executable), device, str(directory), '--continuous'],
                                        stdout=self.output, stderr=subprocess.STDOUT)

    def latest(self):
        if self.process.poll() is not None:
            raise RuntimeError('Camera stopped: ' + (self.directory / 'capture.log').read_text())
        try:
            data = (self.directory / 'current.raw').read_bytes()
            sequence, mono, utc, device_ns, flags = struct.unpack('=5Q', data[:40])
            fmt = json.loads((self.directory / 'format.json').read_text())
        except FileNotFoundError:
            raise RuntimeError('Waiting for camera')
        a = np.frombuffer(data, np.uint8, offset=40); h, w = fmt['height'], fmt['width']
        if fmt['fourcc'] == 'YUYV': rgb = cv2.cvtColor(a.reshape(h, w, 2), cv2.COLOR_YUV2RGB_YUY2)
        elif fmt['fourcc'] == 'BA81': rgb = cv2.cvtColor(a.reshape(h, w), cv2.COLOR_BayerBGGR2RGB_EA)
        else: raise RuntimeError('Unsupported unchanged native camera format: ' + fmt['fourcc'])
        return rgb, dict(sequence=sequence, received_monotonic_ns=mono, received_utc_ns=utc,
                         device_timestamp_ns=device_ns, device_flags=flags, format=fmt,
                         sensor_exposure_time_known=False)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try: self.process.wait(timeout=3)
            except subprocess.TimeoutExpired: self.process.kill(); self.process.wait()
        self.output.close()


class WindowsCamera:
    def __init__(self, role, device, backend):
        from .cameras import Camera
        self.device, self.backend = device, backend
        self.camera = Camera(name=role, device=device, backend=backend).start()

    def latest(self):
        frame = self.camera.latest()
        # The shared Windows camera uses perf_counter_ns(), not monotonic_ns().
        age = time.perf_counter_ns() - frame['ns']
        return frame['rgb'], dict(sequence=frame['seq'], received_monotonic_ns=frame['ns'],
                                  received_utc_ns=time.time_ns() - age,
                                  clock='perf_counter_ns', backend=self.backend, device=self.device,
                                  sensor_exposure_time_known=False)

    def close(self):
        self.camera.close()


def text(canvas, message, x, y, color=(230, 230, 230), scale=.5):
    cv2.putText(canvas, message, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1, cv2.LINE_AA)


class CompareWindow:
    def __init__(self, datasets, dataset, mode, demo, frame_modes=FRAME_MODES):
        import tkinter as tk
        from tkinter import ttk
        from PIL import ImageTk

        self.image_type = ImageTk.PhotoImage
        self.root = tk.Tk()
        self.root.title('相机对比 · 训练图 / 实时图 / 叠加 / Diff')
        self.root.geometry(f'{min(1240, self.root.winfo_screenwidth())}x{min(838, self.root.winfo_screenheight() - 70)}')
        self.datasets = {label: path for path, label in datasets.items()}
        self.modes = {label: key for key, label in frame_modes.items()}
        self.closed = False
        self.save_requested = False
        self.freeze_requested = False
        self.demo_options = None
        bar = ttk.Frame(self.root, padding=(12, 8))
        bar.pack(fill='x')
        ttk.Label(bar, text='参考数据集').pack(side='left', padx=(0, 5))
        self.dataset = ttk.Combobox(bar, values=list(self.datasets), width=40, state='readonly')
        self.dataset.set(datasets[dataset])
        self.dataset.pack(side='left', padx=(0, 14))
        ttk.Label(bar, text='参考帧').pack(side='left', padx=(0, 5))
        self.mode = ttk.Combobox(bar, values=list(self.modes), width=18, state='readonly')
        self.mode.set(frame_modes[mode])
        self.mode.pack(side='left', padx=(0, 14))
        ttk.Label(bar, text='原始示范').pack(side='left', padx=(0, 5))
        self.demo = ttk.Combobox(bar, width=26, state='disabled')
        self.demo.set(ALL_DEMOS if demo is None else str(demo))
        self.demo.pack(side='left', padx=(0, 14))
        ttk.Button(bar, text='保存截图', command=self.request_save).pack(side='left')
        self.preview = tk.Label(self.root, bg='#181818', borderwidth=0)
        self.preview.pack(fill='both', expand=True)
        self.root.bind('<Escape>', self.request_close)
        self.root.bind('<KeyPress-q>', self.request_close)
        self.root.bind('<KeyPress-s>', self.request_save)
        self.root.bind('<space>', self.request_freeze)
        self.root.protocol('WM_DELETE_WINDOW', self.request_close)

    def request_close(self, event=None):
        self.closed = True
        return 'break'

    def request_save(self, event=None):
        self.save_requested = True
        return 'break'

    def request_freeze(self, event=None):
        self.freeze_requested = True
        return 'break'

    def choices(self):
        selected = self.demo.get()
        demo = None if selected == ALL_DEMOS else int(selected.split()[0])
        return self.datasets[self.dataset.get()], self.modes[self.mode.get()], demo

    def set_demos(self, refs, mode):
        if not refs.loaded:
            self.demo.configure(state='disabled')
            self.demo_options = None
            return
        if (refs.dataset, mode) == self.demo_options:
            return
        self.demo_options = refs.dataset, mode
        options = {}
        for number, source in refs.demos.items():
            unavailable = ' · 当前分区无帧' if not refs.demo_rows[number].size else ''
            timestamp = source.partition('_')[2].partition('_')[0]
            recorded_time = source
            if len(timestamp) == 15 and timestamp[8] == 'T':
                recorded_time = f'{timestamp[4:6]}-{timestamp[6:8]} {timestamp[9:11]}:{timestamp[11:13]}:{timestamp[13:15]}'
            options[number] = f'{number:02d} · {recorded_time}{unavailable}'
        default = None if mode == 'pose' else next(
            (number for number, rows in refs.demo_rows.items() if rows.size), None)
        if mode == 'pose':
            options = {None: ALL_DEMOS, **options}
        selected = self.choices()[2]
        self.demo.configure(values=list(options.values()), state='readonly')
        self.demo.set(options[selected if selected in options else default])

    def show(self, canvas):
        rgb = Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
        if self.preview.winfo_width() > 1 and self.preview.winfo_height() > 1:
            rgb.thumbnail((self.preview.winfo_width(), self.preview.winfo_height()), Image.Resampling.BILINEAR)
        self.photo = self.image_type(rgb)
        self.preview.configure(image=self.photo)


def run(args):
    datasets = dict(DATASETS)
    datasets.setdefault(args.dataset, args.dataset)
    readers = {path: References(args.reference_cache, args.ssh_host, args.hpc_python, path) for path in datasets}
    initial_refs = readers[args.dataset]
    out = args.output / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    ros = None
    cameras = {}; executor = ThreadPoolExecutor(max_workers=1)
    future = None; reference = None; requested = None; features = {}; last_feature = 0
    frozen = False; last_frame = None; saved = 0; last_pose_select = 0
    status = 'Waiting for training reference'; selected = None; previous_choice = None
    window = None
    camera_clock = time.perf_counter_ns if WINDOWS else time.monotonic_ns
    seen_cameras = set()
    camera_errors = {}
    try:
        print('Loading actual training pose index (first run uses read-only SSH)...', flush=True)
        initial_refs.load_index()
        if args.demo is not None and args.demo not in initial_refs.demos:
            raise ValueError(f'Original demo {args.demo} is unknown in {args.dataset}')
        # Run a bit-exact RGB resize gate against outputs produced on pinned HPC Pillow11.2.1.
        resize = initial_refs.resize
        verify_resize(resize)
        out.mkdir(parents=True)
        if WINDOWS:
            from .cameras import resolve_device
            main_device = resolve_device(args.main_device, 'dshow')
            if args.wrist_device == WINDOWS_WRIST:
                wrist_device = args.wrist_device, 'sn9c20x'
            else:
                wrist_device = resolve_device(args.wrist_device, 'dshow')
            if main_device == wrist_device:
                raise ValueError('Main and wrist must use different cameras')
            cameras['main'] = WindowsCamera('main', *main_device)
            cameras['wrist'] = WindowsCamera('wrist', *wrist_device)
        else:
            ros = RosReadOnly(args.main_topic, args.wrist_topic)
        if ros is not None and not (args.main_topic and args.wrist_topic):
            devices = [('main', args.main_device, args.main_topic), ('wrist', args.wrist_device, args.wrist_topic)]
            executable = out / 'capture_native'
            subprocess.run(['gcc', '-O2', '-Wall', str(ASSETS / 'capture.c'), '-o', str(executable)], check=True)
            # Discover streaming ROS publishers before opening USB devices. Explicit role topics take priority.
            import rosgraph
            publishers = rosgraph.Master('/camera_compare_readonly').getPublishedTopics('')
            external = [t for t, kind in publishers if kind in ('sensor_msgs/Image', 'sensor_msgs/CompressedImage')
                        and any(s in t.lower() for s in ('realsense', 'wrist', 'd435'))
                        and not t.startswith('/io/internal_camera/')]
            if external:
                raise RuntimeError('Existing external camera topics found; rerun with --main-topic/--wrist-topic to reuse them: ' + ', '.join(external))
            for role, device, topic in devices:
                if not topic: cameras[role] = NativeCamera(device, out / role, executable)
        start = time.monotonic(); frames = 0
        if not args.headless:
            window = CompareWindow(datasets, args.dataset, args.frame_mode, args.demo, frame_modes())
        while args.seconds is None or time.monotonic() - start < args.seconds:
            if window:
                window.root.update()
                if window.closed:
                    break
                dataset, mode, demo = window.choices()
            else:
                dataset, mode, demo = args.dataset, args.frame_mode, args.demo
            refs = readers[dataset]
            if window:
                window.set_demos(refs, mode)
                _, _, demo = window.choices()
            elif mode != 'pose' and demo is None:
                demo = next(number for number, rows in refs.demo_rows.items() if rows.size)
            choice = dataset, mode, demo
            if choice != previous_choice:
                previous_choice = choice
                reference = None; requested = None; selected = None; features = {}
                frozen = False; last_pose_select = 0
                status = 'Waiting for training reference'
            if window and window.freeze_requested:
                frozen = not frozen if mode == 'pose' and reference else False
                window.freeze_requested = False

            now = time.monotonic()
            records, topic_images = ros.snapshot() if ros is not None else ({}, {})
            endpoint = records.get('endpoint')
            valid_pose = (endpoint is not None and endpoint['message']['valid']
                          and endpoint['message']['header']['frame_id'] in ('', 'base', '/base')
                          and time.monotonic_ns() - endpoint['received_monotonic_ns'] < 250000000)
            if future is not None and future.done():
                try:
                    result = future.result()
                    if future_choice == choice and result is not None:
                        reference = result
                except Exception as e:
                    if future_choice == choice:
                        status = 'REFERENCE ERROR: ' + (str(e).splitlines()[-1] if str(e) else type(e).__name__)
                future = None
            if not refs.loaded:
                if future is None and requested != 'index':
                    requested = 'index'; future_choice = choice
                    future = executor.submit(refs.load_index)
                    status = 'Loading dataset index: ' + datasets[dataset]
            elif demo is not None and not refs.demo_rows[demo].size:
                status = f'DEMO {demo:02d}: no frames in the selected dataset split'
            elif mode == 'pose' and not valid_pose:
                status = 'NO FRESH MEASURED POSE: pose matching paused'
            elif demo is not None or mode == 'pose':
                if not frozen and now - last_pose_select > .5:
                    pose = endpoint['message']['pose'] if valid_pose else None
                    selected = refs.select(mode, demo, pose)
                    last_pose_select = now
                if future is None and selected is not None and selected != requested:
                    requested = selected; reference = None; features = {}
                    future_choice = choice
                    future = executor.submit(refs.images, *selected)
                    status = 'Loading verified training PNG ep%d/frame%d' % selected
            canvas = np.full((790, 1240, 3), 24, np.uint8)
            text(canvas, 'READ ONLY | Q/Esc quit | S save raw + comparison | Space freeze reference | no camera controls', 14, 24)
            latest = {}; comparison = {}
            for ri, role in enumerate(('main', 'wrist')):
                y = 88 + ri * 330
                try:
                    if role not in topic_images and role not in cameras:
                        raise RuntimeError('Waiting for existing RGB8/BGR8 ROS image topic')
                    native, meta = topic_images[role] if role in topic_images else cameras[role].latest()
                    age = (camera_clock() - meta['received_monotonic_ns']) / 1e6
                    if age > 250: raise RuntimeError('STALE camera %.0fms' % age)
                    if native.shape != (480, 640, 3): raise RuntimeError('Expected unchanged native 640x480 RGB stream')
                    current = resize(native, 224, 224).copy()
                    latest[role] = (native, current, meta)
                    seen_cameras.add(role)
                    usable = reference is not None and (valid_pose or mode != 'pose')
                    if usable:
                        ref = reference[0][role]
                        metrics, diff = pixel_metrics(ref, current)
                        overlay = np.rint((ref.astype(float) + current.astype(float)) / 2).astype(np.uint8)
                        heat = np.zeros_like(ref)
                        heat[28:196] = cv2.cvtColor(cv2.applyColorMap(np.rint(diff).astype(np.uint8), cv2.COLORMAP_INFERNO), cv2.COLOR_BGR2RGB)
                        if now - last_feature > 1 or role not in features:
                            features[role] = feature_displacement(ref, current, static_main=role == 'main')
                        comparison[role] = dict(metrics=metrics, features=features.get(role), frame=meta)
                        row = [ref, current, overlay, heat]
                        summary = '%s MAE %.2f RMSE %.2f | gray ref/live %.1f/%.1f | age %.0fms | padding excluded' % (role.upper(), metrics['mae'], metrics['rmse'], metrics['reference_gray_mean'], metrics['current_gray_mean'], age)
                    else:
                        row = [np.zeros_like(current), current, np.zeros_like(current), np.zeros_like(current)]
                        summary = role.upper() + ' | ' + status
                    text(canvas, summary, 14, y - 12)
                    for ci, im in enumerate(row):
                        x = 14 + ci * 305
                        canvas[y:y + 224, x:x + 224] = cv2.cvtColor(im, cv2.COLOR_RGB2BGR)
                        text(canvas, ['Training RGB', 'Current RGB', '50% overlay', 'RGB |diff| mean; fixed 0..255'][ci], x, y + 245, scale=.43)
                    ft = features.get(role)
                    if usable and ft:
                        label = 'Features: %d unique, %d inliers, confidence %s' % (ft['unique_matches'], ft['inliers'], ft['confidence'])
                        if ft.get('displacement_xy_px') is not None: label += ' | dx,dy = %.2f, %.2f px' % tuple(ft['displacement_xy_px'])
                        else: label += ' | no reliable global displacement'
                        text(canvas, label, 14, y + 270)
                except RuntimeError as e:
                    if ros is not None and role in cameras and cameras[role].process.poll() is not None:
                        raise
                    camera_errors[role] = str(e)
                    text(canvas, role.upper() + ': ' + str(e)[:130], 14, y + 100, (50, 100, 255))
            if now - last_feature > 1: last_feature = now
            pose_error = None
            if reference is not None:
                meta = reference[1]
                status = f"Demo {meta['demo_number']:02d} | segment {meta['episode_index']}/frame {meta['frame_index']} ({mode})"
                if valid_pose:
                    pp, aa, _ = pose_distance([meta['pose_state_xyz_rotvec']], endpoint['message']['pose'])
                    near = bool(pp[0] <= .02 and aa[0] <= 5)
                    pose_error = {'position_mm': float(pp[0] * 1000), 'orientation_deg': float(aa[0]), 'near': near}
                    status += ' | pose %.1fmm / %.2fdeg | %s' % (
                        pp[0] * 1000, aa[0], 'NEAR pose' if near else 'POSE MISMATCH - wrist geometry inconclusive')
                elif ros is not None:
                    status += ' | NO FRESH MEASURED POSE'
                if frozen:
                    status += ' | FROZEN'
            status_color = (90, 220, 90) if pose_error and pose_error['near'] else (50, 160, 255)
            text(canvas, status[:160], 14, 57, status_color, .47)
            text(canvas, 'Dataset | ' + datasets[dataset], 14, 746, scale=.48)
            last_frame = dict(canvas=canvas, latest=latest, comparison=comparison, records=records,
                              pose_error=pose_error, selection={'dataset': dataset, 'mode': mode, 'demo_number': demo,
                                                               'source_episode': refs.demos.get(demo) if refs.loaded else None})
            frames += 1
            if window:
                window.show(canvas)
                if window.save_requested:
                    save(out / ('snapshot_%03d' % saved), last_frame, reference); saved += 1
                    window.save_requested = False
            time.sleep(max(0, 1 / args.hz - (time.monotonic() - now)))
        if last_frame:
            save(out / 'final', last_frame, reference)
        summary = dict(rendered_frames=frames, snapshots=saved, output=str(out))
        if WINDOWS:
            missing = {role: camera_errors.get(role, 'no frames')
                       for role in ('main', 'wrist') if role not in seen_cameras}
            summary.update(cameras_seen=sorted(seen_cameras), missing_cameras=missing)
        else:
            summary['parameter_set_calls'] = 0
        (out / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        print('Saved ' + str(out), flush=True)
        if WINDOWS and args.headless and missing:
            details = ', '.join(f'{role}: {error}' for role, error in missing.items())
            raise RuntimeError('No valid 640x480 frames received from ' + details)
    finally:
        for camera in cameras.values(): camera.close()
        if ros is not None:
            ros.close()
        for reader in readers.values():
            reader.close()
        executor.shutdown(wait=True)
        if window:
            window.root.destroy()


def save(directory, frame, reference):
    directory.mkdir()
    write_rgb(directory / 'comparison.png', cv2.cvtColor(frame['canvas'], cv2.COLOR_BGR2RGB))
    payload = dict(comparison_valid=bool(frame['comparison']), reference_pose_error=frame['pose_error'],
                   reference_selection=frame['selection'], cameras=frame['comparison'], raw_ros_observations=frame['records'],
                   reference=reference[1] if reference else None, saved_utc=datetime.now(timezone.utc).isoformat(),
                   preprocessing='Pinned OpenPI RGB224 bilinear letterbox; no registration/flip/photometric correction',
                   feature_coordinates='224x224 RGB image pixels; y includes 28-row padding; current minus reference')
    (directory / 'observation.json').write_text(json.dumps(payload, indent=2))
    for role, (native, current, meta) in frame['latest'].items():
        write_rgb(directory / (role + '_native_rgb.png'), native)
        write_rgb(directory / (role + '_current.png'), current)
        if reference and role in frame['comparison']:
            ref = reference[0][role]; write_rgb(directory / (role + '_training.png'), ref)
            overlay = np.rint((ref.astype(float) + current.astype(float)) / 2).astype(np.uint8)
            write_rgb(directory / (role + '_overlay50.png'), overlay)
            _, diff = pixel_metrics(ref, current); np.save(directory / (role + '_rgb_absdiff_mean.npy'), diff)
            heat = np.zeros_like(ref); heat[28:196] = cv2.cvtColor(cv2.applyColorMap(np.rint(diff).astype(np.uint8), cv2.COLORMAP_INFERNO), cv2.COLOR_BGR2RGB)
            write_rgb(directory / (role + '_diff_heatmap.png'), heat)


def verify_resize(resize):
    probes = ASSETS / 'resize_probes.json'
    expected = json.loads(probes.read_text())
    for seed, target in expected['rgb640x480_seed_to_output_sha256'].items():
        rgb = np.random.RandomState(int(seed)).randint(0, 256, size=(480, 640, 3)).astype(np.uint8)
        if hashlib.sha256(resize(rgb, 224, 224).tobytes()).hexdigest() != target:
            raise RuntimeError('Resize differs from pinned training output; use a compatible Pillow environment')


def frame_modes():
    return {mode: FRAME_MODES[mode] for mode in ('first', 'last')} if WINDOWS else FRAME_MODES


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    local_home = Path(os.environ.get('SAWYER_LOCAL_HOME', 'windows-local' if WINDOWS else 'ubuntu-local')).expanduser()
    parser.add_argument('--reference-cache', type=Path, default=local_home / 'camera-compare/reference-cache')
    parser.add_argument('--output', type=Path, default=local_home / 'camera-compare/snapshots')
    parser.add_argument('--ssh-host', default='create')
    parser.add_argument('--hpc-python', default='/scratch/prj/eng_demt_robot_learning/robot-vla/.venv-hpc-service/bin/python')
    parser.add_argument('--dataset', default=DATASET)
    parser.add_argument('--frame-mode', choices=frame_modes(), default='first' if WINDOWS else 'pose')
    parser.add_argument('--demo', '--episode', dest='demo', type=int, help='Original demonstration number, starting at 1')
    if WINDOWS:
        parser.add_argument('--main-device', default=WINDOWS_MAIN, help='DirectShow name or index')
        parser.add_argument('--wrist-device', default=WINDOWS_WRIST, help='sn9c20x (WinUSB), DirectShow name or index')
        parser.add_argument('--list-cameras', action='store_true', help='List DirectShow devices without opening a stream')
    else:
        parser.add_argument('--main-device', default=MAIN); parser.add_argument('--wrist-device', default=WRIST)
        parser.add_argument('--main-topic'); parser.add_argument('--wrist-topic')
    parser.add_argument('--hz', type=float, default=10)
    parser.add_argument('--seconds', type=float)
    parser.add_argument('--headless', action='store_true', help='Bounded actual-camera smoke test; requires --seconds')
    args = parser.parse_args(argv)
    if WINDOWS and args.list_cameras:
        from .cameras import windows_camera_names
        names = windows_camera_names()
        for index, name in enumerate(names):
            print(f'{index}: {name}')
        if not names:
            print('No DirectShow cameras found.')
        print('A Sandberg SN9C201 using WinUSB is selected with --wrist-device sn9c20x and is not listed here.')
        return
    if not 0 < args.hz <= 30 or (args.seconds is not None and (not math.isfinite(args.seconds) or args.seconds <= 0)):
        parser.error('Invalid rate or duration')
    if args.demo is not None and args.demo < 1: parser.error('--demo must be a positive original demonstration number')
    if args.headless and args.seconds is None: parser.error('--headless requires --seconds')
    if not WINDOWS and not args.headless and not os.environ.get('DISPLAY'):
        parser.error('No DISPLAY; start in a desktop terminal or use --headless --seconds 10')
    run(args)


if __name__ == '__main__':
    main()
