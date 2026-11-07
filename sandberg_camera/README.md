# Wrist Camera 使用说明

本模块为 robot learning 提供腕部相机的采集与图像预处理基础，可用于示教数据采集、VLA 训练数据准备和策略部署。默认采集 **640×480 彩色图像**，并生成 **224×224 RGB 策略输入**。

## 1. 文件说明

| 文件 | 用途 |
| --- | --- |
| `camera_test.py` | 独立采集入口，支持实时预览和图像保存 |
| `camera_preview.py` | 使用测试成像配置的持续实时预览，支持中心标记、放大及手动快照 |
| `camera_profile.json` | 设备、成像参数和预处理配置 |
| `camera_pipeline.py` | 采集与部署共用的 Python 接口 |

当前设备使用 Linux V4L2，驱动为 `gspca_sn9c20x`。默认设备路径为：

```text
/dev/v4l/by-id/usb-0c45_USB20_Camera-video-index0
```

使用固定设备标识可以适应重插后 `/dev/videoN` 编号的变化。

## 2. 快速开始

### 在 wrist 上调整相机角度

直接运行（自动使用已配置的 `arcap` 环境）：

```bash
/home/endongsun/Desktop/sawyer_teleoperation/Preview-Wrist-Camera.sh
```

界面会持续显示完整视野、标记附近的局部放大，以及真实的 224×224 策略输入。预览只在按 **S** 或点击“保存快照”时写盘，不会持续采集数据。

同一相机只启动一个预览实例；重复运行命令会唤起已有窗口。若相机被 `camera_test.py` 或其他采集程序占用，先退出对应程序再启动预览。启动脚本固定使用 `arcap`，在 `(base)` 终端中运行也可以。

1. 调整相机的安装角度，让两侧夹指及中间操作区域进入画面，尽量靠近蓝色中心十字，同时保持在绿色裁剪框内。
2. 点击夹爪中间，查看与图像中心的 `dx / dy` 像素偏移；也可以勾选“依次点两个指尖，计算中点”。蓝色十字始终是固定的图像中心。
3. 成像自动沿用 `camera_test.py` 的同一份 `camera_profile.json`：曝光、增益及红蓝平衡均在启动时应用并读回确认，无需在界面手动调节。完整画面仅转换 BGR → RGB 用于显示，224 图也与测试保存的图片使用相同处理流程。
4. 标记是手动指定的图像坐标，不会跟踪夹爪；每次调整相机后重新点击。像素偏移不是机器人位姿或毫米标定。
5. 按 **S** 保存原始图、标记图、224 图和当前成像配置到 `robot_dataset/wrist_alignment/`，**R** 清除标记，**F** 全屏，**Q / Esc** 退出。

快照中的 `profile.json` 可通过 `camera_test.py --profile <快照目录>/profile.json` 复用。界面读取相机，不发送机器人或夹爪运动命令。退出后释放相机，再启动采集程序。

该界面额外使用 `arcap` 中已有的 Tkinter 和 Pillow，需要本地图形桌面。连接中断或长时间没有新帧时，界面会明确显示画面已停止更新。

### 定时采集

使用已配置好的 `arcap` 环境，其中需要 Python、OpenCV、NumPy，以及系统命令 `v4l2-ctl`。

```bash
conda activate arcap
```

采集 30 秒：

```bash
python3 /home/endongsun/Desktop/sawyer_teleoperation/sandberg_camera/camera_test.py --duration 30
```

实时预览并采集 60 秒：

```bash
python3 /home/endongsun/Desktop/sawyer_teleoperation/sandberg_camera/camera_test.py --duration 60 --preview
```

预览会显示完整画面和 224×224 策略图像，完整画面中的绿色框表示裁剪范围。按 **Q** 或 **Esc** 提前结束；预览需要本地图形桌面环境。

指定输出位置：

```bash
python3 /home/endongsun/Desktop/sawyer_teleoperation/sandberg_camera/camera_test.py \
  --duration 30 \
  --output /home/endongsun/Desktop/sawyer_teleoperation/robot_dataset/wrist_runs
```

每次运行会先预热 60 帧，再开始计时采集，并在输出位置创建独立的运行目录。

| 参数 | 作用 | 默认值 |
| --- | --- | --- |
| `--duration` | 采集时长，单位为秒 | `10` |
| `--preview` | 显示实时预览 | 关闭 |
| `--output` | 存放各次运行目录的父目录 | 项目下的 `robot_dataset/runs` |
| `--profile` | 加载指定配置文件 | 脚本同目录的 `camera_profile.json` |
| `--device` | 本次运行使用的设备路径 | 配置文件中的 `device` |

## 3. 输出数据

默认保存到 `/home/endongsun/Desktop/sawyer_teleoperation/robot_dataset/runs/`。每次运行的目录结构如下，时间部分使用 UTC：

```text
20261103T130000Z_a1b2c3d4/
├── source_images/
│   ├── 000000.png          # 完整的 640×480 彩色图像
│   └── ...
├── images/
│   ├── 000000.png          # 处理后的 224×224 彩色图像
│   └── ...
├── timestamps.csv         # 帧编号、图像路径和时间戳
├── profile.json           # 本次运行的配置快照
└── metadata.json          # 配置哈希、实际参数和采集统计
```

两组图像通过相同编号对应，均使用 PNG 无损保存。保留完整图像便于之后调整裁剪区域或重新生成训练图像。

`metadata.json` 包含实际保存帧数、帧率、帧间隔、亮度统计和设备信息，可用于检查一次采集的质量。

### 时间戳

`timestamps.csv` 中的路径相对于本次运行目录，时间戳单位为纳秒：

| 字段 | 含义 |
| --- | --- |
| `frame_id` | 从 0 开始的帧编号 |
| `filename` | 224×224 图像路径 |
| `source_filename` | 640×480 原始图像路径 |
| `monotonic_ns` | 主机读取该帧完成的单调时钟时间，与 `read_completed_monotonic_ns` 相同 |
| `read_started_monotonic_ns` / `read_completed_monotonic_ns` | 调用相机读取前后的时间 |
| `write_started_monotonic_ns` / `write_completed_monotonic_ns` | 图像写盘前后的时间 |

与机器人状态、动作进行对齐时，使用同一主机上的单调时钟，或建立不同设备间的时钟映射。图像时间戳表示主机读取完成时刻；传感器曝光时刻与实际采集延迟需要单独标定。

## 4. 成像与预处理配置

编辑 `/home/endongsun/Desktop/sawyer_teleoperation/sandberg_camera/camera_profile.json` 可调整参数。脚本启动时会应用成像参数并读回确认。

| 项目 | 默认配置 |
| --- | --- |
| 输入尺寸 | 640×480 |
| 采集格式 | BA81 Bayer，由 OpenCV 转成 BGR |
| 自动曝光/增益控制 | 关闭，`gain_automatic=0` |
| 曝光 | `300`，单位由驱动定义 |
| 增益 | `10` |
| 红 / 蓝平衡 | `33 / 28` |
| 水平 / 垂直翻转 | 均关闭 |
| 预热 | 60 帧 |

当前配置在本机运行约 30 FPS，实际帧率由每次采集的 `metadata.json` 记录。曝光、增益和颜色平衡采用固定值，按任务现场灯光调整后，应在同一批数据采集及部署中保持一致。

默认预处理过程为：

```text
640×480 BGR 图像
    → 固定裁剪 [x=80, y=0, width=480, height=480]
    → INTER_AREA 缩放至 224×224
    → 转成 RGB，送入策略的图像处理器
```

`crop_xywh` 按 `[左上角 x, 左上角 y, 宽度, 高度]` 定义。当前接口要求裁剪区域为正方形、位于原图范围内，输出固定为 224×224。

图像文件保存时使用 OpenCV 所需的 BGR 数组；以 Pillow 等方式读取 PNG 时得到 RGB。使用 `cv2.imread` 读取训练图像时，需按模型要求转成 RGB。

## 5. 安装到手腕

在最终安装位置和实际操作距离完成以下设置：

1. 固定相机角度，使夹爪指尖、目标物体和接触区域位于画面内。
2. 在真实工作距离对焦，同时检查 224×224 预览中的边缘和物体细节。
3. 用 `--preview` 检查绿色裁剪框，按任务调整 `crop_xywh`，确保操作过程中的目标与夹爪都在框内。
4. 按实际腕部运动速度采集短片，检查拖影、亮度变化和线缆活动空间。
5. 确认参数后保存配置，采集训练数据和部署策略时使用相同的安装位置与图像处理流程。

## 6. 接入 Robot Learning

在项目根目录运行的程序中，可直接导入共享接口：

```python
import time

from sandberg_camera.camera_pipeline import (
    load_profile,
    open_camera,
    preprocess_rgb,
)

profile = load_profile()
cap, camera_info = open_camera(profile)

try:
    ok, source_bgr = cap.read()
    timestamp_ns = time.monotonic_ns()
    if not ok:
        raise RuntimeError("Camera read failed")

    observation = {
        "wrist_rgb": preprocess_rgb(source_bgr, profile),
        "timestamp_ns": timestamp_ns,
    }
    # wrist_rgb: (224, 224, 3)，RGB，uint8，连续内存。
    # 将 observation 接入自己的示教采集器或策略推理循环。
finally:
    cap.release()
```

持续采集时保持相机打开，在循环中读取图像；结束时释放设备。

| 接口 | 用途 |
| --- | --- |
| `load_profile(path)` | 加载并检查配置；省略路径时加载默认配置 |
| `open_camera(profile, device=None)` | 打开相机、应用参数并预热，返回相机对象和设备信息 |
| `preprocess_rgb(source_bgr, profile)` | 生成供策略使用的 224×224 RGB 数组 |
| `preprocess_bgr(source_bgr, profile)` | 生成供 OpenCV 保存的 224×224 BGR 数组 |

接入示教数据集时，将相机图像与机器人关节状态、末端位姿、夹爪状态、动作、任务描述和 episode 边界一并记录。独立采集脚本负责图像与时间戳；机器人状态和动作由上层采集器接入。

训练数据准备与策略部署应共用相同的裁剪、缩放、方向和 RGB 顺序，并核对模型图像处理器是否还会执行额外的裁剪或缩放。每次采集保存的配置快照可用于复现输入图像。
