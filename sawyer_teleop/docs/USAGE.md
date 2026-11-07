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
固定参数：控制 100 Hz，工具 `right_hand`，最新反馈 / 源引用 / 命令期限 100 / 250 / 250 ms；实机夹爪尚未接入。

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
manifest / HDF5 的 `complete` 只表示数据完整写盘，不表示任务成功。格式见 [DATASET](reference/DATASET.md)。

录制默认保存到 `windows-local/data/`（Ubuntu：`ubuntu-local/data/`）。离线分析和导出（只读录制，不连接 B）：

```bash
python -m sawyer_teleop.cli analyze-motion <录制目录> --output <报告.json>
python -m sawyer_teleop.cli export <录制目录> <输出.hdf5>
```

Windows 下把 `python` 换成 `windows-runtime\python\python.exe`。训练适配范围见 [TRAINING](reference/TRAINING.md)。

### Reset arm

先停止录制并松手。Reset arm 用原轨迹回到 neutral，进行中按钮变成 Stop 可取消。
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
