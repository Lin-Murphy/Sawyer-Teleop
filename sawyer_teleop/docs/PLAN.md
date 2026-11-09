# 项目状态

更新：2026-11-09。当前状态以本页为准；安装见 [SETUP](SETUP.md)，操作见 [USAGE](USAGE.md)，开发与验证见 [DEVELOPMENT](DEVELOPMENT.md)。

> 2026-10-29 发布整理：Windows 入口合并为仓库根目录的 `Setup.cmd` / `Start-A.cmd` / `Start-B.cmd` / `Test.cmd`；
> 文档合并为 SETUP / USAGE / TROUBLESHOOTING / DEVELOPMENT。下文历史条目里的旧脚本名（Install-Dev、Test-A、Stop-B、Install-Quest 等）、
> 旧文档名和 `rog-local/`、`windows-local/artifacts/` 证据链接均指整理前的原作者本机环境，未随仓库发布。

## 当前决定

### "Reset arm between episodes" 网页默认勾选（2026-11-09）

**仅更新并重启 A。** 用户要求该复选框默认勾选：`web/index.html` 的 `resetBetween` 加 `checked`，USAGE 同步。
后端 `reset_between` 缺省仍为 False（接口不传即不复位），录制逻辑不变。注意：勾选时若 B 不提供 Reset 姿态会拒绝开始录制，
当前 ROG 上 B 即提示 "Update B to provide its official neutral pose."，需先换成当前源码的 B 或手动取消勾选。未跑测试，未在浏览器确认。

### 重复运行 Start-A 自动重启旧实例（2026-11-09，仅软件验证）

**仅更新 A；本次需最后一次手动在旧 Start-A 窗口 Ctrl+C（旧代码没有停止接口），之后即可直接双击重启。** 用户要求打开 Start-A 时若已在运行则自动重启。
原行为是复用旧实例并打开其页面。现改为：新进程拿不到配置锁时，通过已验证的本机实例（令牌鉴权）POST `/api/shutdown`；
旧实例在**录制 / 处理中拒绝**（防止截断数据，新进程打印原因并退出），否则记录日志、停止 HTTP 服务、按原逻辑强制断开会话并释放相机 / 锁，
旧窗口退出码 0 自动关闭；新进程每 0.3 s 重试拿锁，最多 45 s，拿到后正常启动并打开新页面（旧页面令牌失效）。
旧实例无响应时不杀进程，提示到原窗口 Ctrl+C。注意：已连接的会话会被直接断开（与 Stop & disconnect 相同），需重新连接、校准。
测试：`test_web_startup.py` 改写复用相关用例为重启用例（含两真实进程接管、录制中拒绝），与 `test_web.py` 共 29 条通过（未跑完整套件）。
哈希前 12 位：`web_server.py` `f6812989ee45`、`web_instance.py` `879d4ed4dad0`。未在 ROG 带相机 / Quest 实测重启耗时。

### Web 一键切换 Quest 无线 ADB（2026-11-09，仅软件验证）

**仅更新并重启 A；B 无需更新，无需重校准。** 用户要求在 Web 界面加按键把 Quest 改为无线连接。
Connections 卡片 Quest serial 旁新增 `Wireless` 按钮（动作 `wireless`）：Quest 须先经 USB 连接并已授权，A 依次执行
`adb -s <USB序列号> shell ip -f inet addr show wlan0` 取 Wi-Fi IPv4 → `adb tcpip 5555` → 每秒 `adb connect IP:5555`（最多约 10 s），
`adb devices` 显示 `IP:5555 device` 才算成功；随后自动填入并保存 `quest_serial=IP:5555`，USB 线可拔。
只允许在未连接时执行（`tcpip` 会重启头显 adbd，会切断会话中的 Quest 输入），与 Find 互斥。
新增 `devices.enable_wireless` / `quest_wifi_ip` 及 `test_devices.py` 4 条、`test_web.py` 1 条；相关测试 `test_devices` / `test_web` /
`test_quest_connection` 通过（未跑完整套件）。哈希前 12 位：`devices.py` `49698aa25353`、`web_session.py` `da4fb879e416`、
`web/app.js` `12d78e90fb06`、`web/index.html` `503252bf03c5`。
**未用真实 Quest 验证**：无线下 ADB 时钟同步往返变大，可能触发 `ADB latency too high to verify input age` / `quest_stale`。
Quest 重启后无线失效，需重新插 USB 再点一次。
补充（同日）：按钮下方状态行改为按连接方式显示 `Quest (USB): …` / `Quest (Wi-Fi): …`（`QuestConnection` 新增 `link` 字段，序列号含 `:` 即 Wi-Fi，
经 `/api/state` 的 `quest_link` 下发）；新增 `test_monitor_reports_link_type`。哈希：`quest_connection.py` `77424226a9d8`、
`web_session.py` `92a6320dcf36`、`app.js` `bfe09cc35c80`、`index.html` `d9d42959f6e3`；`test_quest_connection` / `test_web` / `test_web_startup` 33 条通过。下一步：ROG 上真机点按钮，记录连接耗时、同步往返和 `quest_stale` 频率，决定无线是否可用于遥操。

### 修复 `partial_frame_timeout` 误判断线（2026-11-09）

**仅更新并重启 A；B 无需更新。** 上一条改动上线后，ROG 实测徽标显示 `B disconnected: receive_failed:partial_frame_timeout`。
原因是 `transport.py` 读线程的半包计时：只有某次读完后缓冲区恰好为空才清零。B 持续高频发送 state（含 feedback stream）时，
每次 recv 大多停在某帧中间，虽然帧一直在正常完成，计时却从不清零，0.5 s 后被误判为半包超时并断开。
修复：计时只针对当前未完成的那一帧，本次读取完成了任意一帧就重新计时；真正卡住的半包（无任何帧完成）仍在 0.5 s 后断开。
新增 `test_busy_stream_ending_reads_mid_frame_is_not_a_partial_timeout`：用旧逻辑跑会 WinError 10053 断线（复现），
新逻辑通过；原有 `test_slow_partial_frame_does_not_occupy_connection_forever` 仍通过；完整测试通过。
transport.py sha256 前 12 位 `76ae7ae06fd4`。B 的部署副本有相同读逻辑（B 接收 A 的小消息，触发概率低），本次未改 B。
**未在 ROG 上验证。** 下一步：ROG 重启 A 复测；若改由 B 侧报断开，再同步此修复到 B。

### B 连接频繁断开：ping 超时不再断线，断连原因可见（2026-11-09）

**仅更新并重启 A；B 无需更新。** 现象：Web 上绿点 + "B disconnected"，即会话仍为 connected 但 A↔B 传输已关闭，
界面看不到原因。代码排查发现：A 每 2 s 发 3 次时钟同步 ping，单次 0.3 s 未回就关闭整条连接（`request_timeout:ping`）；
ROG 上 A/B/Gazebo 同机，B 偶尔卡顿即可触发，且同步线程随即退出。另一个问题：连接关闭后读线程报 "not a socket"
会覆盖最初的断连原因。改动：
- `client.py`：ping 超时只跳过该次采样、计入 `ping_timeouts`，不关闭连接；同步线程遇超时继续而非退出。
  hold / arm 等其他请求仍保持超时即断开。持续无法同步仍会在 10 s 后由 `feedback_clock_sync_expired` 失效保护。
- `transport.py`：连接已关闭后不再覆盖 `error`，保留最初原因（A 侧生效；B 部署副本本就落后于源码，未同步）。
- `operator.py` / `web_session.py` / `app.js`：视图新增 `robot_disconnect_reason`；断开时徽标显示
  "B disconnected: 原因" 并标红，事件日志记录一次 "B connection lost: 原因"。
- 未改：发送 socket 超时 0.1 s（`send_failed:timed out`）、半包 0.5 s，二者影响控制时效，待看到实际原因再定。
源码哈希（sha256 前 12 位）：client `229b4e98a222`、transport `401cd7e18675`、operator `6205b4e1c7f2`、
web_session `e54c2a704a58`、app.js `f243e6dc0a27`。新增测试 `test_slow_ping_does_not_drop_b_connection`、
`test_disconnect_reason_reaches_operator_view`、`test_lost_b_connection_is_logged_once_with_its_reason`；完整测试通过
（其中一次完整运行 `test_dataset.py::test_export_excludes_quality_incomplete` 偶发失败，单独与重跑均通过，与本改动无关）。
**未在 ROG 上验证**；页面 JS 本机无 node，未做语法检查。
下一步：ROG 上重启 A，复现断连时记下徽标/事件日志里的原因；若为 `send_failed` 或 `peer_closed` 再针对处理。

### 录制改为分文件夹原始数据，不再生成 raw.hdf5（2026-11-09，软件验证，实机待复验）

**仅更新 A 并重启 Web A；B 无需更新。** 现场 `batch_20261109_180708` 第一段在 7.25 s 出现 `recording_queue_full`，
相机、状态、反馈、命令、事件同时停止；A 主循环实际约 70 Hz（配置 120 Hz），说明 A 进程 CPU 饱和。
11-07 只扩大了缓冲（约 3.6 s 图像），没有解决写入过慢：h5py 写入与 LZF 压缩持有 GIL，与控制、相机、预览线程争抢。
用户决定：录制时不生成 HDF5，直接保存分文件夹的原始数据。

- **新格式（schema 3，`storage_format=folder_raw_v1`）**：`states.jsonl`、`commands.jsonl`、`feedback_joint.jsonl`、
  `feedback_pose.jsonl`、`events.jsonl`、`cameras/<name>/frames.rgb`（原始 RGB 逐帧追加，无损）+ `frames.jsonl` + `info.json`、
  `manifest.json`（元数据、初始状态、质量、`complete`、`writer_stats`）。字段与 schema 2 相同，NaN 存为 null。
- **mp4**：保存后由独立的 spawn 进程从 `frames.rgb` 生成 `cameras/<name>/video.mp4`，结果写 `videos.json`；失败不影响原始数据，
  可用 `episode_store.make_videos(<episode>)` 重做。
- **写入监测**：manifest `writer_stats` 记录队列最高水位、溢出时刻与当时积压、各类数据写入总耗时 / 最大耗时、忙碌比例。
- **读取**：新增 `episode_store.open_episode()`，新文件夹和旧 `raw.hdf5` 用同一套接口读取；对齐索引改为读取时计算，不再存储。
  `dataset.py` 导出、`motion_report` 已改用它；`scripts/` 下的旧迁移 / 评估脚本仍只读旧 HDF5。
- **验证**：按现场负载回放 20 s（两路 640×480@20 fps，约 680 条/秒）：写入线程忙碌 5.3%（11-07 HDF5 版本约 43%），
  队列最高 6 项、图像最高 1.8 MiB，`complete=true`、质量 `ok`；数据约 2.2 GB/分钟；mp4 20 s 数据转换 2.8 s、两路各 400 帧。
  全套 pytest 999 通过 / 23 跳过 / 0 失败（之后修了 mp4 临时文件名并新增 mp4 后台生成测试，该测试单独通过）。
  测试中注入写盘失败、关闭失败改为替换 `recording.open_file`；导出的 `excluded.path` 由 `raw.hdf5` 路径改为片段目录。
- 下一步：重启 Web A，录一段完整 60 s 实机数据，检查 `writer_stats.busy_fraction`、`quality` 和 `videos.json`。

### Web 相机曝光 / 增益滑条（2026-11-09）

> 同日用户要求暂时隐藏两路相机的滑条：仅在 `web/style.css` 末尾把 `.camera-controls` 设为 `display:none`，
> 功能与已保存的相机参数不变；删除该规则即可恢复显示。重启 Web A（或刷新页面）生效。

**仅更新并重启 A；B 无需更新。** 用户要求在 Web 上手动调两路相机的曝光和增益。每路画面下加 Exposure / Gain 滑条
（RealSense 另有 Auto exposure），松手经 `camera_controls` 动作生效并写回 `operator.json`；录制中拒绝修改，保证同一条录制参数不变。
腕部相机在 USB 子进程里直接写 OV7660 寄存器，不重开设备（此前频繁重开曾导致 USB 超时）；RealSense 用 DirectShow 属性。
RealSense 实测：曝光 -13..0 有效、增益上限 128、0.75 恢复自动曝光；滑条曝光上限取 -5，避免超过 30 Hz 帧时长。
经 CameraHub 在线调节 RealSense 亮度随之变化，未重开、保持 20 FPS。相机与 Web 测试全部通过。
**腕部在线调节未做真机验证**：测试时腕部相机物理断开（系统显示未连接），只有寄存器写入的单元测试。
页面 JS 本机无 node，未做语法检查，需在浏览器中确认。证据见 [验证记录](../../windows-local/artifacts/camera-sliders-20261109/verification.json)。
下一步：重插腕部相机、重启 Web A，在页面上拖动两路滑条确认生效与保存。

### Reset arm 姿态改为现场当前位置（2026-11-09）

**只需更新 B 的 `config/lab-neutral-pose.yaml` 并重启 B；A 无需更新。** 用户在 B 上 `rostopic echo -n 1 /robot/joint_states`
（stamp 1794246281.398，速度均为 -0.001，即静止）取得当前位置，按关节名取 `right_j0`–`right_j6`：
`1.2704287109375, -1.1166728515625, -1.3760634765625, 1.06116015625, 0.5706650390625, 1.9465263671875, 3.327021484375`，
原文精度写入，不取整。七轴均在实机限位内，离限位最近的是 J5（余量 1.03 rad）；J6=3.327 超过 ±π，只适用于实机 ±4.7124。
旧姿态 `0.9326, -0.9753, -1.8535, 1.3542, 0.5810, 1.9430, 4.0170` 作废。已同步源码与 `deploy/B-Lab-Ubuntu` 的 yaml、
`test_lab_neutral_pose.py`、两份 DEPLOYMENT.md，并更新部署清单中这两个文件的哈希；`test_lab_neutral_pose` /
`test_reset_arm` / `test_deployment_folders` 全部通过（2 跳过）。未在实机执行 Reset。
下一步：替换 B 上的 yaml → `--check` → 重启 B → A 重连；首次 Reset 前确认路径无障碍、急停可用。

### 腕部相机曝光与颜色（2026-11-09）

**仅更新 A（代码 + 配置）并重启 Web A；B 无需更新。** 旧参数 exposure 300 / gain 4 下任务桌面一半以上像素卡在传感器上限，
三通道上限不同（蓝最低），饱和的白色因此偏青绿。第一轮在地面场景调为 exposure 70 / gain 4，到任务桌面仍饱和，已作废。
第二轮在任务桌面上方实测：gain 4 时曝光降到 20 仍有 14% 饱和；改为 **exposure 150 / gain 0 / red 29 / blue 26**，饱和 0%。
原始 Bayer 模式下 brightness / contrast / saturation 不生效，因此新增主机端 `color_correction`（`cameras.py`，3×3 RGB 仿射，
预览与录制前统一应用，录制 `camera_config` 记录矩阵）。拟合：白桌面定三通道增益 1.120 / 1.151 / 1.125；
红方块和黄碗相对 RealSense 的饱和度差 2.2 倍，因 ×2 会放大镜头色斑，取 ×1.5。按 Web A 同路径实测两相机状态均为 ok，
腕部 p50 171 / p95 197、饱和 0%。两份 `operator.json` 已同步；Linux `sandberg_camera/camera_profile.json` 未改。
调参中腕部 USB 多次重开后超时，需人工重插才恢复。证据见 [调参记录](../../windows-local/artifacts/wrist-exposure-20261109/verification.json)。
**同日撤回颜色处理**：用户实看后认为颜色不对，要求腕部回到原始颜色。已从两份 `operator.json` 删除 `color_correction`，红蓝平衡恢复 33 / 28；保留 exposure 150 / gain 0（解决过曝）及代码中的可选矫正功能（不配置即不生效）。随后按用户要求曝光改回 300，增益保持 0（最终 exposure 300 / gain 0 / red 33 / blue 28，无颜色矫正）；300 / 0 未在任务桌面实测，增益 0 时 150 的桌面 p95 为 171。之后用户一度指定 280 / 6，**最终改为默认 exposure 100 / gain 3 / red 33 / blue 28，无颜色矫正**（两份 `operator.json` 已同步）；该组合未在任务桌面实测（参照：增益 4 时曝光 20–45 桌面仍有 14–34% 像素饱和，增益 3 略暗，界面可能仍提示 Overexposed）。下一步：重启 Web A 复看。

### 录制成功标记与夹爪字段（2026-11-09）

**仅更新并重启 A；B 无需更新。** 起因：检查 `episode_20261107T173703` 时发现 `task_success` 恒为 null、
`gripper_position_m` 全零且 `gripper_valid` 全 False（未标定占位）。决定：VLA 夹爪只学开 / 关，不做毫米标定。
操作员主动保存（Save / A 键 / End recording）写 `task_success: true`（失败示范按 B 丢弃）；到时自动保存、相机丢失、
退出、写入错误仍为 null。删除 `gripper_position_m` / `gripper_valid`，新增 `gripper_open_fraction`（编码器 0 闭–1 开，非间距）。
导出 demo 新增 `episode_task_success`，片段级 `task_success` 仍为 unknown。

相关 6 个测试文件 86 项通过（pytest 默认临时目录在沙箱内权限错误，改 `--basetemp` 重跑）；全量回归未跑。
`scripts/migrate_gripper_and_success.py` 已迁移 `windows-local/data` 下 10 条录制：8 条标为成功，
2 条 `recording_queue_full` 保持 null；全部删除占位字段并补 `gripper_open_fraction`。旧 zip 副本未改。
源码 / 结果哈希见 [验证记录](../../windows-local/artifacts/gripper-success-labels-20261109/verification.json)。
未运行 Gazebo、Quest 或真机。遗留：录制的 `task` 文字仍为空；2.3 s、3.3 s 两条很短，按约定已标成功，需人工确认。
下一步：A 重启后录一条，确认 manifest 的 `task_success` 与新夹爪字段。

### 本地改动合并核验（2026-11-08）

在远端 `f905149` 上整合 A 端相机录制缓冲、Reset 开夹和 8 个 Linux 相机参考文件。参考文件逐一核对与
`origin/multimodality_data` 的 Git 对象一致，5 个 Python 脚本通过语法检查。部署配置沿用远端版本。
首次全量回归 44 项失败、16 项初始化错误，主要为本机回环超时和反馈过期。合并后相关 103 项通过，
独立重跑全量 986 项通过 / 23 跳过 / 0 失败，固定实验室 7,177 条输入离线门通过。
输入、参数、源码哈希和原始测试报告见 `../../windows-local/artifacts/pre-push-20261108-144204/`。
未启动 Gazebo 或连接真机。下一步由现场操作者重启 A 后复验 60 s 录制及手动、批间 Reset。

### Reset arm 回中时张开夹爪（2026-11-07）

**仅更新并重启 A，兼容已启用夹爪的现有 B。** 无新增配置、依赖或校准步骤。手动与批间 Reset 共用现有回中路径。
B 声明支持夹爪时，A 随首个有效回中目标发送 `gripper=-1`，等机械臂到位且夹爪反馈确认张开后才进入完成或下一段录制。
实机使用 Dynamixel 的 `reached` 状态，Gazebo 使用两指位置。Stop、反馈失效或断连仍由现有 hold 路径同时停止回中和开夹。

以 `f405b54` 加本次改动的隔离副本验证，1,008 项中 985 通过 / 23 跳过 / 0 失败，含 44 项 Reset 用例。
覆盖已经在 neutral 时等待夹爪、手柄仍扣扳机时张开、批间自动开夹，以及 Stop / 断线 / 反馈过期后停止提交。
固定 7,177 条输入离线参考门全部通过。输入、源码和参数 SHA-256 见 [验证报告](../../windows-local/artifacts/reset-arm-open-gripper-20261107/verification.json)。
实机和 Gazebo 均未运行。下一步在实验室更新 A 后，验证手动 Reset 与批间 Reset 是否都能确认夹爪张开。

### 实机双相机录制写入队列溢出与缓冲优化（2026-11-07）

**仅更新 A 的 `recording.py` 和录制测试。** 无配置、依赖、文件 schema 或协议变化。录完并断开后重启 A 加载。
实体 B 已提供 schema 1 原生反馈流，A 按 120 Hz 操作循环、Standard 档、两路 20 Hz 相机录制。
`batch_20261107_172819` 的 60 s 片段因 `recording_queue_full` 停收，已保存为 `complete=false`。两路各保存 286 帧，
末帧约在 14.3 s。另保存 1,256 条状态快照、1,280 条提交记录，joint 和 pose 原生反馈各 1,431 条。
同一旧 A 在随后成功保存 16.8 s 片段后再次溢出，第二次失败文件中的数据只到 1.55 s，腕部 / RealSense 31 / 32 帧。
旧日志无法区分现场具体是哪一步卡住。核验时 D 盘剩余约 582 GB。此前 6 s 静止录制未覆盖完整现场负载。

- **离线基线**：复用现场两路 640×480 图像和手柄事件，按 680 条/秒重放相机、120 Hz 状态、100 Hz 两路原生反馈、
  90 Hz 命令等。25 s 无故障回放的队列峰值 27/256。注入一次 1 s 写入停顿后，旧代码在第 5.41 s 溢出，
  复现 `recording_queue_full`。旧队列只能吸收约 0.38 s 的同等输入。
- **改动**：复用 HDF5 批量写入缓冲，相机图像仍逐帧分块、LZF 无损，帧和时间戳一起按批落盘。
  队列扩大至 2,048 项并限制排队图像总计 128 MiB。在该负载下可吸收约 3 s 短时停顿，长期写入过慢时仍明确失败。
  继续用同一写入线程保存 MP4，不改采集频率、控制参数和完整性判定。
- **结果**：新版完整 60 s 回放包含同样的 1 s 停顿，峰值 702/2,048，`complete=true`，质量 `ok`。
  HDF5 两路各 1,200 帧逐像素一致，MP4 各解码 1,200 帧，帧序、时间戳、对齐索引和原生反馈行数均通过。
  每秒 HDF5 数据集写调用由约 703 降为 428。录制、反馈、数据集、Web 和相机相关 pytest 59 项通过，
  含图像内存上限回归。新版尚未在真实 60 s 操作中复验。

实验输入、源码 / 配置 SHA-256、基线失败、队列水位及复验见
`../../windows-local/artifacts/camera-recording-queue-full-20261107/`。下一步由用户结束旧会话后重启 A，
以新批次复验实际 60 s。重启当前工作区还会加载另一个会话在改的 Reset arm / 夹爪逻辑，该改动不属于本次录制优化。

### Raw 数据采集 schema 2（2026-11-07，软件验证通过，实机录制待验收）

**完整原生反馈录制需更新 A、B，现有 A/B 仍可互连。** 现场 B 尚未替换。先保存 raw data，再按各模型需要转换。
要求：初始状态、7 关节 q/dq、末端位姿和 twist（不要 effort、不要 wrench）、两路相机，图像 HDF5 无损 RGB 与 mp4 两份都存；
各路按原生频率录制，导出时再按时间戳对齐；掉帧参考 DROID：不补帧，只记录并标记，导出时丢弃无帧的时间步。
默认阈值：相机单次间隔 >250 ms 或掉帧 >5% 为 degraded；>1 s 无帧为 incomplete，停整批且不计数；反馈丢行 >1% 或间隔 >100 ms 为 degraded。

已改（源码）：新增 `feedback_stream.py`（B 端每条已接受的 joint_states / endpoint_state 进有界队列，带每路序号；
transport 的 latest 槽 `merge` 未发送的行；按会话 opt-in，旧 A/B 兼容，`feedback_stream_enabled` 可关）；
`intera_backend` / `gazebo_backend` / `backends.FakeBackend` 接入并加 `twist`、`head_pan`；`bridge`（hello 能力、
`feedback_stream` 请求、`drain_feedback_stream`）、`server._tick`、`client`（接收、`drain_feedback_stream`）；
`recording.py` 升 schema 2：`feedback/joint`、`feedback/pose`、`states/eef_twist`、`commands/command_kind|target_pos|target_quat`、
`frames/*/source_seq|open_count`、`camera_frame_gap` 事件、`alignment/<camera>/*_index|*_age_ns`、`quality`（attrs 与 manifest）；
`cameras.py` 加 `source_seq` / `open_count`；`operator.py` 加 `initial_state`（反馈 >100 ms 拒绝开始）、
批次间 Reset 的目标与误差、`required_cameras`（实机默认要求全部配置相机）、录制中相机丢失处理、质量为 incomplete 不计数；
`web_session` 传 `required_cameras` / `feedback_stream`，相机录制默认开启；`config/operator.json` 改 `record_cameras_enabled: true`；
`dataset.py` 导出排除 `quality_incomplete`。新增 `tests/test_feedback_stream.py`（12 项通过）；`tests/test_recording.py` 11 项通过。

- **全套软件测试**：仓库 Python，`OPENBLAS_NUM_THREADS=1`，禁用 pytest 缓存，`--basetemp` 指向工作区。允许本机回环后，
  共 1,006 项，983 通过 / 23 跳过 / 0 失败。早期 6 项录制测试失败由 `_initial_state` 遇到替身缺 `dq` / `age_s` 引起，
  已在交接前修复，本轮全套确认通过。
- **交接中约 65% 的 5 项失败**：从进度日志按收集顺序定位到 `test_local_ik.py`，分别是本地 IK TCP 模型绑定、缺模型时禁止回退、
  Hold 后旧 IK 结果失效、末端模型位置偏差拒绝和姿态偏差拒绝。逐项复跑均在 `socket.connect` 报 `WinError 10013`，
  发生于 hello 前。允许回环后 5 项均通过，本轮没有更改产品代码或放宽既有断言。
- **新增导出回归**：写盘标记 `complete=true` 的段若 `quality.status=incomplete`，仍保留原文件，导出记录 `quality_incomplete` 及原因，
  并在划分、生成样本与归一化之前排除。旧数据无 `quality` 时仍可导出。数据布局和实机相机要求已补到 [USAGE](USAGE.md#raw-schema-2)。
- **固定输入门**：`windows_dev.py test lab-scene` 全部 7,177 条输入完整性检查及全部离线参考门通过。
  未移时参考误差 P95 为 48.909 mm（记录与回放一致）。按用户后续指示跳过 Gazebo，仅做过 Docker 镜像自检，未启动仿真。
  提交频率、IK 往返时间和 B 的 `control_gap` 前后对照未做。网页独立质量面板未做，已有录制状态消息和文件摘要可查看质量。
- **现场只读连接**：新 A 已连实验室 B，后端确认为 `intera`，连续读取 3 秒，反馈年龄最大 40.5 ms，未发送运动命令。
  现场 B 未声明 `feedback_stream`，A 回退到 latest；实机 schema 2 原生流、双相机录制和长时负载仍待验证。
  已重新导出 [候选 B 包](../../windows-local/artifacts/raw-schema2-20261107/lab-export/B-Lab-Ubuntu/)，
  82 个清单文件通过大小 / SHA-256 校验，Python 3.8 语法检查通过，固定现场配置逐字节一致。未替换或重启现场 B，`release_accepted=false`。
- **证据**：[验证报告](../../windows-local/artifacts/raw-schema2-20261107/verification.json) 含失败用例、测试与输入文件逐项 SHA-256、
  部署清单和现场读取结果。源码树 SHA-256 `a67918701a3cf01fca5e6dd4920de0ef92e40dc92b2791ef3ef13c1fdbda1e39`，
  `config/operator.json` SHA-256 `436846ecc565c4e458afb7e5a11e2859ebf47a6e2599cc02918da786163755c4`，
  离线参考门生效参数 SHA-256 `1089b2b710790266dfeedb4572f7cfb8576864181dd1c4d62a651bf4048e38d9`。
- **下一步**：现场授权更换并启动新版 B 后，A 重连，勾选本机当前关闭的 **Record cameras**，确认全部相机在线，再做一次实机短录制。
  核对 `quality`、两路原生反馈频率及两路 HDF5 RGB / MP4 的帧数。

### B 自动测量机器人时钟，免手动对时（2026-11-07）

**仅更新 B（intera_backend / 新 robot_clock / feedback_log 哈希表），A 兼容；协议、门限、期限不变；无新依赖；需复制 deploy 并重启 B，A 重连。**
用户反复遇到机器人重启后 `feedback_latest_expired`，要求启动 B / A 连接时自动检测修复、不再手动跑命令。根因：机器人时钟重启后
与 B 差几十 ms，timesyncd 对 <0.4 s 偏差只缓慢 slew，B 用自身时钟校验源时间而持续拒收。改为 B 启动时及每 2 s 用无特权 SNTP
向 ROS master 主机（sawyer.local，11-02 已确认提供 NTP）实测“机器人 − B”偏差，以实测机器人时钟校验 joint / pose / collision / cuff；
测量误差（往返/2）只让数据更旧。往返 >50 ms、stratum 0、超 10 s 无新测量时退回 B 时钟（原行为）。50 ms 残差 / 100 / 500 ms 门限不变；
这是显式测量，不是从反馈数据学习偏差。启动打印测量结果或 WARNING，日志 `feedback_diagnostics.robot_clock` 记录。
新增 `tests/test_robot_clock.py`（回环 UDP 真实 SNTP 往返、杂包 / KoD、过期回退、85 ms 现场偏差下接受新鲜 / 拒绝过旧与超前、cuff）。
完整 988 项 965 通过 / 23 跳过 / 0 失败；B 文件按 Python 3.8 语法解析通过。deploy 仅装入本改动 4 个文件并按磁盘重算清单
（导出包中他人未提交的摄像头 / Web / pyproject 与 CRLF 版 Reset YAML 未装入），`release_accepted=false`。
未连接机器人：机器人 NTP 实际应答、实测偏差与长期稳定性需现场启动 B 时看打印确认；若出现 WARNING 仍按 DEPLOYMENT 手动后备。
证据：`../../windows-local/artifacts/robot-clock-auto-20261107/`。

### 摄像头接入录制复核（2026-11-07）

**本轮仅验证和记录，A/B 源码、运行配置、部署包和驱动无改动。** 用户确认停止 Start-A 后，用隔离 Web A 显式开启
`Record cameras`。先用本机假 B 和合成手柄录制，再按用户要求直连实验室 Intera B（`10.101.119.182`）。
控制与 IK 握手成功，B 报告原生 Cartesian 可用。探针 12/12 次反馈新鲜，估计年龄 12.8–26.1 ms。
实体连接只读反馈，录制用的客户端只允许握手和时钟 ping，拦截运动与夹爪指令。未启动 Gazebo。

- **实体 B，静止录制 6 s**：`wrist` 120 帧（20.02 Hz），`realsense` 119 帧（19.98 Hz，按首末帧计算）。
  HDF5 两路均为 640×480 的 uint8 RGB，图像、序号、读取开始时间和读取完成时间逐帧对应。序号连续，时间严格递增，
  均在片段时间窗内。时间戳使用 A 端单调时钟，记录主机读帧时间。MP4 各自可完整解码，帧数与 HDF5 一致。
  两路相机的质量均为 `ok`，录制前后曝光正常；本段腕部 FIFO / 短帧 / IPC 计数无新增。原有低比例丢帧根因待查。
  `states` 有 357 条实体反馈，七轴在文件精度下保持静止，`commands` 为空。整段因原生反馈流无数据被标为 `degraded`：
  当前实体 B 的握手未声明 `feedback_stream`，本次只读客户端也关闭了原生反馈流请求。
- **隔离对照**：假 B 加真实相机 6 s 得到 117 / 120 帧，HDF5 与 MP4 通过，腕部按目标帧数短 3 帧。
  合成相机 4 s 得到每路 64 帧，序号无缺口，RGB 样本与合成输入一致。初次端到端验证遇到沙箱回环限制
  （WinError 10013），放行本机回环后首次仍因合成源约 16 Hz 未达到脚本原定的帧数门槛。
  复核以合成帧连续性和写入一致性为准，保留降级标记。全套 pytest 在受限沙箱中出现失败和错误，本轮未验收全套。
  放行本机回环后，摄像头、Web 和录制相关 pytest 37 项通过。
- 证据、配置 / 源码 / 脚本 / HDF5 SHA-256 及失败日志：`../../windows-local/artifacts/camera-handoff-20261107-170634-252/`。
  实体录制核验见 `intera-171436/verification.json`。隔离实例已退出并释放相机。下一步由用户重启 Start-A，
  选实体 Intera 并在连接前勾选 `Record cameras`。摄像头补丁待用户确认后单独提交。

### 两路相机改 20 Hz，腕部相机掉帧与画质优化（2026-11-07）

**仅更新 A（`sn9c20x.py`、`cameras.py`、`app.js`、`operator.json`），B 无需更新；无新依赖，需重启 Start-A，无需重校准。** 用户反馈腕部相机常掉帧，要求先改 20 Hz，并参考 `multimodality_data` 分支的处理优化 RGB 准确度、避免过曝 / 过暗。
- 掉帧定位：运行中的 Start-A 采样 20 s，腕部 22–30 FPS 波动、RealSense 稳定 30。旧逻辑见任何错包标记就整帧丢弃，但 “主机未及时轮询（ISO_NOT_ACCESSED）” 时同步设备会把数据留在 FIFO 下次再发，并未丢数据 → 改为忽略该状态、以整帧必须恰好 307200 字节为完整性判据；排队读取 16×32 → 32×64 微帧（约 256 ms）；新增掉帧原因计数（`dropped_short/long`、`bridge_fifo_full`、`packets_not_polled` 等），经 `/api/state` 的 `cameras[].diagnostics` 可查。隔离实测：桥接 FIFO 溢出几乎全部在启动后 0.45 s 内（约前 8 帧），之后 12 s 内仅 1 次（约 0.4%）；**加载负载下的效果尚待重启 Start-A 后复测**。
- 20 Hz：腕部在传感器层插入垂直虚拟行（ADVFL/ADVFH，Linux 曝光 > 547 时用的同一寄存器；分支记录曝光 960 时帧率降到 17 是线索），帧间隔均匀 50 ms、USB 数据量降三分之一。实测 274 行 → 19.50 Hz，据此标定每行约 65.5 µs，20 Hz 用约 254 行，复测 19.3–20.1 Hz（差值来自丢帧计入）。RealSense 硬件只有 6 / 15 / 30，设备仍跑 30（`source_fps`），软件按均匀时间网格保留 2/3（间隔 33 / 67 ms 交替，平均 20）。
- 亮度：虚拟行也延长了这个传感器的实际积分，20 Hz 下沿用分支参数（曝光 300 / 增益 10）亮度均值 107 → 146。扫描后选 **曝光 300 不变、增益 10 → 4**：均值 107.9、中位 85、暗像素 3.1%、过亮 / 截断 0%，与原生 30 Hz 分支参数（106.2 / 83 / 5.3% / 0%）一致；曝光 60 + 增益 10 亮度相同但噪声更大，淘汰。红 / 蓝平衡沿用分支 33 / 28（场景已不同，未重新调白平衡）。分支的指标脚本未入库，过暗 / 过亮阈值为本项目自定（灰度 ≤ 20 / ≥ 245、单通道 ≥ 254）。
- RGB 准确度：去马赛克改 OpenCV 边缘感知（`COLOR_BayerBGGR2BGR_EA`），0.3 ms/帧；同一原始帧对比斜边阶梯伪色略少，边缘色度指标 9.71 → 9.65，**改善很小**。
- 过曝 / 过暗提示：`CameraHub` 每秒对 1/4 降采样图算亮度分位、暗 / 亮 / 截断比例和 RGB 均值，页面在帧率旁显示 Overexposed / Too dark，悬停看数值。
- 关闭卡死：停止重提后 32 个排队传输里约 6–7 个 WinUSB 永不返回，python-libusb1 `close()` 永久等待（Ctrl+C 退出 Start-A 也会卡）。改为排空 1 s 后把这些传输从库的在途登记中移除、保留引用不释放，再关句柄；连续 3 次开关各 1.0 s，进程正常退出。
- 测试：新增虚拟行、均匀抽帧、曝光判定、整帧大小与未轮询包用例；完整 pytest 通过。哈希：`sn9c20x.py` `75bf4728bd26`、`cameras.py` `284729e222eb`、`config/operator.json` `9c780a725811`。
- 真实负载复测（用户重启 Start-A 后只读采样 30 s）：腕部 19.8 FPS 均值（最低 18.0，修改前 30 Hz 档为 22–30 波动），30 s 内丢 6 帧 / 约 655 帧（约 0.9%：桥接 FIFO 满 3 次、短帧 6 次，无传输错包）；RealSense 20.0 FPS（19.7–20.4）。曝光：腕部中位 88、暗 2.0%、过亮 / 截断 0%、RGB 均值 112/109/107；RealSense 中位 143、暗 3.3%、0% 截断，均判定正常。
- 剩余：约 1% 的桥接 FIFO 溢出短帧仍在，未影响 20 Hz 均值。
- 独立进程收包（用户要求，同日）：`SN9C20xProcessCapture` 用 spawn 子进程独占 USB 与切帧，经队列把原始帧交主进程转色；父进程退出时子进程 0.5 s 内自退，关闭超 5 s 强制结束。`-I` 启动方式实测子进程 0.3 s 起、错误可回传。Start-A 真实负载 30 s：19.8 FPS（最低 18.1），丢 5/647（0.8%，FIFO 满 2、短帧 4、长帧 1），`ipc_dropped` 0——与改前 0.9% **无可测差异**，说明剩余丢帧不是主进程 GIL 抢占所致，而在 USB / 设备层（隔离单跑也有约 0.4%）。保留子进程（对录制等后续主进程负载有隔离作用），根因未查明；可尝试的下一步是换 alt 7 带宽档或分析 WinUSB 调度，本轮未做。

### 腕部 Sandberg 相机在 Windows A 上用 WinUSB + 用户态驱动出图（2026-11-07）

**仅更新 A（新增 `sn9c20x.py`、`cameras.py` 的 `sn9c20x` 后端、`operator.json` 的 `wrist` 条目、Windows 依赖 `libusb1==3.4.0`），B 无需更新；需重新 Setup 或手动装 libusb1、重启 Start-A，无需重校准。** 用户坚持在本机用原腕部相机（0c45:627b，SN9C201 + OV7660），Windows 无可用签名驱动（见下一条）。
- 做法：把 Linux `gspca/sn9c20x.c`（GPL-2.0+，文件内注明出处）的桥接初始化表、OV7660 寄存器表、I2C 协议、色彩矩阵 / 伽马 / 红蓝平衡 / 增益 / 曝光写法和 64 字节帧头切帧移植为 Python 用户态读取器；只实现 640×480 原始 BGGR + 手动曝光，按 `cv2.COLOR_BayerBGGR2BGR` 转色。设备驱动为**系统自带、微软签名的 WinUSB**（设备管理器手动指定 “WinUsb Device”），不关任何安全设置。`controls` 直接沿用 `sandberg_camera/camera_profile.json`（曝光 300、增益 10、红 / 蓝 33 / 28 等，V4L2 同名同单位）。
- 绕开的 libusb 1.0.29 问题：任一包出错时 WinUSB 整个等时读返回 ERROR_GEN_FAILURE，libusb 不转换包描述 → 按 USBD `{Offset, Length, Status}` 取出好包，丢弃该帧（同 Linux 丢帧语义），每次重新 `setIsochronous` 后再提交；取消等时传输会被 libusb 内部回退逻辑重提而永不完成 → 关闭时不 cancel，停止重提让 4 ms 传输自然排空，关闭整体限时 3 s。
- 结果：直连笔记本 USB 口、alt 8：首帧 0.77 s，两次各 6 s 连续 168 / 174 帧（28.7 / 29.7 FPS），0–1 个错包，开关可重复；画面颜色正确（红色机械臂、黄黑警示带、绿色夹指）。8790 隔离 Web 实例页面显示腕部 30.0 FPS 采集、约 25 FPS 刷新。新增 `tests/test_sn9c20x.py`（切帧、丢帧、公式、失败传输取包、后端选择）。哈希：`sn9c20x.py` `a90d507e69aa`、`cameras.py` `280d4ceb5699`、`config/operator.json` `683ea5de9cf4`、profile `98aaa60431fb`。
- 失败与淘汰：UsbDk 1.0.22（已安装、服务运行）能枚举但对 Code 28 设备的接管卡死、强杀后枚举失败需重插，**淘汰**（可卸载）；经 WCH 1a86:8095 扩展坞时 alt 6–8 约 2 s 后大量 XACT_ERROR 并掉线重枚举、alt 1–4 不掉线但带宽不足（每帧仅 7–18 KB，帧头 FIFO 满标志），**必须直连电脑**。WinUSB 绑定按 USB 口记，换口需重新指定。录制路径只做了离线 / 隔离验证，未做带摄像头的整批录制与实机遥操。
- 下一步：用户停止旧 Start-A 后重启，确认两路同时在线；再做一次勾选 Record cameras 的短录制核对 HDF5 中两路图像与时间戳。

### 批量录制新增“Reset arm between episodes”勾选项（2026-11-07）

**仅更新 A（operator / web_session / Web 界面），B 无需更新；协议、复位目标、限速、期限不变。需重启 Start-A 生效，无需重校准。**
用户要求批量录制时，在相邻两条之间回到 Reset arm 点位。新增默认不勾选的 `reset_between`（布尔，记入 batch / metadata / batch.json）。
一条保存或丢弃处理完且整批未结束时，进入 `resetting` 阶段，复用原 `_start_reset`（同 neutral 目标、0.25 倍 cubic、到位判据，
Gazebo 含场景恢复）；只有确认到位后才从此刻起计间隔，再开下一条。Stop reset、Save & End、超时、故障或断连都会停止并结束整批，
不自动开始下一条；最后一条后不复位；B 无可用 Reset 姿态时开始前拒绝，不建目录。手动 Reset 在批次中仍被拒绝。
新增 8 项测试（TCP 假后端两条批次到位、未勾选不动臂、停止 / 结束 / 间隔 / 参数 / 不可用）；完整 971 项 948 通过 / 23 跳过 / 0 失败（含 7,177 输入）。
本地 Web 假后端截图确认勾选项显示。未在 Gazebo 或实机运行；实机复位会运动，首次使用需操作者在授权下确认路径与急停。
同文件中他人未提交的摄像头改动原样保留。证据：`../../windows-local/artifacts/batch-reset-between-20261107/`。

### Smooth teleop 默认改为 DROID-like 150 ms（2026-11-07）

**仅更新 A（配置模板），B 无需更新；无代码 / 依赖变化，无需重校准。** 用户要求把界面上已选的 Smooth teleop 开启 +
DROID-like 150 ms 设为默认。仅将 `config/operator.json` 的 `motion.time_constant_ms` 由 20.0 改为 150.0（`enabled` 原本即 true）；
同文件中他人未提交的 `cameras` 改动原样保留。PoseSmoother 上限、Web 档位、校验和 `web_session` 缺省回退 40 ms 均不改。
模板只在首次启动复制为本机配置；本机 `windows-local/config/operator.json` 已保存为 enabled / 150，未改动。
USAGE、CURRENT_TELEOP_METHOD 同步，新增 `tests/test_smoothing_default.py`。相关 97 项（含 lab scene 7,177 输入、Web、启动器、部署）
75 通过 / 22 Windows 跳过 Ubuntu 脚本 / 0 失败。150 ms 约 450 ms 达 95%，滞后明显，仍需实测手感；不代表实机验收。
证据：`../../windows-local/artifacts/smoothing-default-150-20261107/`。下一步：重启 A 即用新默认（已有本机配置不受影响）。

### Web A 显示腕部 + RealSense 两路实时画面，移除 3D 姿态面板（2026-11-07）

**仅更新 A（代码 + `operator.json` 的 `cameras`），B 无需更新；无依赖新增，需重启 Start-A 生效，无需重校准。** 用户要求把 `multimodality_data` 分支（cb95b07 / c85b625）配好的摄像头接入当前项目，在 Live status 下方并排显示两路实时画面，并删除 Actual robot posture。
- `cameras.py`：Windows 下 `device` 可写 DirectShow 设备名，按名字解析到 OpenCV `CAP_DSHOW` 索引（找不到即报 `camera_not_found`，**不回退到其他设备**，避免误用笔记本自带摄像头）。新增 `CameraHub`：Web 服务启动即常驻采集（断开后每 2 s 重连），`/api/camera?name=` 返回带鉴权的 JPEG；勾选 Record cameras 时录制复用同一采集（`SharedCamera`，不重复打开设备）。
- 配置：`wrist` = `USB20 Camera`（Sandberg，0c45:627b），`realsense` = `Intel(R) RealSense(TM) Depth Camera 435 with RGB Module RGB`（UVC RGB 640×480@30，不依赖 pyrealsense2，无深度）。本机 `windows-local/config/operator.json` 同步。旧的 `front`（索引 0 = 笔记本内置摄像头）已删除。
- 页面：右栏 Live status 下新增 Cameras 卡片两列并排；删除 3D 姿态面板及其轮询（服务端 posture 接口保留未用）。CSP `img-src` 增加 `blob:`。
- 分支的 Linux 工具代码与配置（不含 4000+ 调试图片 / robot_dataset）放到工作区根 `sandberg_camera/`、`realsense_camera/` 作参考；其 v4l2 曝光 / 增益 / 裁剪参数在 Windows 上不生效。

结果：本机 DirectShow 枚举为 0 Integrated / 1 RealSense Depth / 2 RealSense RGB / 3–6 Quest 虚拟设备，截帧确认索引 2 为实验室场景。8790 端口隔离实例浏览器验证：RealSense 30.0 FPS、页面刷新约 27 FPS、无控制台错误；完整 pytest 通过（test_web 新增摄像头接口 / 鉴权 / 404 断言，配置哈希 `c2c26ebb6f52`、cameras.py `958a42287434`）。
失败 / 未完成：**腕部相机在 Windows 上 Code 28 “未安装驱动”**（SN9C20x 为厂商类 0xFF、非 UVC，分支方案依赖 Linux `gspca_sn9c20x`），页面显示 No signal 与原因；未安装驱动（属系统改动，需用户决定）。录制在腕部无画面时勾选 Record cameras 会按原规则失败。未接入真机或执行运动。
Windows 驱动排查（同日，用户授权下载检查、未安装）：Microsoft Update Catalog 无 627B 条目；仅第三方 2006–2009 年 Sonix `snp2std` 包。下载 webcamtests.com `Sonix Chicony 5.7.2.004` `Windows.zip`（5,358,659 B，MD5 `0ba07a94…` 与页面所列 `382961be…` 不符，SHA256 `a85d4211…`）：INF 含 NTamd64 的 `USB\VID_0c45&PID_627b`（SN9C201 + OV7660），但引用的 `snp2std.cat` 不在包内，全部 .sys/.dll/.exe 均 NotSigned。64 位 Windows 11 强制驱动签名且本机内存完整性（HVCI）开启，该包无法正常加载；只能靠测试签名模式 / 关闭签名强制与 HVCI，属削弱系统安全，不采用。
下一步（已被上面“WinUSB + 用户态驱动”条目取代）：腕部相机改用免驱 UVC 相机，或在 Ubuntu A 上使用原 SN9C20x 相机。

### 直接共享实验室 B 运行配置（2026-11-07）

**仅更新 B 配置与使用文档（A 兼容）。** 按用户最终要求，直接将源码中的
`config/real-b.env` 纳入 Git，逐项添加中文说明；复用现场主机名、B IP 和夹爪适配器标识，
工作空间使用 `$HOME/sawyer_ws`。撤掉单独的实验室模板，通用空白 example 保留。
仅对此源文件解除忽略，其他本机配置继续忽略；AGENTS、SETUP 同步说明使用与导出方式。
不改启动或运动逻辑、Reset 姿态或依赖，旧部署保留。配置文件更新不会改变已运行的实例。
Bash 语法、配置参数一致性及 Git 空白检查通过；当前执行环境仍缺少可见 ROS 安装，
未完成启动依赖检查，未连接机器人。下一步：在实验室终端 `--check` 后按现场流程启动。

### 本机实机 B 现场配置迁移（2026-11-07）

**仅更新 B 本机配置（A 兼容）。** 将旧部署目录
`/home/k23114984/Lin/B-Lab-Ubuntu-20261102/config/real-b.env` 原样复制到当前代码根的
`config/real-b.env`，保留旧副本。复用现场机器人地址、B 网卡地址、ROS / Intera 工作空间路径及
外接夹爪串口；不改依赖、启动代码或新实测 Reset YAML。现场配置继续由 Git 忽略。
Bash 语法检查通过；`Start-B-Real.sh --check` 在当前执行环境报
`Missing ROS setup: /opt/ros/noetic/setup.bash`，尚未完成依赖检查或硬件验证，未启动 B 或访问机器人。
下一步：在实际实验室 ROS 主机终端运行 `--check`；通过后由操作者停止旧 B 并启动新 B，A 重连 / 校准。

### 实体 Sawyer Reset arm 改为新实测姿态（2026-11-07）

**仅更新 B 配置（A 兼容，A 代码不变）。** 用户提供实测七轴（rad）：
`0.932630859375, -0.975340820313, -1.853528320312, 1.354167968750, 0.580955078125, 1.943027343750, 4.017021484375`，
取代 10-26 姿态。仅改 `config/lab-neutral-pose.yaml`（保留原文精度，不取模 / 归一化；均在实机 URDF 限位内，
J6 在 ±4.7124 内）。机制不变：`Start-B-Real.sh` 加载为 `/named_poses/right/poses/neutral`，B 启动读取，经 `hello.neutral_pose`
给 A；A 无持久化或硬编码实机值。有意不改：FakeBackend 合成值、Gazebo J6 兼容列表及镜像 named pose
（新 J6 超 Gazebo ±3.14，Gazebo 不加载该 YAML）、测试任意角度、`deploy-20261026` 历史副本、env / 夹爪 / 网络。
新增 `tests/test_lab_neutral_pose.py` 校验文本精度、顺序、有限值和实机限位；DEPLOYMENT 增补生效步骤。
按导出流程生成新包后只替换 `deploy/B-Lab-Ubuntu` 的 YAML、DEPLOYMENT.md 与清单（YAML 366 B，
SHA-256 `b5e22278…a97e8e`），保留现场 `real-b.env`，`release_accepted=false`、`hardware_validation=pending`。
相关 102 项（80 通过 / 22 Windows 跳过 Ubuntu 脚本）与完整 960 项（937 通过 / 23 跳过 / 0 失败）通过；
首轮沙箱拒绝默认 pytest 临时目录的失败日志保留。未连接 ROS 或机器人。
证据：`../../windows-local/artifacts/lab-neutral-pose-20261107/verification.json`。
下一步：实验室替换 B 的 YAML（或整个 deploy，保留 env）→ `--check` → 重启 B → A 重连 / 校准，
`rosparam get` 确认后由操作者在授权下首次 Reset，验证到位与路径安全。

### 增补现场快速恢复命令（2026-11-02）

按用户要求，在 DEPLOYMENT 顶部新增“再次出现反馈时间错误：现场快速恢复命令”，集中列出 A 断开 / 操作者停止 B、重启 timesyncd、等待后检查同步、从实际最新版目录启动 B、失败时收集 NTP 和最新反馈日志。明确已有 NTP 配置不必每次重写、示例目录不能替代实际新版路径、启动会初始化夹爪，且恢复需由实际反馈验证。仅文档更新，无代码 / 配置 / deploy 变动，不操作运行实例；检查 Markdown 围栏与空白差异，无需运行代码测试。下一次按该入口执行，未恢复则转完整手册诊断。

### 重启后时间同步排障手册与可复用 prompt（2026-11-02）

用户要求将本次 NTP / 反馈时间问题整理成 Markdown 并提供以后可直接复用的 prompt。已在 DEPLOYMENT 新增“重启机械臂后反馈过期：排查与恢复手册”，集中说明错误链路、50 ms 代码容差与 Ubuntu NTP 配置区别、系统配置不随 deploy 迁移、现场取证命令、ROS 环境与三路源年龄探针、备份 / 恢复 NTP、反馈持续健康判据及后续排障 prompt。明确“启动等待多路反馈健康”仍未实现，用户最终 NTP -111 us 只代表当时同步改善，不宣称完整 VR / 实体验收。

本轮仅文档，不改运行代码、配置或现有 deploy，不需重启；未启动 / 停止 A/B、ROS、串口或真机。Markdown 代码围栏、嵌入 Python 语法、链接及 git diff 空白检查通过；无须重跑运行代码测试。文档改前备份和校验信息在 `../../windows-local/artifacts/time-sync-runbook-20261102/verification.json`。下一次故障先按手册读取新证据，不预设一定是 NTP，也不自动扩大时效或时间超前容差。

### 松开 Trigger 开爪语义只读核对（2026-11-02）

用户澄清“松开 Trigger，希望夹爪打开”。已核对现有 A→B 路径：Grip 保持握持且遥操有效时，Trigger <0.3 映射开爪 -1，实体目标 OPEN=3766 ticks；>=0.3 映射闭爪 1024 ticks。松 Grip 则先暂停 / hold，不自动开爪；暂停 / 反馈无效时不会单独执行 Trigger 开爪。当前实现已符合所述需求，本轮没有代码、配置或 deploy 修改，无需因本次检查重启。

相关既有测试 43 passed，覆盖 Trigger 阈值、六种提交模式、松 Grip 保持 / 去重 / 超时 / 断线；首轮沙箱仅本机回环用例 WinError10013 失败，提升权限后仅替身回环复验通过。证据、源码 / 测试 / 参数哈希及原失败日志在 `../../windows-local/artifacts/gripper-release-check-20261102/verification.json`。未连接串口或机器人，软件通过不代表现场动作验收；下一步操作者在已授权现场流程确认 Trigger 开合行为。

### Demo 反馈源时间允许最多 50 ms 超前（2026-11-02）

用户明确表示 demo 可接受微小时间误差；本条取代初版仅 collision 的 1 ms 容差。现场进一步提供三路 ROS 超前约 83–85 ms，机器人 NTP 可用；配置 Ubuntu 跟随机器人后 NTP 报 +33.540 / -31.471 ms（不当作 ROS 残差实测）。按本次 demo 意图，B 对 joint / pose / collision 和 cuff 源时间统一允许最多 50 ms 超前，覆盖几十毫秒偏差但不会接受此前 85 / 116 ms 超前。此为有界校验放宽，不做偏差自动学习或固定数值补偿；超过上限继续拒绝，需以实际 ROS 消息年龄验证现场是否满足。

**仅更新 B，A 兼容；需现场重启 B、A 重连 / 校准。** 不新增配置 / 依赖；保留 joint / pose 100 ms、collision 500 ms 过期检查，原始时间戳排序、倒退拒绝、重复不刷新、collision / cuff / robot interlock 和全部运动引用 / 命令 / STOP / 关节保护。接受的负年龄按 0 换算本机 captured 时刻，不产生未来单调时刻、不延长已有运动引用。本条放宽了跨机器时间戳校验；源年龄仍受剩余时钟偏差影响，不能宣称真实源年龄仍具有无误差的 100 ms 保证。保持机器人 NTP 同步有助于维持容差，但 NTP Offset 本身不代表 ROS 反馈年龄。

专项 **14 passed**，完整软件 **935 passed / 23 skipped**，覆盖全部 7,177 输入及旧横移 / 抬手、停手回归；现场微秒、31.471 ms、50 ms 边界、超限 / 85 / 116 ms 拒绝、过期 / 时间倒退 / 重复 / 真实碰撞均覆盖，无本轮失败（保留既有 JUnit 属性 warning）。通过官方脚本新导出并更新 `../../deploy/B-Lab-Ubuntu/`，改动仅 intera_backend.py、DEPLOYMENT.md、manifest；完整旧包备份逐文件校验通过，79 清单文件 / 全部80文件 SHA256 / 大小、Python3.8语法、源码一致性与原固定 env 保留检查通过，FTDI 夹爪端口不变。证据、输入 / 源码 / 参数 / 新旧部署哈希及单项patch在 `../../windows-local/artifacts/feedback-skew-50ms-20261102/verification.json`。

本轮未启动 / 停止 A/B、ROS、串口或真机，未运行 Gazebo / 完整 VR，`release_accepted=false`。下一步操作者复制当前 deploy，按 DEPLOYMENT 重启并验证实际反馈可用与 demo 行为；旧 zip 不包含本次放宽，不应复用。实体动作不由本轮自动执行。

### 固定配置启用现场 FTDI 夹爪串口（2026-11-02）

用户反馈夹爪不动，核对实际 deploy 配置与初始化条件后确认当前 `SAWYER_GRIPPER_PORT` 空值禁用驱动。用户随后提供 Ubuntu 只读枚举：`usb-FTDI_USB__-__Serial_Converter_FTAO9VMQ-if00-port0 -> ../../ttyUSB0`。已将固定副本 `../../windows-local/config/real-b.env` 的夹爪端口设为 `/dev/serial/by-id/usb-FTDI_USB__-__Serial_Converter_FTAO9VMQ-if00-port0`，其他字段字节内容不变，后续导出自动沿用。

**仅 B 配置 / 部署文档更新；A/B 运行代码与依赖声明不变。** 已通过官方脚本新导出，再更新 `../../deploy/B-Lab-Ubuntu/`，完整旧包备份先经逐文件 SHA256 核对。实际仅 env、DEPLOYMENT.md 和 manifest 三个文件改变；79 清单文件 SHA256 / 大小、全部 80 文件集合与固定副本一致性通过。证据、现场枚举、配置 / 全部部署新旧哈希在 `../../windows-local/artifacts/gripper-port-config-20261102/verification.json`，无本轮失败；纯配置变更未重跑运行代码测试。

本轮未远程更改实验室文件、打开串口、启动 / 停止 B 或执行实体运动，`release_accepted=false`。设备路径来自现场枚举，不代表串口通信或夹爪身份 / 动作验证。下一步由操作者更新现场 env，另行检查 SDK / 串口权限 / 无独立程序占用并重启 B，A 重连；启动会初始化夹爪并开启扭矩。启动操作详见 DEPLOYMENT，VR / 实体联合验收仍待现场执行。

### 固定实验室 B 配置与后续导出复用（2026-11-02）

用户提供实验室现用 real-b.env 并要求以后一直使用。已逐项按所贴内容保存到源码同级 `../../windows-local/config/real-b.env`：机器人 sawyer.local、ROS_IP / BIND_IP 10.101.119.182、Noetic、工作空间 /home/k23114984/sawyer_ws，Python 自动选择，夹爪端口空值保持禁用。该文件不入源码库，不执行其中内容。现有 deploy 配置与用户内容已逐字节一致，验证后保留。

**仅开发导出工具 / 部署配置规则更新；A/B 运行代码不变。** exporter 默认优先读取固定副本，缺少时仍回退通用示例；`--config` 可显式指定其他配置或示例，错误路径失败退出，不发布半成品。示例保持空地址；仍只导出到新目录。操作规则写入 DEPLOYMENT，源码替换不会覆盖固定副本。

导出 / 迁移专项 **16 passed / 2 skipped**，验证默认复用、显式覆盖、缺失文件失败、通用模板保持及 manifest 配置哈希。通过官方脚本新导出，再同步现有 `../../deploy/B-Lab-Ubuntu/`；本轮实际仅改变 DEPLOYMENT.md 和 manifest。旧包完整备份与逐文件校验通过；79 个清单文件 / 全部 80 文件 SHA256、大小、Python 3.8 语法和固定配置字节一致性通过。证据、源码 / 配置及新旧部署哈希在 `../../windows-local/artifacts/lab-config-persist-20261102/verification.json`，无本轮失败。

没有连接实验室电脑、启动 / 停止 A/B、ROS、串口或真机；现场配置来源为用户粘贴，非远程读取。沿用上一轮碰撞修复软件验证，不代表现场验收，`release_accepted=false`。下一步：操作者使用当前 deploy；现场网络或配置以后若变化，先同步固定副本再导出。

### 将碰撞时间修复更新到现有 deploy（2026-11-02）

用户明确要求直接更新 deploy。已将上一条离线验证通过、官方脚本导出的候选同步到 `../../deploy/B-Lab-Ubuntu/`；先完整备份旧包到 `../../windows-local/artifacts/collision-clock-skew-20261102/previous-deploy/` 并逐文件哈希验证，保留现有 `config/real-b.env` 字节不变。实际变动为 intera_backend.py、DEPLOYMENT.md、manifest，以及随当前已测试源码带入此前用户已要求的 trajectory 默认跟随误差 0.5→1.0 rad；该默认值不是本轮新增决定，A arm 配置仍决定运行值。

重新生成配置对应的 manifest 后，79 个清单文件 SHA256 / 大小、全部 80 文件集合、候选源码一致性与配置保留检查通过；deploy 下仅保留 B-Lab-Ubuntu。证据含新旧全部文件哈希：同目录 `deployment-update-verification.json`。没有启动 / 停止 A/B、ROS、串口或真机；沿用上条软件 931 passed / 23 skipped 结果，未重复无关测试。`release_accepted=false`；本次只更新本机 deploy，实验室复制 / 重启和现场验收仍待操作者执行，启动操作见 DEPLOYMENT。

### collision_source_time_invalid：碰撞状态允许最多 1 ms 超前（2026-11-02）

**仅更新 B（A 兼容）；需现场重启 B、A 重连 / 校准；无新增配置、依赖或协议。** 用户现场只读探针 10 条 `collision=False`，相对 B ROS 时间超前 56–127 us；B 当前 `_accept` 对任何超前源时间均拒绝，因此导致遥操 / Reset 被反馈保护暂停。用户明确要求修改。现仅碰撞状态允许源时间最多超前 1 ms，超过仍报原故障；保留零 / 非有限时间、时间倒退拒绝、重复消息不刷新以及 500 ms 过期和真实碰撞保护。容差内负年龄按 0 计算本机 captured 时刻，原始源时间保留用于排序 / 诊断，不生成未来 captured 时刻、不延长有效期。joint / pose / cuff 和所有运动引用、命令期限、速度 / 关节保护不变。

新增边界 / 现场数值 / 超限 / 过期 / 重复 / 倒退 / 故障恢复 / 真实碰撞及其他反馈不放宽用例；旧代码复现 3 failed / 9 passed，修复专项 12 passed。全套软件 **931 passed / 23 skipped**，包含全部 7,177 输入和旧横移 / 抬手、停手场景。第一次沙箱全套多项失败并中断，原始日志保留；解除沙箱限制后仅运行离线替身 / 本机回环测试通过。Docker Linux engine 管道不存在，未启动引擎，无法进行 Gazebo / 完整 VR 验证；本轮没有启动 / 停止现有 A/B、ROS、串口或真机，`release_accepted=false`。

通过官方导出脚本新建候选包 `../../windows-local/artifacts/collision-clock-skew-20261102/export/B-Lab-Ubuntu/`，不覆盖既有 deploy 或现场配置。导出 79 个清单文件逐项 SHA256 / 大小、Python 3.8 语法、压缩包内容与源码一致性通过。现场输入、参数 / 源码 / 固定输入哈希、改前副本、单项 patch、测试失败 / 成功日志和候选 zip 见同目录 `verification.json`。启动操作仅写在 DEPLOYMENT。下一步：操作者复制候选并保留现场 real-b.env，另行重启 B、只读确认碰撞时间故障解除；隔离仿真 / 完整 VR 与实体运动验收仍待各自授权和执行。

### 跟随误差阈值改为 1.0 rad（2026-11-02）

**仅需更新/重开 A 与本机配置，现有 B 兼容。** 按用户明确要求，将共享轨迹默认值、`config/operator.json` 和 `../windows-local/config/operator.json` 的 `max_following_error_rad` 从 0.5 改为 1.0；适用于普通关节轨迹和 reset。B 既有参数校验允许上限 1.0，接收 A arm 配置后执行该值，因此既有实验室 B 部署包无需为此重导出或覆盖。原生 Cartesian 路径不因该配置新增跟随误差检查。reset 专用速度/加速度比例、0.35 rad 单步跳变保护和期限均不变。

本轮仅软件验证，没有启动/停止任何现有 A/B、ROS、Gazebo、Quest 或真机；运行实例不热加载。重开 A、重新连接/校准后生效。未做物理仿真/完整 VR 或真机验收，`release_accepted=false`。证据与源码/配置/固定输入 SHA256、修改前备份在 `../../windows-local/artifacts/following-limit-1rad-20261102/`。沙箱首次测试因 pytest 临时目录权限失败，指定工作区临时目录后又因硬链接权限失败；提升权限仅运行软件回归，全套软件 927 passed / 23 skipped，包含全部 7,177 输入及旧横移/抬手、停手用例；完整结果见 `pytest-full.log/xml`。下一步在已授权仿真环境确认实际握手阈值、reset 与停止行为，真机仍需另行明确授权。


### 更新 deploy 夹爪部署包（2026-11-02）

用户明确要求更新 deploy。已通过 `scripts/export_deployment.py` 先导出新目录，再将旧 `deploy/` 完整备份到 `../../windows-local/artifacts/dynamixel-vr-20261102/previous-deploy/`并逐文件核对备份；新包更新到既有 `../../deploy/B-Lab-Ubuntu/`，deploy 下仅保留 B-Lab-Ubuntu。没有启动或停止任何 A/B、ROS、串口或真机。
保留原 `config/real-b.env` 内容，缺少时追加空 `SAWYER_GRIPPER_PORT`，避免丢失原地址或自动启用实体夹爪；模板/README纠正“夹爪尚未接入”的旧说明，启动操作见 DEPLOYMENT。新包含驱动、SDK可选依赖、启动配置与夹爪部署说明。
79个manifest文件逐项SHA256与大小通过、源码对照通过、Python3.8语法及B启动脚本 `bash -n` 通过；更新后再次验证全部80个文件SHA256（含manifest本身），没有遗留多余文件。结果与新旧配置/manifest哈希见同证据目录 `deployment-verification.json`。首次整体改名因Windows提示目录被其他进程占用失败，改用完整备份后逐文件复制，不停止现有进程；最终通过。`release_accepted=false`。
下一步：将新B目录复制到实验室，在实际B解释器安装Dynamixel SDK并配置真实串口，按原流程重启B、A重连/校准；VR与实体夹爪联合验收仍待现场单独完成。

### VR Trigger 接入实体 Dynamixel 夹爪（2026-11-02）

**B 必须更新，现有 A 控制兼容；使用新增 ticks 录制字段需同时更新 A。** 用户提供 `D:/project/Sawyer/gripper-integration/` 并确认独立夹爪代码已真机实测通过；本条只报告本次集成的软件证据，不将独立实测当作 VR 联合验收。
入口指定的 `docs/ROG_CODEX_HANDOFF.md` 当前不存在，按现存 README / AGENTS / PLAN / ARCHITECTURE 核对实际源码后接入，没有重建 A/B 或修改官方源码快照。

- 新增独立串口 worker，沿用用户硬件的 ID=1 / 协议2.0 / 57600、模式4、PWM300、速度80、OPEN3766 / CLOSED1024 ticks；不移植阻塞演示。
- 复用现有 A 的 Trigger <0.3 打开 / >=0.3 闭合，以及 Grip 运动许可、会话代次和目标期限。Standard / Fast follow、Intera Cartesian 四档和 Direct pose 共享 B 驱动；无新协议、APK、机械臂速度或保护期限变动。
- 最新意图覆盖、重复目标去重、反向抢占、松 Grip / STOP / 断连保持，串口仅由 worker 操作；独立意图看门狗随 B command-timeout，反馈超过150 ms失效或串口故障反馈至 Intera snapshot，使 Bridge 暂停。通信已断时无法保证电机取消目标。
- 以当前位置替换旧电机目标后才开启扭矩；不开合演示。暂停与正常退出尝试新鲜反馈保持且保留扭矩，不自动释放物体。机械臂原生 STOP 先于夹爪 worker 收尾，避免串口等待延迟机械臂停止。
- SDK 按包收包等待封顶50 ms、串口写超时50 ms，使用单调时钟；不能据此宣称严格端到端停止上界。保留到位、可能接触与故障区别。新增真实 ticks 状态及 HDF5 ticks/有效性/反馈年龄，不伪造米单位。
- 串口默认禁用。新增可选依赖 `[gripper]`、B env `SAWYER_GRIPPER_PORT` 与 CLI `--gripper-port`，只在 Intera 显式配置后初始化；启动与退出语义、SDK安装及配置见 [DEPLOYMENT](DEPLOYMENT.md)。导出同时携带该文档；本轮未导出新部署包、未覆盖实验室部署。

验证：全套 **926 passed / 23 skipped（Windows 跳过 Ubuntu 启动器用例）**，包含最新实验室全部7,177输入、旧横移/抬手与停手回归；随后退出顺序补充与夹爪/Intera专项 **33 passed**（与全套有重叠，不相加）。导出/录制/驱动复验49 passed / 2 skipped；B shell语法检查通过。六条提交路径替身验证包含 Standard、Cartesian10/20/50/100、Direct pose，另测反向、去重、hold抢占、旧会话不重放、串口阻塞、失联/反馈过期、部分初始化清理、接触候选/无位移卡滞/开爪超时与录制ticks。

失败保留：首轮新增故障测试漏注入 cuff 状态，已修正；沙箱本机TCP被Windows10013阻止，提升权限后仅回环软件测试通过。第一次全套导出4项失败因测试源副本漏带新增DEPLOYMENT文档；补齐fixture并修正已有docs目录创建后通过。Git Bash语法检查先被沙箱MSYS命名对象权限阻止，提升权限仅作 `bash -n` 通过。原失败XML/日志不删除。
证据、输入/源码/参数SHA256、测试源码快照和结果在 `../../windows-local/artifacts/dynamixel-vr-20261102/manifest.json`；`release_accepted=false`。本轮无 ROS/Gazebo/真实Quest/串口/真机运行。
下一步：实验室B按DEPLOYMENT安装依赖、核对实际稳定串口并重启B；A重连/校准，现场另行验证VR与夹爪联合开闭、夹物、松手/STOP/断连、各实际运动选项与录制，不把本轮替身测试算作实体验收。

### 加回 Stop-B.cmd（2026-10-30）

**仅更新 Windows 入口（A、B 代码不变）。** 用户要求恢复独立停止入口：根目录新增 `Stop-B.cmd`（ASCII + CRLF），调用 `windows_sim_launcher.py --stop`，与 `Start-B.cmd --stop` 相同。
用户的 B 正在运行，按约定未用它停止该实例；以同一调用改 `--check` 验证路径解析（退出码 0、环境检查 PASS）。README / USAGE / AGENTS 与启动器提示同步；启动器相关测试通过。

### Motion option 整体变灰：native_stop_failed 锁存（2026-10-30）

**A、B 都需更新；重启 B（解除锁存）、重开 A。** 连接后下拉框整体禁用，因 B 锁存 `native_stop_failed`（上次 Direct pose 会话后，跨多次重连保留）。
B 事件随新会话清空，直接原因未取得；隔离 Gazebo 10 步断连/松手与 J6 贴限位复现均未锁存。
改动：未连接时原生选项可选（连接后按 B 能力判断、缺失时 Operator 拒绝握持）；锁存时网页提示需重启 B；B 停止失败写入控制台。
完整软件 930 项 0 失败。J6 中立位距下限 0.17 rad 未改。证据：[锁存记录](../../windows-local/artifacts/native-stop-latch-20261030/README.md)。
下一步：再次锁存时取 B 窗口 `B native STOP latched` 行定位根因。

### 输入平滑新增 DROID-like 150 ms 档（2026-10-29）

**仅更新 A（motion / web_session / Web 选项）；B 无需更新。重开 A，在 Smooth teleop 里选该档，下次连接生效。**
用户要实测 DROID 式重滤波（下条第 4 项）。PoseSmoother 上限 100→150 ms；原 Light 20 / Heavy 40 与默认不变，松手优先取消不变。
`test_motion` 增加 150 ms 参数与 151 ms 拒绝用例；按用户要求（仅改参数）**本轮未运行测试**。改前文件：
`../../windows-local/artifacts/smoothing-150ms-20261029/before/`。下一步：用户实测手感。

### 实机 Standard 不顺：与 DROID 源码对照（2026-10-29，只读分析）

**A、B 均未改。** DROID `33ae6a6` 只读克隆在 `../../droid-reference/droid`：15 Hz 步频、目标 = 实测关节 + 小增量、
等效约 142 ms 低通、1 kHz Polymetis 阻抗，频率不是它丝滑的原因。我方最可疑：Sawyer POSITION_MODE 速度比例默认 0.3、项目从未设置，
0.3×URDF 上限（j1 0.40 rad/s）低于 B 轨迹上限（j1 0.8）；实机日志 5 s 内 3 次跟随误差 >0.35 rad 佐证参考领先真机。
另：实机只发 q 无前馈（Gazebo 用 TRAJECTORY_MODE），A 滤波仅 20 ms。速度比例对流式指令的作用尚未实测。
证据：[对照记录](../../windows-local/artifacts/droid-smoothness-20261029/README.md)。下一步：用户授权后现场记录参考与实测关节速度，再决定一项改动。

### B 远端 IK 改用持久服务连接（2026-10-29）

**仅更新 B（intera_backend / gazebo_backend / 新增 ros_service，A 兼容）。重启 B，无需重校准。** 用户反馈实机 Standard 常只有约 50 Hz。
实机 B 状态日志 control_gap 约 10 ms，B 控制环正常；瓶颈是 A→B 目标：实机强制远端 IK、A 单任务串行，而 B 的 IKService 为
`persistent=False`，每次求解都重新查 master、建 TCP。改为一条持久连接，失败即丢弃并在下次重建，本次照常报失败、不原地重试；
`ik_busy`、80 ms 预算、`IK_IN_COLLISION` 拒绝、期限与协议不变。错开一帧的 IK 流水线评估后暂不做（种子断链、乱序、两端协议改动）。
隔离无界面 Gazebo（同输入，Standard，A 远端 IK）：A 侧 IK 往返中位 7.1→2.9/3.2 ms，P99 10.9→7.1/6.8 ms，0 错误；
容器内直连 IKService 中位 2.6–3.0→0.30–0.36 ms。仿真 IK 服务与网络不同，不能推出实机提升幅度。
新增 3 项单测；完整软件 917 项 0 失败 / 23 跳过（首轮被并行修改中的 `quest.py` 语法错误阻断，修好后重跑，失败记录保留）。
`probe_intera_ik.py` 增 `--connection per-call` 只读对照。证据：[本轮记录](../../windows-local/artifacts/ik-persistent-20261029/README.md)。
下一步：实验室授权后先只读探测两种连接，再重新导出替换 B，看 Web 上 A→B 频率。

### 时效期限放宽：偶尔滞后优于暂停（2026-10-29）

**A、B 都需更新。重开 A；Gazebo B 重启；实验室 B 需重新导出并现场替换（本轮未做）。** 用户决定：偶尔滞后 ≤200 ms 可接受，实机对齐 Gazebo 250 ms。
A 输入 150→200 ms（平滑输入间隔、网页新鲜显示、Quest 同步拒绝门槛随之），IK 任务 100→150 ms；B Direct pose 输入沿用 A 常量 200 ms；
实机 B 源引用/命令 120/200→250/250 ms。最新反馈 100 ms、控制 100 Hz、IK 20/80 ms、STOP 2 s、角度保护不变；命令期限 1 s 未采纳。
新 A 需配新 B（旧 B 会拒绝 >150 ms 的 Direct pose 截止）。另补 ROG 包漏列的 `sim_motion_server`/`local_ik`/`a_timing`。
完整软件 924 项 0 失败；隔离 Gazebo Direct pose 190/199 ms 预算、Cartesian/Direct pose 端到端 4/4 通过。
证据：[期限放宽记录](../../windows-local/artifacts/deadline-relax-20261029/README.md)。下一步：用户重开 A 实测；实验室前重新导出 B。

### Quest 时钟同步改用常驻 adb shell（2026-10-29）

**仅更新 A（quest.py）；B 无需更新。重开 A、重连校准。** 输入 150 ms 上限、不确定度公式与拒绝条件不变；真机 A 同样生效。
Gazebo Direct pose 一握持即 `direct_pose_failed`：Quest 时钟不确定度 64 ms，位姿到 B 仅剩约 36 ms 有效期（隔离 Gazebo 复现：36 ms 失败、60/150 ms 正常）。
不确定度来自每次同步新开 adb 进程（Windows RTT 84–102 ms）；常驻 shell 实测 RTT 约 8–9 ms，真实 Quest 不确定度 14.1–14.7 ms。
shell 失败时单次退回原做法。完整软件 924 项 0 失败。尚未在 Web + Quest 握持中实测。
证据：[诊断](../../windows-local/artifacts/direct-pose-deadline-20261029/README.md)、[修改](../../windows-local/artifacts/quest-sync-shell-20261029/README.md)。

### Gazebo 原生选项改回 A 本地 IK（2026-10-29）

**仅更新 A（client / operator / web_session）；B 无需更新。重开 A、重连校准。** 实机仍强制远端 IK。
用户实测 Gazebo + Cartesian 每次握持 `ik_input_expired`：上一轮让 Gazebo 选 Cartesian 时改用 B 远端 IK（40–67 ms），
加 Quest 时钟不确定度 59–67 ms 后超过 150 ms；IK 方式在 Connect 时确定，同一连接切 Standard 也同样超时；
且连接前选项置灰与“需先选再连”冲突。撤回该规则，Gazebo 允许本地 IK 进入 Cartesian / Direct pose。
隔离 Gazebo：本地 IK 中位 1.9 ms，Cartesian 100 Hz 与 Direct pose 50 Hz 均移动、0 错误、停止确认。
完整软件 923 项 1 失败（`test_motion` 平滑录制计时，单跑 5/5 通过）。未解释：本地 IK 下 100 Hz 档实际约 33 Hz 提交。
证据：[本轮记录](../../windows-local/artifacts/local-ik-cartesian-20261029/README.md)。
下一步：用户重开 A 实测；Quest 时钟同步往返 98–140 ms 待单独排查。

### Gazebo 初始化 j6 卡住与仿真 IK 超时修复（2026-10-29）

**仅更新 B（Gazebo 初始化 / 仿真 Motion Controller，A 兼容）；重启 Gazebo B，无需重校准。** 实机、Reset 目标、参数、期限不变。
根因：初始化没等 `spawn_model` 迟到 1 s 的 `-J` 姿态，Gazebo 把 j6 −2.967 换算成 +3.316，越过 +3.14 后被控制器归一化误差顶在限位。
修法：初始化前等 `/urdf_spawner` 退出。淘汰“暂停中分步设置”（10 次仍败 2 次）。修后连续 10/10 启动成功。
另修仿真 Motion Controller：IK 墙钟预算 8→50 ms（与 Gazebo 抢 CPU 时误判无解，1 s 重试约 5 ms 即解）；中止时记 errorId；
自检脚本结束回原位（原先逐轮漂移约 0.27 m 后在限位处失败）。5 实例 × 3 轮自检 15/15，完整软件 921 项 0 失败，
正式启动器 A→B 端到端通过。证据：[j6 与 IK 记录](../../windows-local/artifacts/j6-init-20261029/README.md)。
下一步：用 Web/Quest 在 Gazebo 实际试 Intera Cartesian 四档。

### Gazebo 仿真 Motion Controller（2026-10-29）

**A、B 都需更新。** 用户要求让 Intera Cartesian 在仿真中可用，取代原“Gazebo 不用自制同名服务”的决定。
官方源码快照没有 `/motion/motion_command` 服务端（实体 Intera 闭源），新增 `sim_motion_server.py`：
仅 `/use_sim_time` 下启动，置 `/motion/motion_command/simulated`；有界加速度直线/旋转跟踪 + LocalIK，100 Hz TRAJECTORY_MODE，
新 goal 沿用指令速度不从零起步；STOP→SUCCEEDED、cancel→PREEMPTED、错误→官方 errorId。
Gazebo B 仅认带标记的服务，hello 标 `simulated`；A 允许 Gazebo 选 Intera Cartesian，选中时用 B 的远端 IK。
两个 Gazebo 入口在场景初始化后、B 前启动它。实机代码路径、参数、期限、模型、依赖不变。
结果：单测 14 项；完整软件 920 项 0 失败 / 23 跳过（含 7,177 输入）；本机 Gazebo 中 SDK 6 项、A→B 端到端 5 项全通过。
保留失败：100 Hz 流式首版速度跳变 0.33 rad/s（量测重同步）已修为 0.013；100 Hz 档实际中位提交间隔约 20 ms（B 每次 SDK FK）。
（更正：当时记为“源码整体 CRLF 的既有问题”有误，见下一条。）

### 换行格式按 .gitattributes 修复（2026-10-29）

**A、B 文本换行格式更新（代码语义不变，重启加载，无需重校准）。** 26 个 `.sh/.py`、10 个 `.md` 改为 LF，2 个 `Start-Web.cmd` 改为 CRLF；
fixtures 未动。上一轮“全部 CRLF”是检查误报：实际 26 个，其中启动脚本等 8 个是上一轮用 Windows Python 编辑时引入的。
完整软件 920 项 0 失败；正式 Windows 启动器已能启动到 B，正式实例内 SDK 6 项、A→B 端到端 5 项通过。
仍未解决（既有）：Gazebo 初始化偶发 `Initial pose did not settle`（7 次中 4 次），只有 `right_j6` 卡在约 3.207（目标 −2.967，仿真上限 3.14）。
证据：[换行修复记录](../../windows-local/artifacts/line-endings-20261029/README.md)。j6 初始化问题已在上一条修复。
这是我们自己的规划器，只验证 SDK/action/停止/超时链路，不代表 Intera 衔接或手感，不能当真机验收。
证据：[本轮记录](../../windows-local/artifacts/sim-motion-server-20261029/README.md)。
下一步：决定是否修复 CRLF（按 `.gitattributes` 规范化）以便正式入口直接用；再用 Web/Quest 在 Gazebo 试四档频率。

### deploy 只保留 B-Lab-Ubuntu（2026-10-29）

**仅更新 B（实验室启动器 / 导出工具，A 兼容）；另整理部署文档。** 用户要求以后 `deploy/` 只保留实验室实机 B。
`export_deployment.py` 只生成 `<output>/B-Lab-Ubuntu`，去掉 A-Windows 与 `--windows-sim`，只校验 Ruckig，
因此无需 windows-runtime/APK/镜像即可导出；导出额外携带 Reset 姿态 YAML 与只读 IK 探测，与现有部署布局一致。
源码 `Start-B-Real.sh` 补上此前只存在于部署副本的 `rosparam load config/lab-neutral-pose.yaml`
（放在只读图检查之后，`--check` 先确认文件存在），避免重新导出丢失 Reset 姿态。运动参数、期限、协议、模型、依赖不变。
DEPLOYMENT_FOLDERS 重写、DEPLOYMENT 重排，其余部署文档、README、AGENTS 同步为“Windows A / 仿真用源码入口”。
Python 3.11 完整软件 **903 passed / 0 failed**；Python 3.8 导出/实机启动器 **42 passed**。
既有失败保留：`scripts/ubuntu/Start-A.sh`、`Start-Web.sh` 为 CRLF，Linux bash 无法执行，ROG A 启动测试失败，本轮未改。
现有 `deploy/B-Lab-Ubuntu` 未替换（10-23 导出 + 10-26 现场补丁，缺持久反馈日志与 Direct pose）；
`packaging/deployment/A-Windows`、`packaging/windows-simulation` 已不再使用，待手动删除。未连接 ROS 或机械臂。
证据见 [本轮记录](../../windows-local/artifacts/deploy-b-only-20261029/README.md)。
下一步需要新 B 功能时按部署目录导出并替换，现场 `--check` 后启动，A 重连/校准。

### 完全移除 Qt 界面与依赖（2026-10-29）

**仅更新 A（界面/入口/依赖/文档）；B 无需更新。** 用户要求不再修改 Qt 并完全清理。
删除 gui / log_panel / recording_panel / robot_view / viewer_process 及其测试与 gui_smoke；
纯 FK/有效期检查迁入 test_display_feedback。Web 为唯一 A 界面：Start-A 与 Start-Web 均启动网页，
`cli operator` 与 `web` 同义；pyproject 去掉 PySide6，windows-runtime 移出 PySide6/shiboken6（574→391 MB）。
文档的 Qt 入口与 3D 窗口说明改为 Web 的 Actual robot posture。控制、参数、协议、模型、期限、B、APK 不变。
Gazebo 容器自身的 `QT_X11_NO_MITSHM` 保留；旧 deploy 快照仍含 Qt，未覆盖。
完整软件 **891 passed / 23 skipped / 0 failed**（含 7,177 输入与旧轨迹）；首轮临时目录权限错误保留。
未重跑浏览器实际流程或 Gazebo/Quest。重开 A 后连接、校准，按原流程使用；不影响 B。
移出的 PySide6/shiboken6 经用户确认已删除，今后只用 Web。测试结果、两轮改前快照与 SHA 见
[Qt 移除证据](../../windows-local/artifacts/qt-removal-20261029/README.md)。

### 四段链路频率显示（2026-10-29）

**A、B 都需更新。** 现有 Web Live status 显示 A 接收（Quest→A）、
A 发送给 B（A→B）、B 执行提交、B→A 反馈接收，全部为最近一秒事件数，单位 Hz。
B 在 joint 直接提交、轨迹采样、旧 Cartesian 和 direct pose 成功提交点统计，
不以 A 收到回执的频率冒充 B 提交频率；保持/夹爪命令不计入该运动目标频率。
A 统计实际收到的 B state 包，不含回执和请求回复。旧 B 缺字段或 B 数据过期显示 —。用户明确要求仅改 Web，本轮 Qt 改动已撤回。
只增诊断计数和显示，运动参数、模型、期限、依赖及默认模式不变。
源码需重开 A/B、重连并校准；本轮未更新部署、未启动 ROS/Gazebo/实体设备。
专项结果与源码 SHA 见 ../../windows-local/artifacts/stream-rates-20261029/README.md。


### Web 反馈常显与 Last pause 折叠（2026-10-29）

**仅更新 A（Web 展示）；B 无需更新。** Timing/feedback 基础数据移出 details，一直可见。
Last pause reason 单独常显；只有明确的手动松手退出消息（Grip released; recording continues / not recording，
以及 released）使用普通颜色，其余非空暂停原因标红。完整 Last pause JSON 改为默认收起的 details toggle，
保留原始内容，轮询仅更新文本，不重置用户展开状态。无暂停时显示 — / No pause recorded。
无控制逻辑/参数/模型/依赖/协议或配置变动，未连接设备、重启服务或改变录制行为。
JavaScript 语法与 HTML 常显/折叠结构检查通过；现有 Web 检查 **15 passed**。
首次语法检查发现 render 内 reason 重名，已改用 lastPauseReason，失败与最终结果保留。
源码 SHA、差异和检查结果见
[显示调整证据](../../windows-local/artifacts/pause-display-20261029/README.md)。
加载方式见 DEPLOYMENT；无需重启 B 或重新校准控制算法，若刷新结束会话则重连后按既有流程校准。


### Web 日志高度与姿态面板顺序（2026-10-29）

**仅更新 A（网页布局）；B 无需更新。** Activity 日志显示区高度加倍：桌面
170→340 px，窄屏 200→400 px；Actual robot posture 移到 Activity 之后，作为页脚前
最后一个全宽面板，保留折叠与三维交互。未改控制代码、配置、依赖或运行进程。
静态 HTML/CSS 核对布局顺序、唯一 ID 与高度；原文件及 SHA 见
[本轮证据](../../windows-local/artifacts/web-layout-20261029/README.md)。
网页刷新后加载；先结束录制、松手并断连，再刷新、重连和校准。

### Fast follow 支持 Real Sawyer（2026-10-29）

**仅更新 A；B 无需因此更新。** 用户明确要求现有 Fast follow 也能用于实体后端。
Web/Qt 和 Operator 同时开放已确认的 intera 后端；原 Standard/Direct pose/旧 Cartesian 保留。
实体 Fast 独立使用 Standard 七关节加速度的 2 倍，当前基准对应 [3,3,6,6,6,6,6] rad/s²；
速度、轨迹算法、跟随误差阈值、关节保护与各时效预算不变。Gazebo 保留其专用覆盖值，不传到实体。
旧配置因存在 Gazebo override 推导的隐式 Fast 不会自动在实体启用；实体需显式选择。
Reset 继续用基准速度/加速度 25%，不随 Fast 增大。新档使用已有 joint arm 轨迹协议，远端 IK，
仍由 B 100 Hz 插值并发送实体 POSITION_MODE；不承诺实际实体加速度等于软件规划上限。
模型、依赖、APK、配置文件、默认选择和 B 源码未改；未更新历史部署或操作 ROS/机械臂。
相关软件回归 **262 passed / 0 skipped**；最终档位专项 **44 passed**，分次运行不相加。
实体 Fast 强制使用有界轨迹，旧配置关闭插值时也不会落入直接目标发布。
期间观察到并行网页 index.html/style.css 修改，原样保留，不计为本轮功能修改。
本轮输入/源码/参数哈希、回环与回归结果见
[实体 Fast 证据](../../windows-local/artifacts/intera-fast-follow-20261029/README.md)。
下一步在重开更新后的 A、重连/校准后由用户显式选择；实体跟随/停止仍需现场授权验收，release_accepted=false。


### Gazebo 初始姿态与 Reset 统一（2026-10-29）

**仅更新 B（Gazebo；A 兼容）。** 当前镜像 neutral 的 J6=3.3161 rad 被仅匹配实验室
3.3439736328125 rad 的适配遗漏，导致 Reset 禁用。现两组已知 J6 在原 ±3.14 rad
Gazebo 模型中均减一整圈；190° 对应约 −170°，不是 −10°。
仿真初始化与 B Reset 共用 named pose 和全七轴限位检查，初始化同步
`/sawyer_local_home`；官方 named pose、模型、实体后端、普通遥操目标和期限不改。
未知不兼容模型仍拒绝初始化；Reset 从正侧返回负侧仍沿限位内长路径。

109 项相关软件检查通过（含全部 7,177 输入、旧轨迹、Reset/Stop）。独立无端口映射
Gazebo 启动成功，B 报 Reset available，五组实际反馈与初始/Reset 目标误差均小于
0.03 rad，J6 约 −2.9670855 rad；未验证本轮完整 VR 或实际 Reset 往返。
验证环境日志目录权限和检查脚本 PYTHONPATH 失败记录均保留；源码/输入/参数 SHA
及结果见[本轮证据](../../windows-local/artifacts/gazebo-initial-reset-20261029/README.md)。
依赖、模型和 A 不变，无需重建镜像；原有 A/B 进程未重启或操作，旧部署未替换。
下一步按部署说明重启仿真 B、A 重连/校准后验证 Reset/Stop 与完整 VR。
独立验证容器已清理，本轮不代表完整发布验收。

### Direct pose 50 Hz 合入现有 Motion option（2026-10-29）

**A、B 都需更新。** 按用户纠正的范围，在现有项目的 Web/Qt Motion option 增加
`Direct pose (50 Hz, experimental)`，不再维护独立产品副本。原 Standard / Fast follow /
IK→FK Intera Cartesian 各档保留；当前配置、默认选择、启动入口、Web 端口和依赖不变。
A 直接发送映射/可选平滑后的末端位姿；B 固定 50 Hz 上限提交官方 Cartesian action，
握持启用时记录关节构型作固定 bias，跳过应用层 IK/FK；显式协商新能力，旧 B 不静默回退。
保留时效、STOP 确认和失败锁；录制用 submitted_pose 事件，旧关节动作导出不适用。
今日远端反馈兼容、持久诊断、control_configuration 与参数哈希保留；新增模式也记录原生参数与 bias。
此前独立副本逐文件确认无后续改动后，原样移至
`../windows-local/artifacts/direct-pose-50hz-20261027/archived-source/`，不再作为日常入口。
旧部署及其配置/录制未修改，未安装依赖、写远端、启动 ROS/机器人或重启现有进程。
相关软件检查 **284 passed / 0 skipped**，含 Web/Qt 新选项、原模式、今日 B 诊断及完整固定输入门。
本轮源码/参数哈希、失败与差异见
[整合证据](../../windows-local/artifacts/integrate-direct-pose-20261029/README.md)。
下一步仅在明确授权的现场阶段更新匹配 B，并重开 A、重连和校准；
原生 action 衔接、构型行为、不可达目标与实际停止仍未验收，release_accepted=false。


### 离开实验室前资料留存（2026-10-29）

**仅更新 A（文档/开发资料）；B 无需更新。** 用户确认询问的是后续本地工作是否还缺实验室资料，
不要求配置校外访问。已只读取回当前 B 源码/配置、Lin/ws/src、sawyer_ws/src 与连接脚本，
包含官方 simulator 的 head_interface.cpp 本地修改；逐文件读回并记录 SHA，旧官方快照未改。
RAM 中最后一份 B 状态也已保存，可能过期，不能当作当前机器人状态；此前已保存 ROS 和 10-26 原始诊断输出。
本地继续当前开发/诊断所需资料已齐，未发现仍必须取回的文件。证据见
[资料留存](../../windows-local/artifacts/lab-away-20261029/README.md)。
未修改远端网络/电源/文件/部署，未清理磁盘或操作机器人；本地代码/依赖不变，无需测试、重启或校准。
后续物理验证仍须实验室现场，资料备份不替代实测。

### 遥操实际方案记录与中文说明（2026-10-29）

**仅更新 B（A 兼容）；另整理 A/B 技术说明。** 成功 arm 时记录实际执行模式、规范化轨迹算法/参数、
Cartesian 协商频率、关节保护、时效预算、session/generation 与参数 SHA-256；
作为 control_configuration 事件和 last_arm_configuration 缓存进入既有持久诊断日志。
暂停/断连后保留最后方案，不将旧方案解释为当前已 arm；B 不伪造其无法获知的 A 平滑/位置比例配置。
控制算法/参数/模型/依赖未改，新增字段对原 A 兼容。
完整主链路、A/B 分工、Standard/Cartesian 区别、Reset 与故障恢复、配置与运行版本边界见
[当前遥操方案](CURRENT_TELEOP_METHOD.md)。独立 Direct pose 50 Hz 仅引用，不修改或合并。

方案记录、日志轮转、Bridge/dispatch、关节保护、native STOP、Reset 相关 **189 passed / 0 skipped**。
证据与输入/源码/参数哈希见 [本轮记录](../../windows-local/artifacts/teleop-scheme-20261029/README.md)。
仅离线；无 ROS/实体操作、无部署导出、未改远端。更新 B 并重连/校准后生效，A 无需升级。
下一步仍是按既有现场授权与发布门验证，日志记录不代表物理验收。

### B 有界持久反馈诊断（2026-10-29）

**仅更新 B（A 兼容）。** 用户要求为真实机械臂增加反馈日志。Intera B 默认开启有界 JSONL，
既有 0.5 秒状态循环读取缓存并写盘，控制线程不做文件 I/O；记录实际 q/dq/pose、原始急停枚举、
全部反馈 invalid 原因与时戳、最新指令、暂停上下文及新增 B 事件。每份约 5 MiB，三份备份，
保留启动参数/源码 SHA、启动失败及运行结束状态；写入失败报告但不终止控制服务。
暂停上下文增加故障当时的机器人状态和反馈，非硬件复位/使能逻辑，不修改保护或运动算法。
模型/参数/依赖不变，新增字段兼容原 A；fake/Gazebo 仅显式指定路径才记录。
不能当作完整轨迹或控制器内部故障日志；短暂状态和超过既有事件缓存的事件可能遗漏。

相关软件 **154 passed / 1 skipped**（Windows 跳过 Linux 信号用例），
并以独立 fake B 进程验证启动及三份连续反馈落盘，无 arm/目标/ROS。
证据/哈希与限制见 [本轮记录](../../windows-local/artifacts/feedback-logging-20261029/README.md)，
实际路径/开关/轮转使用见 部署。本轮未更新部署、未写远端或重启真实 B。
下一步更新 B 并重连/校准后才生效，现场验证仍待授权；A 无需更新。

### 关节超限后无法恢复：只读诊断（2026-10-29）

**仅更新 B（诊断记录）；A 兼容，产品代码未改。** 用户说明触发事件为关节超限。
实验室 SDK 明确提供关节故障 reset；`RobotAssemblyState.estop_source=3` 表示关节故障引发停机，
但本次未取得当次状态/具体关节故障码，不能认定该枚举当时出现。
当前 B 只检查 enabled/ready/stopped/error 等状态，不执行硬件 fault reset 或 enable；
A 的 Reset arm 是回中运动，不是硬件故障复位。最初将机械臂故障锁存列为假设，
后续取回当天项目会话原始输出后，不能将此假设当作该次已确认原因：
16:55:10 跟随误差 0.356 > 0.350 rad，16:58:19/28 回中时 0.353 > 0.350 rad；
随后仍有 Teleop active，表明这些软件保护未造成不可恢复锁存。
17:58:37 起多次 Latest robot feedback expired，直接表现为反馈失效，断流根因未确认。
18 点后另存的 B 状态为 joint 模式且 native_stop_failed=false，不支持当时是 native STOP 锁存。
18:12 TCP 拒绝及 18:14 恢复发生在用户已说明的机器人重启期间，不能倒推为重启前服务崩溃。

另已离线确认 Intera Cartesian STOP 超时/失败后 `native_stop_failed` 锁存，
即使模拟控制器随后确认停止并重连 A，joint/native 两种 arm 都仍拒绝；
没有仅靠重启机械臂清除此 B 内存状态的代码路径。不得通过自动清锁或放宽保护绕过。
10 项关节保护重启用及 STOP 专项通过；复现零 joint publish，只用软件替身，未连接 ROS。
远端已有 10-26 18:12 故障诊断日志显示 `sawyer.local` 解析失败，18:14 重启后日志恢复连接；
这些是连接证据，缺少故障状态正文，不能据此定下关节故障根因。
日志、复现、哈希、失败和决定见
[诊断证据](../../windows-local/artifacts/stop-recovery-diagnosis-20261029/README.md)。
下一步在再次出现时先保留原始错误、robot/state、关节诊断、B native STOP 状态与网络状态；
据故障类型设计显式恢复流程，物理复位/使能和动作需单独授权，本轮未执行。
模型/参数/依赖/部署和运行进程均不变，无需重启或重新校准。

### 实验室 B 远端改动合并到本地（2026-10-29）

**仅更新 B（A 兼容）。** 从 实验室 B 电脑上的 `B-Lab-Ubuntu` 只读获取快照，
69 个文件与远端实时 SHA-256 全部一致。远端 `intera_backend.py` 的空 endpoint `frame_id`
兼容和 cuff 事件状态处理已存在本地 B 部署，本轮补入 `sawyer_teleop` 主源码，
并合入远端 7 项反馈测试；已有 `trajectory.py` 修改与主源码一致，无需再改。
保留本地较新的 Reset 姿态、启动参数加载和 IK 探测工具，不以远端旧文件覆盖；
实验室地址配置和部署 README 已存在本地部署，不覆盖主工程 README 或配置模板。
独立 `sawyer_teleop-direct-pose-50hz`、A、部署目录及远端文件均未修改。

模型、配置、依赖、协议不变。本轮只离线测试，未连接 ROS、启动 B 或执行实体机器人动作。
反馈/adapter/Gazebo/neutral/reset/trajectory 相关 **180 passed / 0 skipped**。
首次运行因旧 adapter 测试夹具缺少新增 cuff 订阅对象而失败，补齐夹具后通过，失败报告保留。
输入快照、合并前源码、差异、清单与哈希及两次测试报告见
[合并证据](../../windows-local/artifacts/lab-merge-20261029/README.md)。
当前运行进程不受影响，无需本轮重启或校准；后续使用主源码启动 B 才加载此修改，
需按原流程重连/校准。本轮不替换运行版，现场验证及既有发布门仍未完成。

### 实验室确认 Reset 姿态（2026-10-26）

用户确认当前实验室 Sawyer 关节姿态作为 Reset 目标。新增 `config/lab-neutral-pose.yaml`，
由 `B-Lab-Ubuntu/scripts/ubuntu/Start-B-Real.sh` 在启动 B 前加载 ROS 参数
`/named_poses/right/poses/neutral`；A 会在 B handshake 后读取这组姿态。加载参数不启用、不移动机器人。
实验室需复制 B 部署并重启 B，再重开 A；未在本机连接 ROS 或实体机器人验证。

### B IK 只读排查工具（2026-10-26）

新增 `scripts/probe_intera_ik.py`，只读取当前 B 反馈并调用 Intera IK service，记录每次 IK
耗时、反馈年龄、成功/失败、P50/P95/最大值及超过 80/100 ms 的次数；不 arm、不 publish、不发送运动目标。
该工具用于区分 Intera IK 服务延迟与 Quest/A 网络问题，需在实验室 ROS 环境中手动运行。

### 待办

- [x] **Reset arm 回中轨迹**：A 的 Reset 专用 cubic 轨迹速度和加速度均降为当前 A 基准的 25%；保留 B 的跟随误差保护阈值，不通过继续放宽阈值规避错误。仿真/离线 reset、neutral、joint protection 专项通过；未部署、不在实体机器人上试跑。现场使用需重开修复后的 A。
- [x] **实验室默认 Reset 值统一**：源码和 A/B 部署测试后端默认 neutral，以及 B 启动时加载的 ROS 参数，统一为用户确认的七关节姿态；运行中的 ROS 参数不会热更新，必须重启 B 后由 A handshake 读取。
- [x] **轨迹跟随误差阈值按用户要求调整**：`max_following_error_rad` 从 `0.350` 改为 `0.500`，同步源码、A-Windows、B-Lab-Ubuntu 和 Windows 本机配置；`joint_protection.py` 的 `MAX_STEP_RAD=0.350` 保持不变。143 项轨迹/reset/neutral/joint protection 软件专项通过。运行中的 A/B 未热加载，实验室需停止旧 B、更新并重启 B，再重开 A；实体安全风险由用户明确承担，仍需现场观察。

## A 握持启用后输入过期修复（2026-10-26）

**仅更新 A；B 无需因此更新。** `operator.py` 在 arm 确认后重新 poll Quest，校验
输入年龄/未来时间、有效跟踪、grip、校准 epoch 和 B 启用状态；以确认后的同一输入与反馈
建立控制原点及首个 IK，失败则 hold，等待重新握持或校准。保留 IK 返回时原始输入检查。
暂停状态保存输入原始年龄/不确定度/过期与未来判断、arm 等待、IK 排队/调用/结果等待；
Qt 主状态悬停详情可查看，无需录制。Quest 同步保留五次 RTT、候选不确定度及拒绝原因，
输入保留原始映射时间用于 source→read / read→poll 诊断；不清空日志、不刷新旧时间戳。
输入 150 ms、IK task 100 ms、remote IK 80 ms 与 B 100/120/200 ms 均未改。
新增 arm 边界测试覆盖新输入、无新输入、松手、跟踪丢失、epoch 改变、未来时间、停止、
arm 异常，以及新输入不能替旧 IK 续期。原 native profile 测试补上真实握持状态。
最终时序专项 **83 passed**；完整软件回归 **898 passed / 23 skipped / 1 failed**，
唯一失败为仿真握手用例在 B 确认停止前切换 profile；等待确认的用例已单项通过，
另一次负载下出现握持等待超时，失败均保留，不宣称完整回归全绿。
用户明确要求停止追查握手测试、优先现场跑通；不继续扩大握手排查。
完整回归覆盖全部 7,177 实验室输入及旧轨迹；最后的暂停诊断保留改动由 83 项最终专项覆盖。
原始报告与源码 SHA 见
[本轮证据](../../windows-local/artifacts/arm-input-fix-20261026/README.md)。
用户另行更新的 deploy/B-Lab-Ubuntu/intera_backend.py 已核对并保留：端点空 frame_id 及
cuff 事件状态处理差异，不直接改 Quest/IK 输入时限；不能从源码或先后顺序排除间接时序影响。
未连接/控制实体机器人，未导出部署、重启进程或修改 B。A 加载改动需手动重开并重新校准；
下一步由用户手动重开修复后的开发 A、重连并校准，使用现有实验室 B；
若仍失败，保留主状态悬停的暂停诊断、ADB RTT/输入年龄以区分实际延迟来源。

## Qt IK 输入过期排查（2026-10-26）

**仅更新 A（排查文档/离线诊断）；B 无需更新。** 用户确认 Qt，反复松手重握仍见
`ik_input_expired`。本机配置为 intera / intera_cartesian_100hz，当前实际后端尚未确认。
离线复现：同步 arm 确认耗时 80 ms 后仍用确认前输入启动 IK，再耗时 80 ms，
原始输入年龄 160 ms，连续三次重握均过期；两种既有 profile 均复现。
正常时序下相同错误后可以重握恢复，尚无永久错误锁证据。
25 项专项通过；未取得该次 Qt 内存时序，不能将此复现等同现场根因。
源码、期限、模型、依赖、部署和运行进程均未改变，无需重启或重校准。
证据/失败/哈希见 [诊断](../../windows-local/artifacts/ik-expired-diagnosis-20261026/README.md)。
下一步取得现场输入/arm/IK 时序，验证 arm 后重取输入并检查松手的最小修复及完整既有轨迹门。

## 既有决定

用户认可 Standard / Fast follow 的 Gazebo 手感，两项保留。新增 Intera Cartesian 原生执行选项；本轮仅实现与离线验证，真机留待现场。
已验证范围仍是 ROG 同机 Gazebo；停止、完整 VR、耐久和真机验收未全部完成。
**正式运行版未替换；`release_accepted=false`。**

Windows 运行 A，也可本机运行 Gazebo B。ROG Ubuntu 继续支持仿真。
实验室 Ubuntu 仅运行实体 Sawyer B，复用已有 ROS Noetic / Intera，不安装或运行 Docker。

## HTML 操作台（Qt 保留，开发源码交付）

**仅更新 A；B 无需因此更新。** 新增本地 HTML/CSS/JavaScript 操作台和独立 Start-Web 入口，
原 Start-A / Qt 主窗口继续可用。网页服务调用现有 Operator，保留输入、滤波、IK、录制及 A/B 协议；
共用三维反馈读取和 FK 从 Qt 模块提取到不依赖 Qt 的 display_feedback，计算逻辑原样保留。
本轮没有更改模型、运动参数、控制期限、Quest APK 或 B 执行源码，也没有新增运行依赖。

网页覆盖连接/设备发现、平滑参数、运动选项与关节保护 Apply、校准/回中、反馈诊断、日志和批量录制。
按用户后续要求，Actual robot posture 放在 Live status 下方，默认折叠，点击标题才读取/展示三维反馈。
本机服务仅绑定 127.0.0.1；临时授权、单页面操作权与请求去重由服务检查。
页面关闭/刷新或连续 3 秒失联时结束本次 Operator，未完成录制保留为不完整数据，不自动重新接管。
这项网页监督期限独立于既有 Quest / IK / B 的时限；浏览器休眠/节流可能触发，操作说明见 DEPLOYMENT。

验证：14 项 Web 软件专项通过；既有相关软件回归 **136 passed / 22 skipped**，
包括 Qt、只读 FK、完整固定实验室/旧轨迹、Operator、录制、启动和部署导出检查；
22 项为当前 Windows 环境无法执行的 Ubuntu shell 用例。Qt 控件使用 offscreen 软件验证。
原生 Windows 启动器实际启动 Web、返回本地页面通过；Web 导入不加载 PySide6。
Chrome 实际点击完成连接、校准、自动命名、两条定时批录制、折叠/展开和三维反馈显示。
浏览器验收用合成 Quest + 软件 B，不是 Gazebo、真实 USB/Quest 或实机验收；完整发布门仍未通过。
内置浏览器首轮出现 Failed to fetch 并触发 3 秒结束保护，原事件保留；系统 Chrome 中未重现，
不将这次环境差异解释成控制性能改善。

源码、原始输入及参数 SHA、测试 XML、原观察失败、录制与截图见
[本轮证据](../../windows-local/artifacts/web-ui-20261026/README.md)。
本轮只更新开发源码，没有重导出或覆盖现有 deploy 快照；`release_accepted=false`。
加载新 Web 入口需要重开 A，重连并校准；B 无需因此重启。Qt/Web 切换前结束录制并断开旧 A。
下一步在目标 Windows / ROG 验证真实 Quest 和 Gazebo 完整链路，继续沿用现有停止、录制与耐久发布门。

## 本轮：Quest 长输入包截断修复

**仅更新 A（含 Quest APK）；B 无需更新。** 截图中的 JSON delimiter / char 1025 错误已用当前协议的
1024 字节长包精确复现：旧 `__android_log_print` 的 1024 字节缓冲只保留 1023 字节，截断了 JSON。
改用完整字符串写入并拒绝超过 4000 字节的包，不删除字段、不降低精度、不改 120 Hz 请求或运动算法。
APK 升至同签名 versionCode 9 / `4.3-input120`；旧坏包仍暂停，A 明确提示运行 Install-Quest.cmd。
**只更新 PC 文件不够：必须在 Windows 运行新版 Install-Quest.cmd 更新头显，再重开应用 / A、重连和校准。**
模型、控制参数、反馈 / 输入期限与 B 不变。安装 / 导出拒绝已知存在截断问题的 versionCode 8。

888 项完整软件检查通过；原生写入边界 1023 / 1024 / 1500 / 4000 字节通过，4001 字节发送前拒绝。
隔离 Gazebo 健康检查通过；全部 7,177 个原实验室快照按原节拍经过生产读取代码，诊断字段扩充为长包，
4,147 个实际写入包为 1424–1452 字节，JSON 错误 0、目标提交 3,251 次，实测关节产生运动。
原输入、时序和运动字段不改；4 次 quest_stale、1 次 ik_input_expired、3 次松手暂停均保留。
本轮主机模拟 Android 日志出口，未接实际 Quest，不能冒充 Windows / 头显或完整物理发布门通过。
历史原收包时刻缺失仍未补造；`release_accepted=false`，正式运行实例未替换。
三套部署同步到当前 deploy；小补丁、证据、原失败和修复前后对照见
[quest-packet-fix](../../rog-local/artifacts/quest-packet-fix-20261023/)。下一步在 Windows 安装 versionCode 9 后核对实际 DISPLAY / INPUT、校准、握持和按钮。

## 统一三套部署包

**仅更新 A（部署文档/开发工具）；B 无需因此更新。** 产品代码、配置、模型、依赖与运动参数没有变化。
重新生成同批 `A-Windows`、`B-Lab-Ubuntu`、`Windows-Simulation`，入口统一为 `../deploy/`。
用户明确要求删除旧部署包；已删除此前日期目录及旧三套，将验证后的新三套移到 deploy 根目录。
历史实验、原始录制和本机配置保留；包内说明明确右后方 3D view、实机 Standard 和仿真 Fast follow 的范围。
80 项部署 / 启动 / 显示 / 固定录制检查通过，包含最新实验室 7,177 输入及旧轨迹；逐文件 SHA 与源码一致性通过。
重导出不改变正在运行的实例；如加载整批新版，结束录制后退出旧 A/B，按原流程重开、重连 / 校准。
整批仍包含此前双端功能，应配套更新 A/B；本轮没有启动 ROS、Gazebo、Quest 或实机。
Windows 原生与现场验收仍待完成，`release_accepted=false`。详见 [部署整理证据](../../rog-local/artifacts/deployment-refresh-20261023/)。

## 三维显示默认右后方视角

**仅更新 A；B 无需更新。** 默认观察方向改为机械臂右后方、俯视 25°；Reset view 回到该角度。
只改相机方位，反馈、运动、配置、模型和依赖不变。重开三维窗口加载；无需重启 B 或重新校准。
25 项现有显示 / 固定录制回归通过，包含最新实验室全部 7,177 输入与旧轨迹。
使用上轮保存的 Gazebo 实测姿态渲染同姿态前后对照；本轮未新启动 Gazebo，不冒充实时画面。
证据见 [右后方视角](../../rog-local/artifacts/robot-view-right-rear-20261023/)。新候选目录为
`../deploy/20261023-right-rear/`，旧快照和正式运行版保留，发布状态不变。

## A 实际反馈三维显示

**仅更新 A；B 保持兼容。** 新增默认关闭的 3D view，独立窗口显示 B 实际关节反馈驱动的简化机械臂。
复用 B 已有模型与反馈，不读取发送目标、不增加 IK / 控制接口、不增加依赖或模型包。
绘制在独立进程，A 以约 30 Hz 非阻塞发送最新显示快照；拥塞丢弃显示更新，不排队。
过期冻结、变灰并标记，断连 / 新会话清除旧姿态，显示进程不持有 B 控制连接。
完整软件 884 项通过；最新实验室 2,917 个和旧轨迹 2,331 个实测姿态逐点匹配独立 FK。
隔离 Gazebo 健康门通过，关闭—打开—关闭三轮都送入全部 7,177 原始输入、保存成功。
打开时 1,698 次显示更新与 B 实测关节值完全一致；显示发送 P95 0.088 ms。
输入接收→目标发送 P95 为 8.519 / 8.467 / 8.454 ms；控制 tick 间隔 P95 均约 8.33 ms。
显示回归检查通过，不把三轮波动解释为控制改善。基线中的 IK / 输入过期暂停、各轮长尾和原输入过期段全部保留；
历史快照缺失的原收包时间没有重建，不能当作完整真实 Quest / 物理发布门通过。
重开 A 加载新界面，按原流程重连 / 校准；会话内切换显示无需重连或校准。
证据在 [robot-view](../../rog-local/artifacts/robot-view-20261023/)，使用见[三维显示](ROBOT_VIEW.md)。
新候选目录为 `../deploy/20261023-3d/`，旧运行版保留；Windows 原生与真机未执行。
同一源码包含此前关节保护和 Intera 的 A/B 改动，整套更新不能据本节单独替换 A。

## 双端关节跳变保护（离线已验证）

**A、B 都需更新。** UI 新增默认勾选 Joint protection，所有遥操运动选项共用 0.35 rad 单关节阈值。
A 检查 IK/目标，B 独立检查远端 IK、接收目标与实际提交；拒绝跳变并暂停，回中使用受限的官方 neutral 路径。
三份完整录制 6,966 对相邻目标最大 0.288065 rad，0.35 rad 无拦截；10 Hz 合并目标可能超限，现场再比较。
详见 [设计与数据边界](JOINT_PROTECTION.md)。旧录制、失败段和 7,177 输入均保留。
本轮仅离线，未启动 ROS/Gazebo/真机，未打包或覆盖冻结部署。依赖、模型、APK、时间预算不变。
重启 A/B、重连/校准加载新代码；之后松手并结束录制，用 Apply 同时应用选项和保护勾选。
完整软件 **868 passed / 0 skipped**，包含本轮保护专项及完整实验室/旧场景回归；不代表物理或真机验收。
原始测试与逐项统计存于 [本轮证据](../../rog-local/artifacts/joint-protection-20261023/)。

## 输入平滑轻度 / 重度（软件已验证）

**仅更新 A（界面与平滑参数选择）；B 无需更新。** Smooth teleop 保留关闭选项，
新增 Light 20 ms / Heavy 40 ms，原 20 ms 配置仍对应 Light；旧自定义时间常数原样保留。
下次连接保存并传入既有滤波器，不在当前会话热切换；不改滤波算法、IK、轨迹、速度 / 加速度、模型或任何期限。
重开 A 加载界面，结束录制、松手并重连 / 校准后应用设置。独立于 Motion option 的 Apply。
相关软件回归 146 项通过，两档均覆盖录制、松手 / 追踪丢失 / 反馈过期 / 慢 IK 取消与重握，
包含最新实验室全部 7,177 输入和旧横移 / 抬手轨迹。Qt 界面、配置保存、重连、普通遥操不录制和保存 / 丢弃流程通过。
仅软件与合成设备验证；本轮未重启 Gazebo / 生产 B，未验证新入口的 Windows 原生 / Quest 实际手感，也未打包或覆盖部署。
期间观察到并行关节保护修改，全部保留；为避免混用版本，另存 `tested-source` 快照再次通过相同 146 项，
两轮测试不相加。平滑功能自己的生产差异只涉及 gui.py，见 `smoothing-only.patch`；
本节“仅更新 A”只适用于该差异，不能套用到其他并行功能的整套源码更新。
证据在 `../rog-local/artifacts/smoothing-levels-20261023/`，实际控制发布门仍待完成。

## B 地址历史与部署快照

**仅更新 A（界面与连接成功状态）；B 无需因此更新。** B address 改为可输入下拉框，
仅在完整 B 握手成功后保存最近 10 个地址，去重并将最近一次置顶；重开恢复上次成功地址。
失败、超时、仅 socket 已连接都不写历史；B 已连接但 Quest 启动失败时仍保留正确的 B 地址。
配置写入现有本机 operator.json，旧配置继续兼容。导出新副本清空本机地址历史。

本轮不改运动、模型、依赖、APK 或保护参数。重开 A 才加载界面；重连后按原流程校准。
137 项相关软件回归通过，含最新实验室全部 7,177 输入及旧轨迹。
实际 Qt 窗口用回环软件 B 完成两次连接和重开恢复，截图与证据保存在
[address-history](../../rog-local/artifacts/address-history-20261023/README.md)。
原 deploy 的 17,124 个文件逐项 SHA 未变；新候选已导出到 `../deploy/20261023/`：
A-Windows、B-Lab-Ubuntu、Windows-Simulation 均逐文件校验通过，产品源码与本轮一致。
校验脚本曾误要求部署携带开发 egg-info，失败已保存；修正比较范围后通过，产品无须返修。
新快照同时包含前轮 Intera Cartesian 改动，该功能仍要求 A/B 配套，不能将本轮“仅更新 A”套用到它。
未执行 Windows 原生运行、ROS/Gazebo 或实机；不替换正式运行版。

## Intera Cartesian 频率对照

**A、B 都需更新。** Motion option 保留 Standard / Fast follow，原生选项扩展为
`Intera Cartesian (10 Hz)`、`(20 Hz)`、`(50 Hz)`、`(100 Hz)`。原有保存键对应 10 Hz。
A 检查 B 宣告的频率选择能力，arm 显式协商本次频率；旧 B 提示升级，不静默改成另一个执行方式。
Apply 仍需松手、结束整批录制、停止已确认。四档只改变目标提交频率，速度/加速度一致。

SDK 核对：每次提交新的 MOTION_START，客户端转而跟踪最新 action，不主动取消旧 action；
异步返回不表示机器人已接受或到位。公开客户端没有跨 action 连续衔接保证，内部控制器行为仍需真机测量。
默认 corner_distance=0 的路点零速含义不能当作跨 action 平滑机制。

B 在 100 Hz 控制环中提前一轮准备 FK，按就近控制周期提交；跳过错过的周期，不排队补发。
发送频率是目标节拍，实际间隔受控制周期量化、抖动、输入和 FK 影响，不保证每次恰好间隔 1/hz。
保留一个有效在途 FK 和一个最新待处理槽，仍复验原引用、generation 与期限；STOP 异步确认和失败锁定不变。
录制包含本次所选频率、实际提交间隔、提交时原引用年龄、提交前最新 action 状态，不冒充物理轨迹参考。

离线测试已复现并修复“每次额外等待一轮”和“严格起算点导致频率减半”；原始失败证据保留。
10 Hz 加少量通信/FK 延迟仍可能耗尽 120 ms 原引用预算，保留作对照，不当成最终推荐。
完整软件 **790 passed / 0 skipped**，原生专项 70 项；结果见 [本轮证据](../../rog-local/artifacts/intera-cartesian-rates-20261023/) 和 VALIDATION。
全部 7,177 条实验室输入与旧轨迹保留；两条真实 Quest 录制六个源文件 SHA 未变。

代码需重开 A/B、重连/校准后加载；同次已停止会话内切换档位无需重启或重校准。
模型、原有两项参数、依赖、APK、100/120/200 ms 期限不变。仅离线，未启动 ROS/Gazebo/真机，未覆盖部署快照。
此前初版 732 项结果和 38 项原生专项仍保存在 [初版证据](../../rog-local/artifacts/intera-cartesian-20261023/README.md)。

## 目录与迁移：模块化工作区

**仅更新 A（文档/启动安装与开发工具）；B 无需因此更新。** 与上述 Intera Cartesian 功能工作分开记账。
用户要求日常只传小源码，各环境按需复制或替换。已统一到 Sawyer-Workspace：

- sawyer_teleop 约 15 MB；Windows Python/adb 在 windows-runtime，固定镜像在 gazebo-image。
- rog-local 与 deploy 一起归入父目录；原环境、录制、失败和部署快照保留，无运行实例被中断。
- 启动和导出自动寻找同级资源；部署导出仍生成自带 runtime 的独立目录，不覆盖已有输出。
- Windows 自动生成 windows-local 保存本机配置、录制、日志、测试报告；替换源码不覆盖这些内容。
- ROG 虚拟环境仅修复安装和源码路径；依赖版本不变。未启动 ROS、Gazebo、Quest 安装或实机。

本轮没有修改运动算法、保护参数、模型、原配置模板或 APK；Quest 安装器仅增加可选本机配置路径。
86 项相关软件回归通过，包含最新实验室 7,177 输入及旧轨迹；真实资源三类临时导出与逐文件校验通过。
目录启动、导出与固定录制验证见 [modular-workspace](../../rog-local/artifacts/modular-workspace-20261023/)。
Windows 原生运行仍待目标机检查，正式运行版未替换。操作见[开发指南](DEVELOPMENT.md)。
之前整目录迁移的 678 项软件结果仍保留在 [历史证据](../../rog-local/artifacts/windows-development-20261023/)。

## 已完成的能力与证据

| 范围 | 状态 / 边界 |
| --- | --- |
| Quest 120 Hz | versionCode 8 / `4.2-input120`；[实测 119.98 Hz](../../rog-local/artifacts/quest-120-install-20261022/actual-rate.json)，当次手柄未追踪，不代表完整新版遥操验收 |
| 两项运动选项、完整入口录制 | 用户认可手感；[两条真实试用审计](../../rog-local/artifacts/rog-live-recording-review-20261022/review.json)，保存成功不等于停止门通过 |
| Reset scene、UI 与日志 | 源码和相关软件/隔离仿真已验证；Reset scene 需 A/B 配套加载；详见 VALIDATION |
| 全项目审阅 | 634 项当时软件回归通过；[可靠性修复证据](../../rog-local/artifacts/reviewer-cleanup-20261022/README.md)，后续源码需单独验证 |
| 四类运行入口 | Windows A / 仿真、ROG 仿真、实验室实机；91 项相关检查通过，实机未执行；[入口证据](../../rog-local/artifacts/launchers-20261022/) |
| 独立部署目录 | [A / 实验室 B](../../rog-local/artifacts/deployment-folders-20261022/) 与 [Windows 本机仿真](../../rog-local/artifacts/windows-sim-deployment-20261022/)已生成和校验；Windows 原生未验证 |
| 官方 Cartesian 路线 | 已新增四档原生执行候选，软件证据见本轮记录，真机待现场；当前 Gazebo 缺 `/motion/motion_command` 五个 action 接口；N1/N2/N3 未运行；[服务核对](../../rog-local/artifacts/rog-native-20261022/) |

原用户试用源码保存在 [tried-source](../../rog-local/artifacts/rog-live-recording-review-20261022/tried-source/)，不被新开发覆盖。
不要依据旧日志推断当前实例在线；磁盘源码、运行进程和部署快照分别核对。

## 两条真实录制

| 选项 | 时长 | 动作 / 实测状态 | 留存问题 |
| --- | --- | --- | --- |
| Fast follow | 48.538 s | 4,123 / 4,831 | 5.545 s 一次本地 IK 无连续解暂停 |
| Standard | 60.005 s | 5,590 / 5,998 | 58.729 s 右手柄追踪丢失 |

两项无 recording_error，输入序号连续、握持锚点完整；动作不同，不能直接排名或计算同条件改善比例。
两条新录制不替换旧实验室 fixture，不补造旧数据缺失的收包时刻。

## 待评估优化：自适应输入平滑（暂不实施）

2026-10-23 用户要求仅记录为优化参考；暂不实现、不运行新实验、不打包，不列入当前执行任务，后续由用户明确启动。
**本次仅更新 A（文档/开发工具）；B 无需更新。** 代码、配置、模型、依赖和运行进程不变，无需重启或重新校准。
未来若按以下范围实现，仍仅更新 A；B 的协议与执行语义保持兼容。

目标：静止时加强抑抖，正常移动时减少滤波滞后，同时保留慢速精细操作。
候选参考 [One Euro Filter 作者说明与实现](https://gery.casiez.net/1euro/)，在 A 坐标映射之后、IK 之前，
按平滑后的速度矢量和角速度连续调整强度；位置三轴共享系数，旋转沿最短路径单独调参。
新增可选 Adaptive 实验档，替换当前固定滤波，保留关闭 / Light / Heavy 对照；不叠加第二层位姿滤波。
静止等效时间常数 20–40 ms、移动 5–10 ms 仅为待探索范围，不是已验证参数或端到端延迟承诺。

先固定 Motion option 为 Standard，仅改变 A 滤波，避免与 B 运动参数同时调整。
自适应滤波与 B 轨迹分工不同，但目标变化会影响跟随误差、暂停及实际路径，必须联合验证。
保持最新目标覆盖、不排队、松手优先停止并清空滤波状态、原有过期和关节保护；首轮不加位置锁定死区或运动预测。
快速微抖也可能有大速度，慢速有意运动也可能被过度平滑，不能将速度等同于用户意图。

验证参考：先同输入离线对照，再做 Gazebo；静止、往复、三点 / 四点循环、随机抖动，慢直线、微位移、小圆、
慢速 / 快速转腕、起步、换向、停手仍握持、松手与重握均需覆盖。保留实验室全部 7,177 输入、
旧横移需抬手才能跟上的证据与停手用例、两条真实 Quest 录制及失败 / 无提交段。
分别统计滤波目标和末端实际反馈的抖动、输入到目标 / 提交 / 反馈的响应、路径误差、过冲、收敛尾巴、暂停原因及停止时间。
期望静止抑抖接近 Heavy、移动延迟优于 Light，并且精细操作、换向和停止不退化；这只是待验证目标，不能视为现有收益。

## 下一步与发布门

1. 在迁移后的 Windows 执行软件测试，再验证本机 Gazebo / Quest 链路；保存结果和失败。
2. 已知 IK 暂停只在证据支持时做必要修复。保持最新实验室全部 7,177 输入，以及旧横移/抬手、停手和两条新录制。
3. 按[固定门槛](reference/ROG_TRACKING_EXPERIMENT.md)完成停止、录制、完整链路三次重复与十次耐久，全部通过后再替换运行版。
4. 真机是单独现场阶段；当前未连接、使能或执行真机，不将 Gazebo 的快速参数套用到实机。

更早实验与淘汰分支见历史归档。上一版完整状态原文保存在
[本轮 before](../../rog-local/artifacts/windows-development-20261023/before/docs/PLAN.md)，逐轮数据和失败继续保留。

- [x] Reset follow-up (2026-10-26): Reset-only speed/acceleration reduced from 50% to 25% of A baseline after entity B still reached 0.503 rad at the 0.5 guard. Reset/neutral/joint protection tests passed; B threshold remains 0.5 and joint jump protection remains 0.35. Restart A before field use; do not execute another entity reset until A is reloaded.
