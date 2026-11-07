# 项目状态

更新：2026-10-29。当前状态以本页为准；安装见 [SETUP](SETUP.md)，操作见 [USAGE](USAGE.md)，开发与验证见 [DEVELOPMENT](DEVELOPMENT.md)。

> 2026-10-29 发布整理：Windows 入口合并为仓库根目录的 `Setup.cmd` / `Start-A.cmd` / `Start-B.cmd` / `Test.cmd`；
> 文档合并为 SETUP / USAGE / TROUBLESHOOTING / DEVELOPMENT。下文历史条目里的旧脚本名（Install-Dev、Test-A、Stop-B、Install-Quest 等）、
> 旧文档名和 `rog-local/`、`windows-local/artifacts/` 证据链接均指整理前的原作者本机环境，未随仓库发布。

## 当前决定

### Smooth teleop 默认改为 DROID-like 150 ms（2026-11-07）

**仅更新 A（配置模板），B 无需更新；无代码 / 依赖变化，无需重校准。** 用户要求把界面上已选的 Smooth teleop 开启 +
DROID-like 150 ms 设为默认。仅将 `config/operator.json` 的 `motion.time_constant_ms` 由 20.0 改为 150.0（`enabled` 原本即 true）；
同文件中他人未提交的 `cameras` 改动原样保留。PoseSmoother 上限、Web 档位、校验和 `web_session` 缺省回退 40 ms 均不改。
模板只在首次启动复制为本机配置；本机 `windows-local/config/operator.json` 已保存为 enabled / 150，未改动。
USAGE、CURRENT_TELEOP_METHOD 同步，新增 `tests/test_smoothing_default.py`。相关 97 项（含 lab scene 7,177 输入、Web、启动器、部署）
75 通过 / 22 Windows 跳过 Ubuntu 脚本 / 0 失败。150 ms 约 450 ms 达 95%，滞后明显，仍需实测手感；不代表实机验收。
证据：`../../windows-local/artifacts/smoothing-default-150-20261107/`。下一步：重启 A 即用新默认（已有本机配置不受影响）。


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
