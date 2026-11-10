# 实验室 B 部署补充

## 机器人时钟自动测量（2026-11-07 起，取代下方手动恢复）

B 启动时及运行中每 2 秒，用普通 UDP（无需 sudo、不改系统配置）向 `SAWYER_ROBOT_HOST`
（即 ROS master 主机，`sawyer.local`）的 NTP 服务测量“机器人时钟 − B 时钟”，
并用实测机器人时钟校验 joint / pose / collision / cuff 的源时间戳。机器人重启后的
几十毫秒时钟差会在下一次测量（≤2 s）后自动消除，不再需要重启 timesyncd 或手动跑命令；
A 断开重连即可。启动 B 时终端会打印一行：

- `Robot clock measured via NTP: robot - B = +85.0 ms (+/- 0.3 ms)…`：已自动补偿；
- `WARNING: robot clock NTP measurement failed (…)`：暂按 B 自身时钟（旧行为），后台每 2 s 重试。

门限不变：残差超前 50 ms、joint / pose 旧于 100 ms、collision 旧于 500 ms 仍拒绝；测量误差
（往返一半）只会让数据显得更旧。往返 > 50 ms 或超过 10 s 未成功测量时不使用补偿。
当前测量值写在每条反馈日志的 `feedback_diagnostics.robot_clock`。只有出现 WARNING 且
A 仍报 `feedback_latest_expired` 时，才按下面的手动流程排查。

## 再次出现反馈时间错误：现场快速恢复命令（手动后备）

适用：重启机械臂后出现 `feedback_latest_expired` 或 `*_source_time_invalid`，
且本机已经配置好跟随 `sawyer.local` 的 NTP。以下是恢复尝试；相同页面错误也可能
有其他原因，不能只凭错误名称认定时钟故障。

### 1. 断开 A，停止当前 B

在 A 页面断开连接；到自己启动 B 的终端按 `Ctrl+C`，等待退出。
不要停止其他人的实例，也不要在运动过程中调整系统时间。

### 2. 重新触发时间同步

```bash
sudo systemctl restart systemd-timesyncd
```

等约一分钟，再检查：

```bash
timedatectl timesync-status
```

确认 Server 仍为 `sawyer.local`（本次现场 IP 为 `10.101.119.178`），
Poll interval 在 16–32 秒，Offset 已明显收敛。配置已保存在系统中，
**不需要每次重写 NTP 配置，也不需要安装 chrony / ntpdate**。
等满一分钟或显示 synchronized 并不自动代表 ROS 反馈已经健康。

### 3. 启动最新版 B

进入本机实际使用的最新版 B 目录；以下目录名是本次现场示例，复制新包后应改为新路径：

```bash
cd /home/k23114984/Lin/B-Lab-Ubuntu-20261102
bash Start-B.sh
```

不要只照抄旧目录名而启动旧包。启用夹爪串口时，启动 B 会初始化夹爪并开启扭矩。
检查 B 没有启动异常，新反馈持续有效后，再让 A 重新连接 / 校准。

### 4. 若仍失败，收集这些输出

保持 B 运行，在另一个终端执行（不需要加载 ROS 环境）：

```bash
timedatectl timesync-status
tail -n 1 ~/.local/state/sawyer-teleop/feedback.jsonl
```

把这两部分、A 页面错误和 B 启动输出一起提供，不反复 Reset 或继续扩大容差。
日志需是当前运行实例的最新记录；`sample.valid=true`、`fault=null`、seq 持续增长
才说明 B 实际反馈在恢复。若仍是源时间错误，按下节完整手册测量三路 ROS 源年龄；
若 B 健康而 A 仍过期，再查 A 的接收与传输路径。

## 重启机械臂后反馈过期：排查与恢复手册

记录日期：2026-11-02。此节归纳本次代码检查和用户现场输出，不表示已经完成
自动恢复功能或实体运动验收。以后遇到相同错误，先核对当前版本和新证据。

### 问题是什么

A 能连接 B 的 TCP 端口，不代表 B 已拿到可用于控制的机器人反馈。
本次机器人与 Ubuntu B 的时间差导致 B 拒绝 joint / pose / collision 消息，
A 随后报 `feedback_latest_expired`；不应把这个表层错误直接解释为网络延迟。

| 证据 | 本次含义 |
| --- | --- |
| `*_source_time_invalid` | 源时间零值、非有限、过旧或超前超过当前容差；必须测量年龄才能区分 |
| `collision_source_time_invalid` | 碰撞消息时间无效，不等于真的碰撞；真实碰撞报 `robot_collision` |
| `seq:0`、`captured_ns:0`、q / pose 为 null | 尚未接受有效运动反馈；巨大 age 是零时间基准造成的派生数值 |
| seq 只到 1，最后有效反馈约 28 秒前 | 后续消息持续被拒绝，并非只丢了一帧 |
| `gripper.supported:true, valid:true` | 夹爪驱动已接入并有有效反馈，不代表已提交开合动作 |
| `gripper.supported:false` | 当前 B 没启用夹爪；另查运行目录、启动参数与 env |

当前 A 的反馈检查先检查年龄、再检查 sample.valid，因此源反馈无效的详细原因
可能被 `feedback_latest_expired` 遮住；应直接看 B 日志中的 sample.fault 和
feedback_diagnostics.invalid。

### 已经改过什么，哪些没有改

- **代码 / deploy 已改**：B 的 joint / pose / collision / cuff 源时间允许最多
  50 ms 超前。关节 / 位姿 100 ms、碰撞 500 ms 的旧反馈检查保留；原始时间排序、
  重复不刷新、STOP / 碰撞 / 手导 / 关节保护保留。负年龄不会生成未来的本机 capture。
  这是允许有界时钟误差，不是精确对时，不能声称真实采集年龄完全没有测量误差。
- **Ubuntu 系统已由用户修改**：B 使用 `sawyer.local` 做 NTP 时间源；轮询
  16–32 秒。调整 NTP 本身没有修改 Python 代码。
- **尚未实现**：启动时等待多路反馈持续健康再宣布就绪、直接显示各路源年龄、
  机器人重启后自动确认恢复。当前启动脚本只检查本地依赖、ROS master 与 IK 服务，
  不能据“端口已监听”就宣称反馈健康。
- **没有采用**：1 秒容差或禁用源时间校验。允许很大的超前会掩盖滞留的旧反馈。

固定网络与串口配置见下一节。NTP 文件在 Ubuntu 的 `/etc/systemd/`，不会随 deploy
或 real-b.env 一起迁移。新终端的 ROS 环境也不会因 B 已启动而自动加载。

### 每次故障先收集证据（只读）

先让 A 保持断开，不反复 Reset。以下检查不打开串口、不使能、不发送运动：

```bash
hostname
ip -br -4 addr
timedatectl timesync-status
cat /etc/systemd/timesyncd.conf.d/90-sawyer.conf
tail -n 1 ~/.local/state/sawyer-teleop/feedback.jsonl
```

在实际准备启动的 B 目录中另查：

```bash
pwd
grep -E '^SAWYER_(ROBOT_HOST|ROS_IP|BIND_IP|WORKSPACE|B_PYTHON|GRIPPER_PORT)=' config/real-b.env
grep 'FEEDBACK_FUTURE_TOLERANCE_S' src/sawyer_teleop/intera_backend.py
```

核对日志 utc 是否新鲜；重启后 run_id / pid 应对应新实例。若怀疑实际启动的是另一份包，
对仍存在的日志 pid 使用 `readlink -f /proc/实际PID/cwd` 确认目录，不只检查磁盘上任意副本。
不要把此前成功的日志当作当前状态。

如果三路仍报源时间错误，在同一个新终端先加载环境（下面为本次实验室路径）：

```bash
source /opt/ros/noetic/setup.bash &&
source /home/k23114984/sawyer_ws/devel/setup.bash &&
export ROS_MASTER_URI="http://sawyer.local:11311" &&
export ROS_IP="10.101.119.182" &&
unset ROS_HOSTNAME
```

然后直接测量消息到达时的源年龄，而不是先 echo 消息、再手工运行 date：

```bash
timeout 10s python3 - <<'PY'
import rospy
from sensor_msgs.msg import JointState
from intera_core_msgs.msg import EndpointState, CollisionDetectionState

rospy.init_node("feedback_time_probe", anonymous=True)
counts = {}

def callback(msg, name):
    counts[name] = counts.get(name, 0) + 1
    if counts[name] <= 3:
        now = rospy.Time.now().to_sec()
        stamp = msg.header.stamp.to_sec()
        print("%s age_ms=%.3f stamp=%.9f now=%.9f" %
              (name, (now-stamp)*1000, stamp, now), flush=True)

subs = [
    rospy.Subscriber("/robot/joint_states", JointState, callback, callback_args="joint"),
    rospy.Subscriber("/robot/limb/right/endpoint_state", EndpointState, callback, callback_args="pose"),
    rospy.Subscriber("/robot/limb/right/collision_detection_state", CollisionDetectionState,
                     callback, callback_args="collision"),
]
rospy.Timer(rospy.Duration(5), lambda _: rospy.signal_shutdown("done"), oneshot=True)
rospy.spin()
PY
```

负 age 表示源时间超前；当前 joint / pose 应在 -50～100 ms、collision 应在
-50～500 ms 内，此外仍须通过值、排序及机器人状态检查。若运行的代码版本不同，
先查实际阈值。`invalid ROS_IP`、`http://:11311` 表示探针终端环境没加载好，
不能据此判断运行中的 B 断连。

### 本次恢复方法：让 B 跟机器人同步

本次用户的只读 NTP 查询确认机器人 `10.101.119.178 (sawyer.local)` 提供服务。
其他机器人 / 环境需重新核对，不猜测设备地址或默认安装时间服务。
对已存在的 90-sawyer.conf 先备份。保持 A 断开，由操作者停止自己运行的 B，
再修改系统时间同步，避免控制过程中调整时钟：

```bash
sudo mkdir -p /etc/systemd/timesyncd.conf.d
sudo cp -a /etc/systemd/timesyncd.conf.d/90-sawyer.conf /etc/systemd/timesyncd.conf.d/90-sawyer.conf.bak
printf '[Time]\nNTP=\nNTP=sawyer.local\nFallbackNTP=\nPollIntervalMinSec=16s\nPollIntervalMaxSec=32s\n' | sudo tee /etc/systemd/timesyncd.conf.d/90-sawyer.conf
sudo systemctl restart systemd-timesyncd
```

首次安装没有原文件时跳过 cp，不必因备份不存在而停下。这里采用单行 printf，
避免先前粘贴 `<<'EOF'` 漏引号导致终端一直显示 `>` 的问题；遇到这种情况按 Ctrl+C
退出再完整输入。配置项说明见 [systemd 官方文档](https://www.freedesktop.org/software/systemd/man/247/timesyncd.conf.html)。

等约一分钟，再查看 `timedatectl timesync-status`，确认 Server 指向机器人，
Poll interval 在 16–32 秒，并复测 ROS 源年龄。若尚未稳定，保留新日志继续定位；
不要因已等满一分钟就认定成功，也不要自动扩大容差。

本次最终用户输出为 Offset=-111 us、Delay=215 us、Jitter=41 us、2 个采样包，
说明当时 NTP 状态明显改善；这不是长期稳定或完整遥操验收的证据。

### 恢复完成的判据与启动顺序

1. 当前 IP / 工作空间 / 串口配置与实际电脑匹配，确认使用最新版 B。
2. NTP 正确且 ROS 三路源年龄满足当前阈值，不只看 synchronized=yes。
3. 由操作者启动 `bash Start-B.sh`；启用串口时会初始化夹爪并开启扭矩。
4. 比较几条新反馈日志：seq 持续增长、captured_ns 更新、sample.valid=true、fault=null。
   新日志连续健康后，A 再连接 / 校准；实体动作按现场授权执行。
5. 若 B 已有效而 A 仍过期，再调查 A 接收间隔、时钟估计不确定度与传输 / 排队，
   不继续把所有错误归为 NTP。若夹爪 unsupported，独立检查其配置。

配置保存后 Ubuntu 重启仍会加载，但机器人重启可能再次造成短暂时间变化，
不能保证永不故障。当前手册提供恢复路径，没有实现启动自动健康门或自动恢复。

### 可直接复用的排障 prompt

```text
请接手 Sawyer 重启后遥操反馈失效问题，按现有证据定位并给出具体恢复步骤。
项目在 D:\project\Sawyer\Sawyer-Workspace\sawyer_teleop。
先遵守 AGENTS.md；若入口指定的 ROG_CODEX_HANDOFF.md 不存在，说明后读取现存
AGENTS / docs/PLAN.md 最新决定，以及 docs/DEPLOYMENT.md 的“重启机械臂后反馈过期”手册。

已知历史：Ubuntu B 为 rll3-OptiPlex-7050 / 10.101.119.182，机器人 sawyer.local
此前解析为 10.101.119.178，ROS Noetic，工作空间 /home/k23114984/sawyer_ws。
这些值必须与当前输出核对，不能假定地址、路径和运行版本未变。
固定配置位于源码同级 windows-local/config/real-b.env，导出默认沿用它；保留现场配置。
当前源码 B 允许最多50ms源时间超前；joint/pose旧反馈100ms、collision500ms。
曾有83–85ms超前，导致三路 source_time_invalid，再被A显示为feedback_latest_expired。
Ubuntu曾配置 /etc/systemd/timesyncd.conf.d/90-sawyer.conf：
[Time]，NTP= 清空列表后 NTP=sawyer.local，FallbackNTP=，
PollIntervalMinSec=16s，PollIntervalMaxSec=32s。NTP不是deploy的一部分。

先只读收集当前网络、timesync状态/配置、B实际进程目录和版本、real-b.env、
最新feedback.jsonl（核对utc/run_id/pid），必要时测三路ROS消息到达时的age_ms。
新终端先完整加载ROS环境；给出一次可复制的成组命令，避免反复追问已有信息。
区分源时间无效、源反馈中断、A传输/排队、旧部署/配置和夹爪未启用。
不要把NTP Offset直接当ROS年龄；captured_ns=0产生巨大age不代表网络延迟。
若证据仍是时钟问题，指导操作者备份配置、停止自己的B，再恢复NTP并复测；
给出B日志持续健康后才连接A的步骤。若不是时钟问题，按新证据查对应路径。
当前代码没有“启动等待多路反馈持续健康”的自动门，不能说它已经实现。
不自动操作真机、打开串口、使能/运动或停止别人的实例；不禁用时间/STOP/碰撞/
关节保护，不自动把容差改为1s。代码与本地部署更新可在定位后按明确范围实施，
保留配置、备份与校验，进度写PLAN，启动步骤写DEPLOYMENT，软件结果不当真机验收。

下面是本次最新报错和终端输出：
【粘贴页面报错、B启动输出、timesync-status和最新反馈日志】
```

## 查看 B 状态与断连原因（2026-11-10）

B 运行时在另一个终端只读查看（在仓库根目录 `~/Lin/sawyer-teleop` 下；直接用 `python3 -m` 会因缺少 PYTHONPATH 找不到模块）：

```bash
PYTHONPATH=sawyer_teleop/src python3 -m sawyer_teleop.cli monitor --status-file /dev/shm/sawyer-teleop-$(id -u)/intera-b.json --once
```

`Service error` 非空表示 B 执行出错；为空而 A 显示 `peer_closed` 时看 `Recent events`：
更新后的 B 在断开前会记一条 `connection_closed: <原因>`，B 终端也打印 `A connection closed: <原因>`。
常见原因：`send_failed:timed out`（B 向 A 发送卡住超过 0.2 s，多为 Wi-Fi；2026-11-10 起由 0.1 s 放宽）、`receive_failed:partial_frame_timeout`、`peer_closed`（A 主动断开）。

## Quest 无线连接（可选，2026-11-09，未经真机验证）

1. Quest 与 A 电脑连同一网络；Quest 先用 USB 连 A，并在头显里允许 USB 调试。
2. Web 界面处于未连接状态时，点 Connections 卡片 Quest serial 旁的 **Wireless**；日志出现 `Quest wireless ADB ready: IP:5555`、按钮下方显示 `Quest (Wi-Fi): Authorized · IP:5555` 即连上，序列号已自动填好并保存。
3. 拔掉 USB 线，再点 Connect。
4. Quest 重启或换网络后无线失效：重新插 USB、再点一次 Wireless。要改回 USB，在 Quest serial 里选 USB 序列号（或清空）后再连接。
5. 若频繁出现 `ADB latency too high to verify input age` 或 `quest_stale`，说明无线延迟不够，改回 USB。

## A 端两路相机（Windows，2026-11-07）

Web A 启动后常驻采集 `operator.json` 的 `cameras`，在 Live status 下方并排显示；勾选 Record cameras 时录制复用同一采集。

- **RealSense D435（workspace）**：按 DirectShow 名 `Intel(R) RealSense(TM) Depth Camera 435 with RGB Module RGB` 打开，只取 RGB，免装驱动。找不到名字即报错，不会回退到笔记本自带摄像头。
- **Sandberg 腕部相机（0c45:627b）**：Windows 没有可用签名驱动，用系统自带 WinUSB + 项目内用户态读取器（`backend: "sn9c20x"`）。首次或**换了 USB 口**后需在设备管理器指定一次：
  “USB20 Camera” → 更新驱动程序 → 浏览我的电脑 → 让我从列表中选取 → Universal Serial Bus devices → 厂商 / 型号均选 “WinUsb Device” → 警告点“是”。
  **必须直接插电脑 USB 口**：经 WCH 扩展坞（1a86:8095）时高带宽传输约 2 s 后掉线。成像参数在 `wrist.controls`。默认值由用户指定为 `exposure 100 / gain 3`（2026-11-09；未在任务桌面实测饱和情况），红蓝平衡保持原 33 / 28，不做颜色矫正（用户要求腕部输出传感器原始颜色）。代码支持可选的 `color_correction`（主机端 3×3 RGB 矩阵），默认不配置即不处理。Linux 的 `sandberg_camera/camera_profile.json` 未同步。原始 Bayer 模式下 `brightness` / `contrast` / `saturation` 不生效，只有曝光、增益、红蓝平衡和少量伽马有效。
- 依赖：Windows 运行时需 `libusb1==3.4.0`（`Setup.cmd` 会按 pyproject 安装；`Test.cmd env` 自检会导入 `usb1`）。
- 同一相机同一时间只能被一个 Web A 占用；改配置或代码后直接再双击 Start-A：会自动停止旧实例（断开会话、释放相机）再启动新的（2026-11-09 起）。正在录制时会拒绝重启，先 Save & End 等处理完。若旧实例无响应，仍需在原窗口 Ctrl+C。

## 实验室 Reset arm 姿态（2026-11-07，2026-11-09 按现场当前关节角更新）

实体 Sawyer 的 Reset arm 目标改为现场实测姿态（rad，`right_j0`–`right_j6`）：
`1.2704287109375, -1.1166728515625, -1.3760634765625, 1.06116015625, 0.5706650390625, 1.9465263671875, 3.327021484375`。
来源唯一为 `config/lab-neutral-pose.yaml`：`Start-B-Real.sh` 在只读图检查后 `rosparam load` 为
`/named_poses/right/poses/neutral`，B 启动时读取一次，经握手 `hello.neutral_pose` 发给 A；A 不保存旧值。
J6=3.327 rad 超过 ±π，只适用于实机 ±4.7124 模型，不用于 Gazebo（Gazebo 仍用镜像内 named pose）。

生效步骤：A 断开 → 停止旧 B → 用新 deploy 替换实验室 B 的 `config/lab-neutral-pose.yaml`
（或整个目录，保留现场 `config/real-b.env`）→ `--check` → 重启 B → A 重新连接 / 校准。
只改 A 端文件或只改 YAML 而不重启 B 都不会生效。启动后可只读确认：
`rosparam get /named_poses/right/poses/neutral`，以及 A 界面 Reset 可用。
首次实际 Reset 会让机械臂运动到新姿态，需操作者确认路径无障碍、急停可用；本轮未做实机验证。

## 固定实验室配置（2026-11-02）

用户从实验室现有配置确认：机器人 `sawyer.local`；B 的 ROS_IP / BIND_IP
均为 `10.101.119.182`；ROS Noetic；工作空间 `/home/k23114984/sawyer_ws`；
Python 留空自动选择。夹爪串口根据用户现场设备枚举更新为
`/dev/serial/by-id/usb-FTDI_USB__-__Serial_Converter_FTAO9VMQ-if00-port0`
（当前指向 ttyUSB0）；这是现场枚举得到的设备路径，尚未验证通信或 VR 联合动作。
固定副本保存在开发工作空间
`windows-local/config/real-b.env`（源码目录的同级，不入库）。

后续运行 `scripts/export_deployment.py --output 新目录` 默认把该固定副本
逐字节复制为包内 `config/real-b.env`；示例模板保持通用空地址。
没有固定副本的其他机器仍使用示例模板。需要其他实验室配置时用
`--config 实际env路径` 显式选择；需要通用包可指定 `--config config/real-b.env.example`。
更新固定配置后须重新导出；脚本仍只生成新目录，不覆盖已有部署。
复制前确认现场网络地址没有变化。实验室若再次改配置，先将实际 env 同步到固定副本。

若只更新实验室已有目录，在其 `config/real-b.env` 中将
`SAWYER_GRIPPER_PORT` 设为上述完整设备路径，不保留旧空值。
加载配置需停止旧 B、启动新 B，A 重新连接 / 校准；启动会初始化夹爪并开启扭矩。
先确保独立夹爪测试程序不占用串口，B 解释器已安装 `[gripper]` 依赖且用户具备串口权限。
现场操作需由操作者执行，本轮仅配置 / 导出，不打开串口。

## Demo 反馈时间超前容差 50 ms（2026-11-02）

用户明确接受 demo 中的小幅时钟误差。当前 B 对 joint / pose / collision /
cuff 反馈统一允许源时间最多超前 50 ms，替代初版仅 collision 的 1 ms 容差。
这是时间戳校验范围的有界放宽，不是时钟偏差估计或修正；仍建议保留已经设置的
机器人 NTP 同步。此前 83–85 ms 或 116 ms 的超前仍会拒绝，不能据 NTP Offset
单独保证 ROS 反馈满足容差，应以实际 ROS 消息年龄为准。

关节 / 位姿的旧于 100 ms、碰撞旧于 500 ms、时间倒退、重复消息不刷新、
真实碰撞 / cuff 手导和机器人未就绪保护保留。负年龄按 0 换算本机 capture，
不会生成未来单调时刻或给已有运动引用续期；源年龄仍存在时钟偏差造成的测量误差，
不能据此声称保留了跨机器精确 100 ms 的真实采集年龄保证。

仅更新 B，A 兼容，无配置或依赖变动；复制当前 deploy 后按原流程重启 B、
A 重连 / 校准加载。不要用旧 zip 覆盖当前带有夹爪端口的 env。
本轮只做离线软件验证与本地部署更新，不自动执行现场重启或实体动作。

## 初版碰撞反馈微小时钟超前修复（已被上节替代）

仅更新 B，A 兼容，无新增依赖、配置或协议。碰撞状态最多允许源时间超前 1 ms；
其他反馈不使用该容差。碰撞反馈的 500 ms 过期、时间倒退和真实碰撞保护保留。
容差内消息以收到时刻为最晚捕获时刻，原始源时间戳保留，不延长有效期。

本轮只完成离线验证；新导出包是候选，未完成 ROS / 真机现场验收。
将候选包复制为 Ubuntu 上的新目录，保留旧目录；把正在使用的旧目录
`config/real-b.env` 复制到新目录同名位置，保留地址、工作空间和夹爪串口配置。
加载候选需按原流程停止旧 B、启动新 B，A 断开后重新连接 / 校准。
这些现场操作未由本轮自动执行。先用只读反馈检查确认故障解除；实体运动需另行授权。

## 实体 B 的外接 Dynamixel 夹爪

2026-11-02：本次接入仅完成软件验证，不包含 VR 与实体夹爪的联合验收。
硬件参数来自用户提供并确认已真机测试的 `gripper-integration/`。

## 配置与启动

更新实验室 B 的源码；当前 A 已会发送夹爪命令，兼容本次 B，无需更换 APK。
只有使用新增 ticks 录制字段时才需同时更新 A 的 `recording.py`。
在 **B 实际使用的 Python 环境**安装可选依赖，例如项目 `.venv-b`：

```bash
.venv-b/bin/python -m pip install '.[gripper]'
```

在现有 `config/real-b.env` 加入实际串口（空字符串表示禁用，旧安装行为保持）：

```bash
SAWYER_GRIPPER_PORT="/dev/serial/by-id/实际设备名称"
```

确认 B 用户具备该串口权限，并停止占用同一串口的独立夹爪测试程序。
先运行 `bash scripts/ubuntu/Start-B-Real.sh --check` 仅检查依赖；它不打开串口，也不验证夹爪在线。
之后按原实验室流程重启 B、重连 A 并校准。启动真实 B 会初始化串口、配置电机并开启夹爪扭矩；本轮未执行该步骤。
也可使用现有 bridge CLI，额外传 `--gripper-port /dev/serial/by-id/实际设备名称`；仅 Intera 后端接受此参数。

## 操作语义

- 右手柄 **Grip（侧握键）**仍为机械臂运动许可；只在有效握持、当前会话与目标期限检查通过时提交夹爪命令。
- **Trigger（食指扳机）**小于 0.3 打开（3766 ticks），大于等于 0.3 闭合（1024 ticks）。这是二值开合。
- Standard / Fast follow / Intera Cartesian / Direct pose 共用同一夹爪驱动，不因切换模式自动开合。
- 松开 Grip、STOP、输入过期或断连：取消旧意图，以新鲜位置反馈保持。松开 Trigger 才是打开命令；需保持 Grip 许可。
- 不在机械臂暂停时独立操作夹爪。夹爪命令随合法运动目标提交，IK 暂停时同样暂停。
- 串口 worker 使用最新意图，不排队追赶；重复命令不重启动作。位置轮询通常 20 ms，连续 3 次处于 ±20 ticks 判到位。
- 闭合停滞沿用参考代码的 0.8 s / 0.6 s / 5 ticks 条件，有至少 20 ticks 净闭合量才报告 `contact_candidate`；这不能确认抓取成功。
- 打开受阻或持续运动超过 10 s 会保持并锁存故障；通信故障与超过 150 ms 的夹爪反馈失效会使 B 暂停机械臂。故障需要排查并重启 B。
- worker 另有与 B 命令期限相同的意图看门狗，防止主循环停止后继续追赶目标。SDK 收包等待封顶 50 ms、串口写超时 50 ms；操作系统/USB 延迟仍可能超过该值。

## 暂停与退出

暂停、正常退出均尝试读取当前位置并写回保持，**保留扭矩**，退出只关闭串口。
这与参考 `close()` 的卸扭矩行为有意不同，避免退出时自动释放物体；退出后也不能认为电机已卸力。
初始化先关闭扭矩、写模式/PWM/速度，再将当前位置写为目标并开启扭矩，不执行打开→闭合→打开演示。
串口已失联、进程被强杀或系统断电时，无法保证软件保持成功；B 日志/快照会保留可检测的故障。

状态缓存提供 `position_ticks`、`target_ticks`、状态、故障、时间与编码器端点；原始状态/事件日志保留这些信息。
A 更新后，HDF5 另存 `gripper_position_ticks` / `gripper_target_ticks` / `gripper_ticks_valid` / `gripper_feedback_age_ms`
和 0–1 编码器刻度 `gripper_open_fraction`。未标定指间距，不再写米单位占位字段（2026-11-09）。

## 联合验收待办

现场分别验证初始化无意外开合、空夹开闭、夹物后的可能接触、反向抢占、松 Grip 保持、STOP、断连与重连不重放、USB 故障和录制 ticks。
逐一覆盖实际使用的运动模式，保留反馈与失败记录；用户原夹爪实测不代替本次 VR 联合验收。
