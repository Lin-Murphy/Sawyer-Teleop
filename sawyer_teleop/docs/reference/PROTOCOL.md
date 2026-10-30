# A/B 协议 v1

RPC 是调用方式，TCP 是传输层，二者不互斥。本项目在两条 TCP 持久连接上使用轻量 JSON 请求/回执：
真机 / fake 的控制与状态共用 8765，远端 IK 使用 8766；Gazebo 对应 8875 / 8876。本地 IK 只需控制连接。

每帧是 4 字节大端无符号长度 + UTF-8 JSON 对象，最大 65536 字节。
拒绝重复 JSON 键、NaN、Infinity、错误维度和未知消息。TCP_NODELAY 已启用。
发送线程与读取线程独立；待发送目标和状态各只有一个 latest 槽；
可靠消息队列最大 128，溢出或发送超时关闭连接，不积压旧动作。

## 建立连接

控制握手：`{"type":"hello","version":1}`。
响应提供 `session`、`generation`、关节顺序、弧度单位、base/tip、后端说明、超时参数。
仅允许一个 A 控制连接，第二个连接拒绝。IK 握手包含同一 session。
IK 连接不会自行获取控制权。

所有控制请求带 `session`；除 target / gripper_observation 流消息外，请求另有 `id`。
最近 128 个成功控制请求缓存回执，重复同一请求不重复执行；重复 id 内容不同则拒绝。
超过保留窗口的旧请求拒绝。A 不盲目重试 arm/hold：请求超时关闭控制连接。

| 类型 | 关键字段 | 含义 |
|---|---|---|
| ping | id | 返回 B 单调时钟 b_ns |
| arm | id, feedback_seq, trajectory / execution_mode（可选） | 反馈有效才能获取新 generation；可选受限轨迹 |
| target | seq, generation, feedback_seq, q[7], gripper（可选） | 更新最新关节与夹爪目标槽 |
| gripper_observation | value（-1 / +1 / null） | 仅显示扳机转换值，不执行动作 |
| hold | id, reason | 取消槽并更新 generation；关节模式提交保持目标，原生模式提交 MOTION_STOP 并异步确认 |
| reset_scene | id, generation, feedback_seq | 仅支持该能力的 Gazebo；已保持并位于 neutral 时启动异步物体复位，返回任务 id |
| state | sample, armed, reason, generation, events | B 定期发送，不伪造源数据更新 |
| submitted | seq, generation, q, b_ns, before | 后端 publish 返回后回执，表示提交，不表示到位 |
| solve | id, generation, feedback_seq, pose, seed, timeout_s | 独立 IK 连接，仅求解 |

target 必须满足：当前 session、当前 generation、递增 seq、有效有限 q[7]、
引用的反馈在 B 的 64 条源样本窗口内且未过期。入槽和 publish 前各检查一次。
未协商轨迹时，关节数值直接传给后端。目标 seq 可跳号，旧目标覆盖不补发。

### 可选仿真场景还原

Gazebo hello 的 `scene_reset` 包含 `available`、`models`、`reference`、`resets_clock=false`；
不可用时给出 `reason`。新版 A 的 Reset arm 先沿原受限轨迹回中、保持，然后发送 `reset_scene`。
新版 A 连接不支持该能力的旧 Gazebo B 时禁用 Reset arm 并提示升级；真机 / fake 保留原回中行为。
旧 A / 策略客户端连接新版 B 时不自动恢复场景，必须显式请求该能力。

B 要求正确 session/generation、有效当前反馈和引用、未 armed、已有 hold，以及 neutral 误差
小于 0.008726646 rad、速度小于 0.02 rad/s。ROS 服务在独立工作线程执行，不阻塞 100 Hz 反馈。
`state.scene_reset` 为 `{id, state}`，state 取 `running/completed/failed`，完成含 models，失败含 error。
运行期间禁止 arm / 重复 reset_scene；hold、断线或反馈无效请求取消，未完成的服务返回前保持禁止 arm。
A 等待匹配 id 的完成状态，5 秒未完成则取消并报告可能部分还原；更旧 generation 的排队遥测不作失败依据。
Stop 不会撤销已经还原的物体。

基准为 B 初始化时的非 sawyer 模型世界坐标姿态；每个模型经 `/gazebo/set_model_state` 恢复姿态并清零
线/角速度。不调用 reset_world/reset_simulation，不修改机器人关节或仿真时钟。
基准模型缺失时在写入前拒绝；后来新增模型保留，不重建已删除模型，不声明恢复完整物理引擎状态。

### 可选夹爪控制

hello 的 `gripper_control=true` 表示 B 接受 target 中的 `gripper`：整数 -1 张开、+1 闭合。
A 仅在输入有效且已握持接管时发送，右扳机 `<0.3` 为 -1，`>=0.3` 为 +1。
夹爪与同一 target 共用 session、generation、seq 和反馈有效期；不单独缓存或重放。
Gazebo 首版对接 `generic_left/right_finger_controller`，每指张开目标 0.04 m、闭合 0 m；
相同开合值不重复提交。暂停时依新鲜反馈保持当前位置，不自动张开。
submitted 回执增加 `gripper`；sample 的 `gripper` 含实际指位、有效性、目标与执行状态。
无该能力的真机 B 仍只控制手臂；旧 A 的 gripper_observation 始终只显示，不隐式升级为控制。

### 可选双端关节跳变保护

`hello.joint_protection={schema:1,max_step_rad:0.35,neutral_reset:true}`。
新版 Operator 每次 arm 显式携带 `joint_protection={enabled:true|false,max_step_rad:0.35}`；
B 严格验证字段/类型/阈值并在 arm reply 与 state 回显。A 缺能力或回显不一致时拒绝运动。
未协商扩展的旧客户端保留旧语义；UI 缺省开启，显式关闭记入配置/录制。

A/B 的 IK result 相对该请求 seed、接收目标相对上次通过目标逐关节检查，
B 提交前对上次实际提交值复查。阈值固定 0.35 rad，不角度包裹、不裁剪、不因低频放大。
超限 B 以 `joint_jump` hold，pause_details 包含 stage/joint/target_rad/reference_rad/delta_rad/max_step_rad。
无效 generation/期限的 IK 先丢弃，不能用迟到的跳变结果停止新代次。

`arm.motion_purpose` 缺省 teleop；只有 `neutral_reset` 可豁免远距离目标的输入跳变检查，
且必须启用 cubic 轨迹、目标逐轴严格等于 B 的官方 neutral。所有实际插值样本仍检查。
普通 STOP/hold 不受跳变门阻挡。原有时间预算、速率/加速度和跟随误差保持不变。
完整边界与录制筛查见 [关节保护](../JOINT_PROTECTION.md)。

### 可选实时关节轨迹

支持的 B 在 hello 中添加 `joint_trajectory`，包含 `algorithm`、`control_hz`、
`per_sample_receipts=true`。新版 A 默认在 arm 中附带 `trajectory` 配置；
能力缺失时明确拒绝进入该模式。未附带配置的旧客户端 / 策略环境仍走直传。

配置包括 `enabled`、七维 `max_velocity_rad_s` / `max_acceleration_rad_s2`、
`min_duration_ms` 和 `max_following_error_rad`。B 检查配置，不接受未知项和无效数值。
轨迹生成保持位置、速度连续并限制整段加速度；不保证跃度连续或末端笛卡尔速度上限。

同一个 target 可以产生多个 submitted 回执：

- `seq` 仍是源 target 序号；新增 `execution_seq` 是本连接递增的实际执行样本编号。
- `q` 是此次真实 publish 的插值点；`trajectory.goal_q` 是源 IK 目标。
- `trajectory.dq/ddq` 是该时刻参考曲线导数；`sample_b_ns` 是采样时刻，
  `b_ns` 仍为 publish 返回时刻，二者不表示物理到位。
- A 用 execution_seq 作为 HDF5 动作序号，保留 seq 作为事件 source_command_seq。
  客户端不可收到第一条回执就丢弃源目标上下文。

B 每周期仅采样当前曲线，新目标替换曲线，不排队。只有外部新目标能更新命令看门狗；
每个插值点都重新验证该目标引用的反馈，默认超过 120 ms 即停止，即使当前反馈还新鲜。
调度间隔超过反馈时限也取消，避免卡顿后跳到曲线远处。
hold / 断连 / 故障立即清空曲线，按新鲜反馈保持，不等待旧轨迹减速或到达终点。

arm/hold、失联、反馈失效、指令超时都会使旧 generation 无效。
断线后新 session，不允许旧连接中的目标在新会话执行。A 的握持状态机还要求松开再握持。

### 可选原生 Intera Cartesian

hello 的 `intera_cartesian` 提供 `available`、`schema=1`、`experimental=true`、B 默认 `options`、`send_hz_choices=[10,20,50,100]` 和 `command_semantics`（choices 还受 B control_hz 限制）；
不可用时提供 reason。A 需同时确认 backend=intera 和此能力，不能给旧 B 发送原生 arm。
显式 `arm.execution_mode="intera_cartesian"` 与 `trajectory` 互斥；省略 execution_mode 维持旧 joint 语义。
新版 A 对四档使用可选 `arm.cartesian_send_hz`；B 只接受上述选择，且不能超过 control_hz。
字段与 joint 模式互斥；旧 A 省略时沿用 B 默认 send_hz。B 每次接管重新选择，不修改 ROS 默认配置。
A 在旧 B 不宣告 choices 时禁用四档，避免旧 B 静默忽略新字段；Standard 旧协议不受影响。
B 独立验证服务能力，原生模式仍拒绝本地 IK 的 model_sha256，保留远端 IK 和现有 target 格式。

B 只保留一个在途 FK 和一个最新待处理槽；新目标覆盖待处理槽，仍有效的在途 FK 可完成提交，避免高频输入使发送饥饿。准备结果不是 submitted。准备完成后重验 session/generation/原引用与期限，
由控制线程调用 `MotionTrajectory.send_trajectory(wait_for_result=False)`，只在提交后发 receipt。
提前一轮准备，按目标周期就近量化到控制轮次（半轮容差，100 Hz 时为 5 ms）；错过的周期跳过，不补发。
频率表示目标节拍，单次提交间隔可因量化/抖动而短于或长于 1/hz，实际值必须从回执计算。
原生 receipt 保留源 seq 与 q（目标关节零空间偏好），附 `native_cartesian`（command_kind、goal_q、fk_ms、source_sample_ns、
send_hz、submit_interval_ms、reference_age_ms、status_before_send），
fk_ms 表示准备开始至提交的耗时，包含控制环轮询等待，并非纯 FK 服务耗时。
send_hz 为本次请求频率；submit_interval_ms 为 B 实际两次提交完成时刻之差，首条为 null；
reference_age_ms 为原引用在提交完成时的年龄。status_before_send 仅是提交前 SDK 跟踪的最新 action 状态，
不代表全体被替换 goal 的结果审计，更不证明速度连续。
不提供 execution_seq 或伪造 trajectory.dq/ddq；这些 q 不是底层关节执行参考，不代表到位。
A 在 manifest 写明 `command_semantics="cartesian_goal_joint_bias"` 和原生选项，并保留实际反馈。

state 新增 execution_mode、native_send_hz、native_stopping、native_stop_failed。原生 hold 先返回 holding=false，
成功 STOP 结果且反馈有效后 state 才报告 holding=true；未确认期间或失败后拒绝所有 arm，防止切换绕过停止。
最新反馈失效也必须发 STOP；不发送旧关节位置代替原生取消。FK 线程没有发送权限，迟到结果不能越过停止/新 generation。

## 时间

Operator 类默认 50 Hz，当前 A 配置上限 120 Hz；B 调度/反馈 100 Hz。
B 默认最新反馈/原引用/命令期限为 100/120/200 ms；当前 Gazebo 实验值见下表。
100 Hz 是服务调度频率，不是对机器人原始反馈频率的保证。

A 通过 5 次 ping 的最小往返时间估计 B→A 单调时钟偏移和 RTT/2 不确定度，每 2 秒更新。
A 判断反馈年龄时加上该不确定度；10 秒无有效时钟更新视为过期。
这是网络往返模型下的保守检查，不能证明 Wi-Fi 硬实时；长时间掉线需重新连接。
命令引用由 B 发出的源样本 seq，B 使用自己的单调时钟判定，无需 A/B 墙钟完全一致。

ROS 的 source stamp 另行检查：与 B ROS 时钟差超过允许年龄、时间回退、重复旧时间戳均不能刷新样本。
反馈 `captured_ns` 包含 ROS 源数据在到达 B 之前的年龄；组合样本以最旧源判定新鲜度。
`available_ns` 表示组合样本在 B 可用的最晚到达时刻，用于训练的因果对齐。
ROS master/机器人原有时间同步需继续正常工作。

Quest 另有协议 3、设备 session / recenter / seq、设备 boottime。
A 用 `/proc/uptime` 的最小 RTT 估计偏移，计入 10 ms 量化误差；10 秒更新，
输入年龄上限 150 ms，30 秒无时钟更新视为过期。ADB stdout 使用独立读取线程，
避免 Windows 上 selectors 不支持子进程管道的问题。

## 停止与阻塞

反馈过期时 A 停发新目标。B 的看门狗独立于 IK：
指令停止超过实际 command_timeout_s（标准 200 ms、当前 Gazebo 250 ms），且本地反馈仍新鲜时，仅提交一次当前关节保持目标。
若反馈已经不可信，B 停止发布，不拿陈旧反馈生成保持位置。
该行为不能保证在任意故障下机械臂立即静止；原有硬件急停独立工作。

TCP 内核已经缓存的字节不能撤回，因此除 latest 槽外还必须有 seq/generation/反馈引用检查。
hold 进入可靠队列并清理 A 尚未发送的 target 槽；已在传输中的帧仍受 B 检查。
IK 只有一个正在执行的后端求解，至多两条 IK 服务连接；超时结果不用于运动，
阻塞的 ROS 服务不会堵住控制/反馈线程。服务一直不返回时，后续 IK 报 busy，需恢复 B 环境。

B 状态事件只保留最近 64 条；A 把录制期间收到的事件写入该段数据。
B 桥接 ROS 日志放在 `/dev/shm`，退出清理；不改动实验室其他 ROS 节点的日志设置。

## A 本地 IK 模型协商（源码开发版）

协议 version 1 保持兼容，新增可选字段，不改变 target / submitted / HDF5 动作语义。

- `hello.local_ik_targets: true`：B 支持绑定模型的本地 IK 会话。Intera 真机目前为 false，附 `local_ik_unavailable_reason`；没有等效自碰撞预检前，B 拒绝携带模型绑定的本地 arm。模型仍可导出用于只读对比，远端路径保留。
- `hello.kinematics`：启动时从实际 `/robot_description` 提取的 base → 当前 tip 工具链；含 `available`、`schema: 1`、`base_frame`、`tip_frame`、固定关节与七轴的 origin / axis / lower / upper、固定关节顺序、规范化内容 `sha256` 和原 URDF `urdf_sha256`。模型不完整时返回 unavailable 与原因；不从硬编码模型补缺，也不传网格或插件。
- 本地 A 在连接及每次 arm 前以关节反馈计算 FK，与 B 独立末端反馈核对。容许异步采样的差异为 10 mm / 0.03 rad；这不是求解精度或过期时间，也不能替代真机标定验收。
- 本地 `arm` 携带 `model_sha256`；B 不匹配则拒绝。该代次的目标和每个插值参考都由 B 检查模型关节限位，越界取消轨迹，不裁剪、不放宽。
- 本地 A 只连接控制端口；不创建 IK socket，不发 `solve`。A 单工作线程，前一解作种子；种子越界、不可达、连续解失败或预算耗尽均暂停，不自动切远端。模型在 B 启动时固定，更换模型/工具后重启 B 并重新连接、校准 A。
- 远端回退仍使用原 IK 端口与 `solve` 请求。旧 A 不发送模型 hash 时保留原有协议行为。
- 100 ms 最新反馈、120 ms 原反馈引用、200 ms 无命令看门狗及停止 / generation 检查不变；本地目标沿用发起求解时那条反馈引用，不能用更新反馈替旧结果续命。

## 当前仿真期限与诊断

| 条件 | 当前值 | 来源 |
| --- | --- | --- |
| A 最新观测 | 250 ms | feedback_profile=gazebo_250ms，仅双重确认 Gazebo 后 |
| B 原引用 / 命令 | 250 / 250 ms | config/gazebo-reference-timeout-ms 与实际 hello |
| B 最新源 / 控制间隔 | 100 / 100 ms | B 原保护 |
| Quest 输入 / 本地 IK / 任务 | 150 / 20 / 100 ms | A 原保护 |

A 的反馈档不作为 wire 参数改变 B；旧 110/150 ms 兼容档仍在代码内，UI 不再提供下拉框。
150/250 档还要求 B 的原引用至少 180 ms。A 观测可用不等于原引用仍允许动作，B 在接收和每次提交均复验。
真机/fake 强制标准档，源时间、引用身份与 generation 不因参数选择重置。

a_frame_received_ns 是 A reader 接纳状态帧时的单调时刻；a_received_ns 保留快照观察含义，两者是 A 派生字段。
诊断分开 B 源年龄、传输/队列等待和 A 驻留，原引用预算固定于 IK 发起，不被后续新反馈或时钟更新续期。
