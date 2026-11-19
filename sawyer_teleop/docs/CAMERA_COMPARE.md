# 推理前实时相机对比

Windows 安装和启动见 [DEPLOYMENT 的 Windows 相机对比](DEPLOYMENT.md#windows-相机对比)。Windows 以首帧、末帧为参考，截图包含两路 RGB、叠加、热图和指标。姿态匹配与 ROS 图像话题使用以下 Ubuntu 入口。

在实验室 Ubuntu 桌面终端，从仓库根目录启动。

```bash
./sawyer_teleop/scripts/ubuntu/Start-Camera-Compare.sh
```

窗口上下分别是主相机、腕相机，每行依次显示真实训练帧、当前画面、50% 叠加、RGB 绝对差热图。默认约 10 Hz 显示；特征匹配约每秒更新一次。右上热图使用固定 0–255 色阶，不随每帧重新拉伸；黑色补边不参与指标。没有先配准、翻转、曝光补偿或自动几何校正。

窗口顶部可选 **参考数据集**、**参考帧** 和 **原始示范**。原始示范按录制清单编号 **01–30**，旁边显示录制时间。内置的三个数据集使用同一套示范编号。State7 Alltrain 包含全部 30 条，两套 EEF Train 各包含 24 条，缺少的示范会标注「当前分区无帧」。

选 **首帧** 或 **末帧** 后，程序按原始录制顺序，在所选示范的所有分段中取第一张或最后一张可用训练帧。姿态匹配可选某条示范，也可选 **全部示范**。切换时窗口继续显示当前画面，参考加载完成后更新叠加图和 diff。

- `S`：保存截图、两路原始尺寸 RGB、训练图、当前图、50% 叠加图、热图、数值差异，以及真实时间和 ROS 观测。
- `空格`：在姿态匹配模式锁定或解锁当前训练参考。首帧和末帧模式直接锁定所选帧。
- `Q` / `Esc` / 关闭窗口：退出并释放本程序拥有的相机。**先退出，再启动推理**。

程序不移动、使能、归位机器人，也不写相机参数。默认沿用 D435 和 USB 腕相机现有格式，通过 V4L2 采集。打开前检查可见占用者，驱动 `REQBUFS` 再执行独占检查；忙设备不重试、不接管，不停止其他进程。若发现外部相机 ROS 话题，会要求指定话题以复用现有流：

```bash
./sawyer_teleop/scripts/ubuntu/Start-Camera-Compare.sh --main-topic /实际主相机RGB话题 --wrist-topic /实际腕相机RGB话题
```

支持 `sensor_msgs/Image` 的 `rgb8` / `bgr8`，要求原始 640×480；不要把机器人内置相机话题当作 D435/USB 相机。未提供话题时使用工程既有设备 ID。程序不改变已有自动曝光/自动增益模式；驱动原本启用的自动算法仍可自行改变曝光，采集前后控制读数均保留。首次启动或推理结束后相机刚开始采集时，应等画面亮度稳定再判断。

首次启动通过既有 `create` SSH 连接读取真实后训练集的完整姿态索引（12,877 帧），缓存数值和固定 OpenPI resize 源码；仅为实际需要的参考帧读取图片。后续使用缓存，不请求模型、不申请 GPU。数据集默认：

`/scratch/prj/eng_demt_robot_learning/sawyer_finray_pi05_state7_alltrain_v1_2026-11-12/train/`

姿态匹配按实测 `base/right_hand` 位置及 SO(3) 旋转距离最小化 `(位置差/20mm)^2 + (角度差/5°)^2`。空 ROS endpoint frame ID 沿用本实验室已经验证的 `base:right_hand` 映射。超过 250ms 的画面或姿态会标为过期。首帧和末帧模式可在没有实时姿态时做像素对比，窗口会提示姿态不可用。位置差超过 20mm 或旋转差超过 5°时显示 `POSE MISMATCH`，这是参考筛选标准。判断腕相机机位时还要考虑夹爪和末端姿态差异。

PNG 从真实 Parquet `image` / `wrist_image` 列提取，记录数据集、分段来源、帧编号和 SHA256。State7 的映射还提供上游 Parquet 路径，用它逐字节核对 PNG。当前 RGB 按固定 OpenPI bilinear resize 变成 224×168，再上下各补 28 行黑边；已有训练图不二次缩放。RobotPC 的 Python3.8 使用现有 Pillow10.4，启动时以 HPC Pillow11.2.1 独立生成的五组全尺寸随机 RGB 输出逐字节验证同一 resize。

MAE/RMSE 的单位为 0–255 RGB 灰度级，计算区域是全部 224×168 内容，保留真实黑色物体。热图为各像素三个通道绝对差的均值。亮度、场景/物体、机械臂姿态和机位都可能造成差异，指标不能直接当作相机位姿误差。

位移使用双向 SIFT、Lowe0.7、去重空间位置和 2px RANSAC。主相机的特征限定在右侧柜体/墙面（224 图像 x=130..219），减少机械臂/抽屉/物体变化影响；更换场景后应复核此区域。腕部只在匹配覆盖足够时显示全局位移。低可信匹配不显示可靠全局位移。置信度是匹配数量、内点比例和覆盖面积的规则判断，不是统计概率。二维图像旋转不是实际相机安装角度。

参考缓存默认存放在 `ubuntu-local/camera-compare/reference-cache/`，截图和观测存放在 `ubuntu-local/camera-compare/snapshots/`。两者均由 Git 忽略。程序保留最近采集帧、主动截图和退出时的最后一帧。

```bash
./sawyer_teleop/scripts/ubuntu/Start-Camera-Compare.sh --output ubuntu-local/camera-compare/my-check
./sawyer_teleop/scripts/ubuntu/Start-Camera-Compare.sh --frame-mode last --demo 6
./sawyer_teleop/scripts/ubuntu/Start-Camera-Compare.sh --headless --seconds 20
```

本工具需要 Ubuntu、ROS Noetic/catkin、包含 Intera 消息的工作空间、Tk 和 gcc。沿用 `sawyer_teleop/config/real-b.env` 的机器人连接信息；`SAWYER_REAL_CONFIG` 可指定本机配置。若相机对比使用其他已构建的 catkin 工作空间，可设置 `SAWYER_CAMERA_COMPARE_WORKSPACE`。

新电脑首次安装（在仓库根目录，使用与 ROS Noetic 相同的 Python 3.8）：

```bash
sudo apt install python3-venv python3-tk gcc openssh-client
python3 -m venv --system-site-packages ubuntu-local/camera-compare/.venv
ubuntu-local/camera-compare/.venv/bin/python -m pip install -e './sawyer_teleop[camera-compare]'
./sawyer_teleop/scripts/ubuntu/Start-Camera-Compare.sh --help
```

`--system-site-packages` 使环境能够复用系统 ROS 的 Python 依赖；Intera 消息由启动器加载的 catkin 工作空间提供。确认 `real-b.env` 中的工作空间、机器人地址和本机 ROS IP 与现场一致。仅安装依赖和查看 `--help` 不连接 ROS、不打开相机。

启动器优先使用 `ubuntu-local/camera-compare/.venv`，然后使用项目 `sawyer_teleop/.venv`。可在所选环境安装可选依赖 `pip install -e './sawyer_teleop[camera-compare]'`，也可通过 `SAWYER_CAMERA_COMPARE_PYTHON` 指定已有的 ROS 兼容 Python 环境。SSH 主机和远端 Python 可通过 `--ssh-host`、`--hpc-python` 覆盖。远端 Python 需要 PyArrow 和 Pillow。

安装包还提供独立命令 `sawyer-camera-compare`，等价于 `python -m sawyer_teleop.camera_compare`。直接使用命令时需自行加载 ROS/catkin 环境并设置 `ROS_MASTER_URI`、`ROS_IP`；实验室日常使用上面的 shell 启动器即可，不需要启动 A、B 或 Web 操作台。首次使用参考图需要本机已配置 `create` 的 SSH 密钥登录和远端训练数据读取权限。

`--demo` 和别名 `--episode` 现在都接收从 1 开始的原始示范编号。截图记录所选示范、原始录制 ID，以及实际参考的分段和帧号。
