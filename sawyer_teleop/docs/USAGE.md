# 使用

**先启动 B，再启动 A。** 安装见 [SETUP](SETUP.md)。

| 后端 | A 里选 | B 地址 | 控制 / IK 端口 | IK |
| --- | --- | --- | --- | --- |
| 同机 Windows Gazebo | Gazebo Simulation | `127.0.0.1` | 8875 / 8876 | A 本地 |
| Ubuntu Gazebo（另一台） | Gazebo Simulation | 那台 Ubuntu 的 IPv4 | 8875 / 8876 | A 本地 |
| 实体 Sawyer | Real Sawyer | `SAWYER_BIND_IP` | 8765 / 8766 | B 远端官方 IK（自动） |
| 软件测试 | Software Test | 测试 B 地址 | 8765 / 8766 | 远端 |

地址填 **B 电脑**，不填机器人控制器地址。跨机时放行 A→B 的两个 TCP 端口。

## 启动 B

### Windows 仿真 B

```bat
Start-B.cmd              :: 带 Gazebo 三维窗口
Start-B.cmd --headless   :: 无画面，物理与反馈照常
Start-B.cmd --stop       :: 停止本项目的仿真容器（也可双击 Stop-B.cmd）
Start-B.cmd --check      :: 只检查，不启动
```

等待窗口出现 `backend=gazebo`，保持窗口打开；Ctrl+C、`--stop` 或 `Stop-B.cmd` 都会停止。
已有同项目实例时会保留场景直接复用；渲染默认自动探测 GPU，可加 `--rendering software|gpu`。
重启 B 会重置场景。

### Ubuntu 仿真 B

在 `sawyer_teleop/` 下：

```bash
bash scripts/ubuntu/Start-B-Sim.sh --gui                       # A、B 同机，带画面
bash scripts/ubuntu/Start-B-Sim.sh --headless --bind <本机IPv4>  # A 在另一台电脑
```

容器名 `sawyer-phase3-sim`；Ctrl+C 停止。另开终端只读监视：`bash scripts/monitor-sim.sh`。

### 实体 Sawyer B

按实验室流程完成机器人开机、SDK 准备后，在 `B-Lab-Ubuntu/` 下：

```bash
bash Start-B.sh
```

脚本依次：source ROS 与工作空间 → 拒绝仿真时钟 → 只读检查 ROS master 和官方 IK 服务 →
把 `config/lab-neutral-pose.yaml` 载入 `/named_poses/right/poses/neutral`（Reset 目标）→ 启动 B。
**不会**自动使能、回中、清急停、启动 roscore。使能按实验室流程，保持唯一运动控制者。
固定参数：控制 100 Hz，工具 `right_hand`，最新反馈 / 源引用 / 命令期限 100 / 250 / 250 ms。
配置了 Dynamixel 串口的 B 支持夹爪控制，见 [部署补充](DEPLOYMENT.md#配置与启动)。

## 启动 A

Windows 双击 `Start-A.cmd`；Ubuntu 在 `sawyer_teleop/` 下 `bash scripts/ubuntu/Start-A.sh`。
会自动打开浏览器；**请使用启动窗口打印的完整 URL**（带本次启动的临时授权），不要直接打开 `index.html`。
可选参数：`--no-browser`、`--port 8781`。保持启动窗口打开，Ctrl+C 退出。

同一时刻只有一个网页持有操作权，其余页面只读，释放后可点 Acquire controls。
持有操作权的页面关闭、刷新或 3 秒没有请求状态时，服务结束其控制会话（未完成的录制会作为不完整数据保留）。

## 遥操流程

1. 头显戴好，Sawyer Quest Input 在前台，右手柄被追踪。
2. A 选后端、B 地址、Quest 序列号 → **Connect**，确认反馈为 Fresh。
3. 面向所选机械臂底座 +X 方向，点 **Calibrate**。
4. 先松开右侧握键，再握住开始跟随；松开即暂停，重握从新的手柄 / 机器人锚点开始。

右手位置控制末端平移，姿态控制旋转；仿真中右扳机控制夹爪。追踪或反馈失效会暂停，恢复后先松手再握。
重新连接或 Quest 参考系变化后要重新校准。普通遥操不写录制文件。

**结束**：Save & End（等保存完成）→ 松开握键 → Disconnect → 关闭 A → 停止 B。

## 界面要点

- **Live status**：四段链路频率（最近一秒事件数）：A 接收（Quest→A）、A 发送（A→B）、B 执行提交、B→A 反馈。
  B 执行提交是交给后端 / SDK 的次数，不表示到位。
- **Last pause reason**：正常松手是普通文字，其他原因标红。**Last pause details** 默认收起，字段含义见
  [TROUBLESHOOTING](TROUBLESHOOTING.md#last-pause-details-字段)。
- **Actual robot posture**：默认折叠，展开后显示 B 实际关节反馈的三维姿态，见 [ROBOT_VIEW](ROBOT_VIEW.md)。
- **Activity**：内存最多 600 条，过滤 / 暂停 / 清空只影响显示。

### 输入平滑（Smooth teleop）

A 在 IK 前对目标位姿做一次指数滤波，无死区。Light 20 ms，约 60 ms 达到 95%；Heavy 40 ms，约 120 ms；
DROID-like 150 ms（默认，2026-11-07 起），约 450 ms 达到 95%，更顺但明显更滞后。
默认只作用于首次启动时从模板复制的本机配置；已有 `windows-local/config/operator.json` 保持其保存值。不勾选则不经过滤波。修改在**下次连接**生效。松手不经过滤波，仍优先取消。

### 运动选项（Motion option）

切换前：松手 → 结束整批录制 → 等停止确认 → 选择 → **Apply**，下一次握持生效，无需重连。

| 选项 | 后端 | 执行方式 |
| --- | --- | --- |
| Standard（默认） | Gazebo / 实机 | IK → B 100 Hz 关节轨迹；关节加速度 `1.5,1.5,3,3,3,3,3` rad/s² |
| Fast follow（实验） | Gazebo / 实机 | 同上，加速度更高（Gazebo `6,6,12,…`；实机为 Standard 的 2 倍）；速度、期限与保护不变 |
| Intera Cartesian 10/20/50/100 Hz | 实机 / Gazebo | IK 后 B 做 SDK FK，提交原生 CARTESIAN action；频率是目标提交节拍，不是伺服频率 |
| Direct pose 50 Hz（实验） | 实机 | A 直接发送末端位姿，B 提交间隔 ≥ 20 ms，跳过 IK/FK |

- 速度上限均为 `.8,.8,1,1,1.57,1.57,1.57` rad/s；Reset 使用基准速度 / 加速度的 25%。
- 原生选项（Cartesian、Direct pose）要等 B 在握手中宣告能力：**先 Connect 再选**。没有 SDK 服务时选项为灰色，Standard 照常可用。
- Gazebo 下的原生选项由项目自带的仿真 `/motion/motion_command` 执行（hello 标 `simulated`），
  只用于跑通链路，**不能当作 Intera 实机验收**。
- 原生 STOP 要等结果确认；2 秒无确认或失败会锁定 B，必须现场检查后重启 B。
- B 的原生参数可通过 ROS 参数 `/sawyer_teleop/intera_cartesian` 覆盖（`send_hz`、`max_linear_speed` 0.6、
  `max_linear_accel` 0.6、`max_rotational_speed` 1.57、`max_rotational_accel` 1.57、`max_joint_speed_ratio` 1.0），改后重启 B。

### 关节跳变保护

`Joint protection (±0.35 rad / 20°)` 默认勾选，覆盖所有遥操选项。A 检查 IK 解和发送目标，B 独立检查远端 IK、
接收目标和实际提交；超限直接暂停，不裁剪目标。关闭它不影响其他限速、跟随误差和过期保护。详见 [JOINT_PROTECTION](JOINT_PROTECTION.md)。

### 录制

填写 Batch name、数量、单条时长、间隔和输出目录（A 电脑上的目录），点 **Start Recording**。

| 操作 | 结果 |
| --- | --- |
| 右手柄 A / Save Episode | 保存当前条，按间隔接续下一条 |
| 右手柄 B / Discard Episode | 丢弃当前条，按间隔接续 |
| 到达单条时长 | 自动保存；未达数量则继续 |
| Save & End | 保存当前条并结束整批，等待写盘完成 |
| 松开握键 | 停止跟随，录制继续 |

保存成功才计数，同名不覆盖；倒计时不会自动握持或驱动机器人。结束录制也不会停止遥操。

**相机曝光 / 增益滑条**：Cameras 每路画面下方有 Exposure、Gain 滑条，松手即生效，不重开相机，并写回 `operator.json`，下次启动沿用。腕部相机：曝光 1–800（传感器行数），增益档位 0–28。RealSense：曝光 -13 到 -5（2 的幂秒，-6 = 1/64 s），增益 0–128，另有 Auto exposure 开关；拖动曝光会自动关闭自动曝光。需要本页持有控制权；**录制进行中滑条锁定**，每条录制的参数记录在 `camera_config.controls`。

实机录制要求开启 **Record cameras**，并确认全部已配置相机在线且正在出帧。已有本机配置会保留原勾选状态。
开始时还需要 100 ms 内的机器人反馈。
录制中某路相机持续 1 秒无帧时，A 停止整批，保留该段供检查，该段不计入批次数量。

**Reset arm between episodes**（默认不勾选）：勾选后，每条保存或丢弃处理完、且整批未结束时，A 自动执行一次
Reset arm（与按钮相同的复位目标、0.25 倍限速与到位判据；Gazebo 同时恢复场景），确认到位后才开始间隔倒计时，
再开始下一条。复位会使机械臂运动，期间手柄握持无效；完成后需松开再握持才恢复遥操。点 **Stop reset**、
Save & End，或复位超时 / 故障 / 断连，都会停在原地并结束整批，不会自动开始下一条。最后一条之后不复位。
开始前若 B 不提供可用的 Reset 姿态，则拒绝开始录制。该选项记入 `batch.json` 与每条 metadata 的 `batch.reset_between`。
manifest / HDF5 的 `complete` 只表示数据完整写盘，不表示任务成功。格式见 [DATASET](reference/DATASET.md)。

#### Raw schema 2

| 位置 | 内容 |
| --- | --- |
| `feedback/joint` | B 已接收的原生频率关节反馈，含 7 轴 `q` / `dq`（rad / rad/s）、源时间戳和每路序号 `stream_seq`。 |
| `feedback/pose` | B 已接收的原生频率末端反馈，含 `eef_pos`、`eef_quat`（xyzw）及 `eef_twist`（线速度在前、角速度在后），`eef_twist_valid` 标识是否有效。 |
| `frames/<相机名>/rgb` 和 `<相机名>.mp4` | HDF5 保存无损 RGB，MP4 保存同序帧供预览。以 HDF5 的每帧 `ns` 为准。 |
| `alignment/<相机名>` | 每帧在 `states`、`feedback/joint`、`feedback/pose` 中各自最新且不晚于该帧的行索引及年龄，字段为 `state/joint/pose` 对应的 `*_index` 和 `*_age_ns`，无匹配时为 `-1`。 |
| `quality` | HDF5 根属性和 `manifest.json` 中的质量摘要，含相机帧率、掉帧率、最大间隔，以及每路反馈频率、丢行率和最大间隔。 |
| `metadata.initial_state` | 每段开始时的关节位置 / 速度、末端位姿和可用的 twist、夹爪状态、反馈年龄及源时间戳。批间复位后还记录目标关节角及最大到位误差。 |

`feedback_stream` 按 A/B 会话协商，仅供录制。旧 B 或禁用该能力时仍保存 `states` 最新值快照，原生反馈组为空并在质量中标记缺失。
两类反馈均不记录 effort 或 wrench。录制保留各路原始时间戳并记录缺口，不补帧。导出按时间戳选择仍有效的过去帧，
丢弃没有有效帧的时间步，并跳过 `quality.status == incomplete` 的整段。

录制默认保存到 `windows-local/data/`（Ubuntu：`ubuntu-local/data/`）。离线分析和导出（只读录制，不连接 B）：

```bash
python -m sawyer_teleop.cli analyze-motion <录制目录> --output <报告.json>
python -m sawyer_teleop.cli export <录制目录> <输出.hdf5>
```

Windows 下把 `python` 换成 `windows-runtime\python\python.exe`。训练适配范围见 [TRAINING](reference/TRAINING.md)。

### Reset arm

先停止录制并松手。Reset arm 用原轨迹回到 neutral。B 支持夹爪控制时，回中开始即发出张开指令，
机械臂到位且夹爪确认张开后才完成。手动复位和批间自动复位行为相同，进行中点 Stop 可同时取消回中和张开。
Gazebo 回中并保持后，还会异步还原场景中非机器人模型的位置并清零速度（不重置 `/clock`）。
Gazebo 的初始姿态与 Reset 相同：官方 J6=3.3161 rad 在 Gazebo 中表示为 −2.967 rad（整圈等价），仅仿真使用。

## 改了代码怎样生效

运行中的 A/B 不会热加载。通用顺序：Save & End → 松手 → Disconnect → 重启需要更新的一端 → Connect → Calibrate。

| 改动 | 做什么 |
| --- | --- |
| 只改网页静态文件 | Disconnect 后刷新页面（会结束会话，需重连校准） |
| 只改 A 的 Python | 重开 A |
| 改 B（仿真） | `Stop-B.cmd`（或 `Start-B.cmd --stop`）后重启；不需要重建镜像 |
| 改 B（实机） | 重新导出 `B-Lab-Ubuntu`，替换后在现场重启 B |
| 改协议或两端语义 | A、B 都更新 |

不要同时运行两份源码的本机 B，它们使用同一容器名和端口。
