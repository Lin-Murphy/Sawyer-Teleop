# 排障

## 先看哪里

| 信息 | 位置 | 保存规则 |
| --- | --- | --- |
| 操作事件、警告、错误 | A 网页底部 Activity | 内存最多 600 条，不写盘 |
| 最近一次暂停的完整诊断 | A 网页 Last pause details | 内存，只保留最近一次（重复失败不会覆盖首次原因） |
| A 启动失败 | `windows-local/logs/windows-launch.log` 和启动窗口 | 追加 |
| B 当前状态 | `scripts/monitor-sim.sh`（仿真）/ `cli monitor`（实机） | `/dev/shm` 单份快照，只读 |
| B 持久反馈与实际方案 | `feedback.jsonl`（实机 B 默认开启） | 约 5 MiB × 4 份滚动 |
| 完整输入、动作、反馈、时序 | 录制目录的 `events.jsonl` / `raw.hdf5` | 只有 Start Recording 才写 |

出问题先保存：启动窗口原始输出、Last pause details 的 JSON、B 的 `feedback.jsonl*`（会滚动淘汰）。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `Python runtime not found` | 先运行 `Setup.cmd` |
| `Simulation image download failed` | 检查网络后重试；或手动下载镜像放到 `gazebo-image/`，见 [SETUP](SETUP.md#windows-本机仿真-b) |
| `simulation archive checksum mismatch` | 压缩包损坏或不是固定版本：删掉 `gazebo-image/*.tar.gz` 后重新运行 `Start-B.cmd` |
| `Simulation image missing`（Ubuntu） | 按 [SETUP](SETUP.md#ubuntu-仿真-b) 下载并 `docker load` |
| `Simulation image differs from the verified image` | 本地同名镜像不是固定版本；删除该镜像后重新导入 |
| `Docker Desktop Linux engine is unavailable` | 打开 Docker Desktop，切到 Linux containers |
| `WSLg display is unavailable` | 升级 WSL（`wsl --update`），或改用 `Start-B.cmd --headless` |
| GPU 渲染失败 | 自动退回 CPU；可加 `--rendering software` 并缩小 Gazebo 窗口 |
| `Existing simulation keeps its reference timeout` | 已有实例的期限与请求不同：Disconnect A，`Start-B.cmd --stop` 后重启 |
| 页面提示未授权 | 用 Start-A 窗口打印的完整 URL 打开 |
| `Quest packet truncated at 1023 bytes` 或 `char 1025` 的 JSON 错误 | 头显 APK 太旧：关闭 A，运行 `Setup.cmd quest`，确认 versionCode 9 |
| adb 显示 `unauthorized` | 在头显里允许 USB 调试；USB 在线不等于右手柄被追踪 |
| `ADB latency too high to verify input age` | USB 时钟同步往返太慢；换 USB 口 / 线，关闭其他 adb 程序 |
| 头显 HUD 未达 120 Hz | 系统降频；A 会如实显示，不要拿配置值代替实测 |

## 暂停原因

| 原因 | 先看什么 |
| --- | --- |
| `released` / `Grip released; …` | 正常松手，不是故障 |
| `controller_tracking_lost` / `quest_not_focused` | 右手柄追踪、头显佩戴、输入 App 是否在前台 |
| `quest_stale` / `motion_input_gap` | USB 与包时刻：`input`、`quest_transport` |
| `ik_input_expired` | IK 用到的输入已过期：`ik_input.age_ms` 与 IK 各段耗时 |
| `local_ik_no_continuous_solution` | 当时目标、种子、关节限位；不要直接归因于"加速度不够" |
| `feedback_*` | 反馈年龄与期限、B 源年龄、递送上界、原引用剩余寿命（不能只看 RTT） |
| `joint_jump` | 哪个阶段、哪个关节、差值多少，见 [JOINT_PROTECTION](JOINT_PROTECTION.md) |
| `command_timeout` | A 在命令期限内没有发新目标 |
| `trajectory_following_error` / `trajectory_control_gap` | 实测与参考之差、B 调度间隔 |
| `trajectory_unfeasible` / `joint_model_limit_exceeded` | 目标超出运动或模型限位 |
| `*:intera_cartesian_stop_failed` | 原生 STOP 未确认，B 已锁定，现场检查后重启 B |
| `recording_queue_full` / 保存错误 | 写盘或相机负载；数据会明确标记为不完整 |

B 发起的暂停会在提示里带上具体数值，例如
`B paused: Latest robot feedback expired (112.000 ms > 100.000 ms). Release grip, then squeeze again.`

## Last pause details 字段

这个 JSON 由 A 在暂停瞬间拼出（`operator._pause`），所有时间单位为 ms，`*_ns` 为 A 的单调时钟纳秒。

**操作端**

| 字段 | 含义 |
| --- | --- |
| `reason` | 暂停原因码（同上表） |
| `operator_interval_ms` | Operator 控制循环最近一次间隔，看 A 是否卡顿 |
| `arm_wait_ms` | 从握紧到 B 确认 arm 的等待时间 |
| `input` | 暂停时手柄输入：`age_ms` 年龄、`uncertainty_ms` 时钟不确定度、`expired` / `future` 判定、`ingress_seq` 包序号、`epoch` 参考系代次、`valid` / `reason` |
| `ik_input` | 正在执行的 IK 所用的那帧输入（字段同上）；与 `input` 对比可判断是 IK 慢还是输入断了 |
| `ik_task_ms` | IK 任务从提交到现在的总时长 |
| `ik_queue_wait_ms` / `ik_call_ms` / `ik_result_wait_ms` | IK 排队、求解、结果等待派发三段耗时；没有在途 IK 时为 null |

**`quest_transport`（Quest 传输）**

| 字段 | 含义 |
| --- | --- |
| `clock` | 当前时钟映射：`revision` 同步代次、`offset_ns`、`uncertainty_ns`、`sync_at_ns` |
| `last_sync` | 最近一次 adb 时钟同步：每次往返 `rtt_ms`、`best_rtt_ms`、`method`、是否 `accepted` |
| `sync_error` / `stream_error` | 同步或 logcat 流的错误 |
| `sync_age_ms` | 距上次同步的时间（超过 30 s 输入判为失效） |
| `ingress_seq` / `packet_seq` | A 已收到的最新包序号 / Quest 包内序号；与 `input.ingress_seq` 比较可看是否积压 |
| `source_to_read_ms` / `read_to_poll_ms` | 头显采样→A 读到、A 读到→控制循环取用 |

**反馈（仅暂停前正在跟随时出现）**

| 字段 | 含义 |
| --- | --- |
| `connected` / `fault` / `fresh` / `age_ms` | 与 B 的连接、反馈失效原因、是否新鲜、最新反馈年龄 |
| `a_limit_ms` / `b_latest_limit_ms` / `b_reference_limit_ms` | A 观测期限、B 最新反馈期限、B 原引用期限 |
| `b_limits_reported` | B 是否在握手中报告了上面两个 B 期限（否则用默认值） |
| `source_valid` / `source_fault` / `b_reason` | B 侧反馈源是否有效、原因 |
| `b_source_age_ms` | B 发送时，反馈源样本已有多旧 |
| `delivery_upper_ms` | B 发出→A 收到的上界（含 B 排队、网络、A 接收，并加上时钟不确定度） |
| `a_residence_ms` / `arrival_gap_ms` | 反馈在 A 已停留多久 / 最近两帧到达间隔 |
| `clock_uncertainty_ms` / `clock_sync_age_ms` | A↔B 时钟映射的不确定度与年龄 |
| `reference_seq` / `reference_remaining_ms` | 当前目标引用的反馈序号及其剩余寿命 |
| `budget_valid` | 以上时间预算是否可信（已连接、10 s 内同步过、年龄非负） |

B 侧自己的暂停上下文（`pause_details`：最新 / 原引用反馈的序号、年龄、故障、期限，命令年龄，控制间隔等）
写入 B 的 `feedback.jsonl` 与录制事件，并用于生成上面带数值的提示文字。

## B 反馈诊断日志（实机）

实机 B 默认写 `${XDG_STATE_HOME:-$HOME/.local/state}/sawyer-teleop/feedback.jsonl`，启动时打印实际路径。
每 0.5 秒一份缓存反馈（q / dq / 位姿、急停、反馈失效原因、运行 / STOP 状态、最近命令）加新增事件；
每次成功 arm 另记 `control_configuration` 与 `last_arm_configuration`（执行模式、轨迹参数或 Cartesian 频率、保护开关、期限和参数哈希），
可以据此确认当时实际用了哪套方案（见 [CURRENT_TELEOP_METHOD](CURRENT_TELEOP_METHOD.md)）。

这是有界排障记录：0.5 秒之间的短暂状态可能采不到，事件超过 B 的 64 项缓存可能丢失。
`b_ns` 是 B 单调时钟，不能与 UTC 或 ROS 时间直接混用。
直接运行 `python -m sawyer_teleop.cli bridge …` 时可用 `--feedback-log <路径>` 改位置或 `--no-feedback-log` 关闭；
fake / Gazebo 默认不写。写盘失败只在控制台报告，不影响控制。

实机状态快照（只读，不占控制连接）：

```bash
PYTHONPATH="$PWD/src" python3 -m sawyer_teleop.cli monitor \
  --status-file "/dev/shm/sawyer-teleop-$(id -u)/intera-b.json"   # 加 --once 只看一次
```

只读 IK 延迟探测（不 arm、不发运动，需已 source ROS）：`PYTHONPATH=src python3 scripts/probe_intera_ik.py --samples 30`；
加 `--connection per-call` 用改前的每次新建连接方式对照（同样只读）。
指标含义见 [B_MONITOR](reference/B_MONITOR.md)。
