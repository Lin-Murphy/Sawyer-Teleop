# 数据与时间语义

每段录制一个目录，所有文件都在 A。相机抓取、网络读取、HDF5/视频写盘各在线程中运行。
单一写盘线程拥有 HDF5 与 MP4；队列有界，满了就报告录制失败并暂停遥操，不静默丢帧继续训练。
默认队列最多 2,048 条混合状态、命令、图像及日志事件，排队的 RGB 图像另有 128 MiB 上限。

## 导出训练文件

在已安装依赖的环境（Windows 用 `windows-runtime\python\python.exe`）：

```bash
python -m sawyer_teleop.cli export <录制目录> <输出.hdf5>
```

Windows 与其他入口统一见[使用指南](../USAGE.md)。输出已存在时拒绝覆盖。
fake / 合成自检数据不可作为真实任务示范；保存成功不自动代表任务成功。

## 原始数据 raw.hdf5

- `states/{ns,captured_ns,uncertainty_ns,source_seq,source_stamp,pose_source_stamp,q,dq,eef_pos,eef_quat}`
  保存 B 的实际反馈。`ns` 是组合观测在 B 可用的时刻映射到 A 单调时钟；
  `captured_ns` 是最旧源样本时刻，用于新鲜度检查。
- `commands/{ns,uncertainty_ns,q,seq,feedback_seq,generation,segment}`
  保存 B 回执确认已提交的 7 维绝对关节目标。仅发出但未提交的目标不写入动作数组。
- 夹爪扩展：`commands/gripper` 为 -1 张开 / +1 闭合，0 代表无夹爪指令，
  `commands/gripper_valid` 标识指令是否存在；`states/gripper_position_m` 保存两指实际位置，
  `states/gripper_valid` 标识位置是否可用（不可用时零值只是占位）。字段与各自的命令 / 状态逐行对齐。
  `submitted` 事件同步记录 `submitted_gripper`。旧数据没有这些字段仍可读取。
  当前 robomimic 导出及策略仍为七维关节动作，不包含夹爪任务训练。
- `frames/<相机名>/{ns,read_start_ns,seq,rgb}`：RGB uint8 NHWC。
  `ns` 是 A 收到一帧的时刻，**不是曝光时刻**。摄像头内部缓存和驱动延迟仍需现场测量。
- 根属性 `metadata`、`complete` 保存模型、坐标映射、事件、录制质量、停止结果。
- `events.jsonl` 包含提交动作对应的原始 Quest 输入、目标位姿与暂停原因。

`metadata.motion`，记录平滑开关、时间常数、算法与 IK 种子策略。
`submitted` 事件中的 `raw_target_pose` 是未经平滑的映射目标，`target_pose` 是送入 IK 的平滑目标，
`quest` 始终保留对应原始输入。`timing` 记录输入收取 / 映射时刻、IK 排队 / 开始 / 完成、
目标发送、B 提交映射时刻、回执收取、时钟不确定度、反馈年龄和 RTT。
`commands/q` 仍只保存 B 确认已提交的关节目标，训练动作会体现所选平滑方式；不以原始手柄意图覆盖实际动作。
字段为兼容新增，旧 HDF5 和 robomimic 导出流程继续可用。

实时轨迹模式新增 `metadata.trajectory`，保存关节速度、加速度和跟随误差配置。
B 的每个实际插值点均作为独立动作保存：`commands/seq` 使用执行样本编号，
事件 `source_command_seq` 保留原 A 目标序号，二者不再一一对应。
`submitted_q` 与 HDF5 `commands/q` 一致；`trajectory.goal_q` 是 IK 目标，
`trajectory.dq/ddq` 是 B 参考曲线速度 / 加速度，不是实测值。
原始手柄和 IK 时序仍属于该点对应的源目标；同源目标可以对应多个插值点。
普通遥操不写盘，显式录制结束后产生的点不补写进已结束的录制。

相机 MP4 用固定播放帧率方便检查；时序以 HDF5 每帧时间戳为准。
`complete=true` 表示写入没有报错，且采集已明确封闭并由用户保存（旧版也接受结束时保持目标确认）。
新版停止录制不要求停止遥操，结束后到保存前的动作不会写入本条数据。
这不代表任务成功，也不代表物理静止检测。
写盘失败、直接退出、失联且无法确认保持的片段保留为 incomplete，默认不导出。
突然断电留下的 `.pending_*` 目录也默认排除，不能仅通过改名当作完整数据。

## 运动诊断

只读取已保存数据，不连接 B、不执行机械臂动作。可以输入一个 episode、`raw.hdf5` 或整个批次目录。
普通遥操仍不写文件，需要诊断时主动录制；本功能不会另行偷偷录制。

```bash
python -m sawyer_teleop.cli analyze-motion <录制目录> --output <报告.json>
```

输出文件已存在会报错，不覆盖。报告包含已保存的命令 / 反馈间隔、每关节目标变化和斜率、
IK 各环节耗时、输入到 B 提交的估计时延、原始 / 平滑目标差异、是否为 fake 数据。
命令变化按抓握片段统计，不跨超过 100 ms 的缺口计算斜率。
目标变化不等同于机械臂实际抖动；反馈间隔是录制采样间隔，不等于机器人发布频率。
输入到提交是时钟对齐估计，需同时看不确定度，不包括机械臂到位耗时；旧数据缺少的时序项显示 `null`。

轨迹模式报告还包含 `planned_velocity_abs_rad_s`、`planned_acceleration_abs_rad_s2`、
源目标数量及 IK 目标到提交点的偏差。planned 指参考曲线约束，不能替代实际关节反馈验收。

## 导出的 dataset.hdf5

```text
data/                        attrs: total, env_args, control_hz, action_normalization
  demo_0/                    attrs: num_samples, source_episode, truncated, task_success
    obs/
      joint_pos              N × 7, float32, rad
      joint_vel              N × 7, float32, rad/s
      eef_pos                N × 3, float32, m, base
      eef_quat               N × 4, float32, xyzw
      front_image            N × H × W × 3, uint8
    next_obs/                相同结构
    actions                  N × 7, float32, [-1,1]编码
    actions_raw              N × 7, 原始绝对关节目标, rad
    timestamps_ns            A 时钟下的均匀采样网格
    command_seq              该时刻选中的最新已提交目标
    rewards                  全0，未标注奖励
    dones                    末样本为1，只表示片段边界
mask/
  train                      demo 名称数组
  valid                      demo 名称数组
```

默认每 20 ms 一条训练样本。命令采用最近已提交目标的零阶保持；
观测选择网格时刻之前最新的反馈，且加入时钟不确定度后仍不得晚于网格时刻。
next_obs 选择下一网格时刻的过去观测。
图像采用过去最新帧，因此 30 FPS 图像在 50 Hz 数据中会重复，但不会偷用未来帧。
状态超过 100 ms、图像超过 250 ms、命令间隔超过 100 ms 时不跨越缺口拼接。
松手/重新抓握使用新 segment，进一步分割；不足两条的短片段不导出。

有时间戳回退则报错，不排序或悄悄修复。新输出名用于保留旧数据。
train/valid 以原始 episode 为单位，固定随机种子 42，默认约 20% 验证数据；
只有一个 episode 时 valid 为空，训练配置会关闭验证。

## 动作编码

原始控制和 raw 数据始终用弧度，不对遥操目标限幅。
导出时，对所选完整 episode 的全部目标计算每关节 min/max：

```text
center = (min + max) / 2
scale  = max((max - min) / 2, 1e-6)
actions = (actions_raw - center) / scale
actions_raw = actions * scale + center
```

同一导出文件所有 demo 共用一套编码，参数写在 `data.attrs.action_normalization`。
这是数据坐标编码，不是机器人安全关节范围，也不是仅从训练集学习的统计量；
编码使用了所选训练/验证数据的范围，这一点在评估时应披露。
后续合并数据、继续训练和部署必须保留模型所用编码，不能重新计算后混用旧模型。

`env_args` 标记离线 Sawyer 数据；`rollout.enabled=false`。
尚无可以把该元数据直接转换为真实 Sawyer 在线 rollout 的 robomimic 环境实现。
项目已提供独立 Gazebo 低维策略适配；后续训练入口需将环境元数据和此文件的动作编码一起写进 checkpoint。
原始 HDF5 的离线标记继续保留，不会自动连接真机。

格式参考 [robomimic 数据说明](https://robomimic.github.io/docs/datasets/overview.html)；
兼容验证使用 PyPI 发布版 0.3.0，未把文档中的 0.5 版本误当作已发布安装包。

## 来源与在线观测

新导出文件增加 `data.attrs.source_backends` 与 `joint_names`，明确区分 fake、Gazebo 与真机。
策略环境保留四个低维观测键及七维动作编码，不重新估计 center / scale；
自定义环境类型为 1001，名称为 `SawyerGazeboJointTarget`。当前副本没有训练脚本，见 [TRAINING](TRAINING.md)。

在线环境使用同一个 B 反馈生成 float32 的 `joint_pos/joint_vel/eef_pos/eef_quat`，四元数为 xyzw。
`step` 解码绝对关节目标、确认 B 提交，再读取其后的新反馈。时序仍以单调墙钟调度，
Gazebo 源时间另行校验；暂停或回退不能刷新旧数据。
训练 / 验证片段边界仍不等于任务成功。首版不提供 RGB 在线观测、物体状态或完整物理状态恢复。
可用入口与限制见[训练边界](TRAINING.md)。

## 真实入口与握持锚点

仅在显式录制期间保存：quest_capture_started/ended、quest_ingress、quest_clock_sync 和 engaged。
每条入口保留 ingress_seq、read_completed_ns、received_ns、mapped_ns、uncertainty_ns、clock_revision、
原始 packet 与验证结果；重复、无效或损坏输入不静默删掉。旧缓存不套用新的时钟映射。
engaged 保存真正消费的输入、arm 后选中的反馈、robot_origin、vr_origin、mapping、segment 和 generation。
从握持中途开始录制时记录 active_engagement。旧数据没有这些字段则明确缺失，不补造。

manifest 与 HDF5 metadata 带 motion_profile/status 和完整 trajectory 参数；录制期间不能切换选项。
Activity 面板的过滤、暂停和三位小数仅作用于显示，不改变此处原始数据。

## 失败文件与导出

录制资源关闭失败时停止等待并报告错误。选择保留的数据留在 `.pending_*`，manifest 记录 `recording_error`，
不晋升为 `episode_*`、不计入保存成功数量，也不参与批次导出；不要手动改名来绕过失败标记。
导出使用独占的随机临时文件，完成并刷盘后在同目录以硬链接发布；输出已存在则失败，不覆盖其他进程结果。
原有 `.partial` 和用户原始数据保持不动。目标文件系统需支持硬链接；不支持时明确失败，不降级为可能覆盖的操作。
