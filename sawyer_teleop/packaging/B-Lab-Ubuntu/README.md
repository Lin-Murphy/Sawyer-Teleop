# 实验室实体 Sawyer B

把整个 `B-Lab-Ubuntu` 文件夹复制到实验室 Ubuntu 的一个新目录，保留旧运行版。
复用原有 Ubuntu 20.04 / Python 3.8 / ROS Noetic / Intera，不需要 Docker或仿真。可选外接夹爪需要另装 Dynamixel SDK，见部署包根目录的 `docs/DEPLOYMENT.md`。
需要一个已编译、包含 Intera SDK 的 catkin 工作空间（默认 `~/sawyer_ws`），不会修改它。

## 首次填写地址

编辑本目录 `config/real-b.env`，只需填写三项：

| 配置 | 填什么 |
| --- | --- |
| `SAWYER_ROBOT_HOST` | 机器人实际 IP 或主机名，不含 `http://` 或端口 |
| `SAWYER_ROS_IP` | B 电脑连接机器人那张网卡的 IPv4，机器人必须能回连 |
| `SAWYER_BIND_IP` | Windows A 能访问的 B 电脑 IPv4；可以与上一项相同 |

模板默认 `SAWYER_ROS_SETUP=/opt/ros/noetic/setup.bash`、`SAWYER_WORKSPACE=$HOME/sawyer_ws`，路径不同时一并修改。
地址不能使用 `127.0.0.1` 或 `0.0.0.0`。换成新副本时，把旧副本 `real-b.env` 的三项地址抄过来；不要照抄示例地址。

## 启动与退出

在本文件夹打开终端，先只检查本地依赖和配置（不访问机器人）：

```bash
bash Start-B.sh --check
```

现场按实验室流程完成机器人开机和 SDK 准备，再运行：

```bash
bash Start-B.sh
```

脚本依次：source 原有 ROS / 工作空间 → 拒绝仿真 master → 检查官方 IK 服务 →
把 `config/lab-neutral-pose.yaml` 加载为 `/named_poses/right/poses/neutral`（Reset 目标）→ 启动 B。
加载参数不会移动机械臂；脚本不会自动使能、回中、清除急停或启动 roscore。
按实验室流程使能，并保持唯一运动控制者。

保持终端打开。Windows A 选择 Real Sawyer，地址填 `SAWYER_BIND_IP`，放行 TCP 8765 / 8766。
结束时先让 A 保存、松手并 Disconnect，再在这个终端 Ctrl+C。

## 运行参数

| 项 | 值 |
| --- | --- |
| 控制 | 100 Hz，工具 `right_hand`；可选外接 Dynamixel 夹爪，默认禁用，配置见 docs/DEPLOYMENT.md |
| 最新反馈 / 源引用 / 命令期限 | 100 / 250 / 250 ms |
| IK | 官方远端服务；本地 IK 和 Gazebo 快速参数不用于实机 |
| 关节跳变保护 | A、B 各自独立检查，每关节 0.35 rad |
| Ruckig | `runtime-wheels/` 内固定的 Linux x86_64 / Python 3.8 版 0.12.2，必须随文件夹保留 |

默认使用系统 Python 3；已有专用解释器时在配置中填写 `SAWYER_B_PYTHON`，它必须能导入 ROS 消息与 Ruckig。

## 运动选项

在 A 的 Motion option 选择，默认 Standard。切换前松手、结束整批录制并等待停止确认，再 Apply。

| 选项 | 执行方式 |
| --- | --- |
| Standard | 远端 IK → B 100 Hz 关节轨迹 → POSITION_MODE |
| Fast follow | 同 Standard，关节加速度上限为其 2 倍；速度、期限和保护不变 |
| Intera Cartesian 10 / 20 / 50 / 100 Hz | 远端 IK 后由 B 做 SDK FK，提交原生 CARTESIAN action |
| Direct pose 50 Hz（实验） | A 直接发送末端位姿，B 以不短于 20 ms 的间隔提交原生 action |

原生选项需要 `intera_motion_interface` 和 `/motion/motion_command` 的 goal/cancel/status/feedback/result；
缺少时 B 拒绝该选项，Standard 仍可用。原生 STOP 要等结果确认；超时或失败会锁定 B，
必须现场检查后重启 B，不能靠重连 A 或切换选项绕过。A 与 B 应来自同一份源码；旧 B 缺少新能力时 A 会提示升级。

## 实体反馈兼容

`/robot/limb/right/endpoint_state` 按 Intera 底座坐标约定读取，兼容 `frame_id` 为空、`base` 或 `/base`；
其他明确坐标系仍拒收。位姿有效标志、数值及 100 ms 源时间检查保持启用。

`/io/robot/cuff/state` 按 SDK 的 IO 状态缓存处理，消息创建时间不作为连续心跳。必须收到有效 `right_cuff`
状态且发布者保持连接；断线会清空缓存，重新收到消息才恢复。cuff 按下、异常状态、未来或倒退时间戳仍阻止控制。
关节、位姿、机器人状态和碰撞反馈各自执行原有时效检查。

## 诊断

B 默认把有界反馈诊断写到 `${XDG_STATE_HOME:-$HOME/.local/state}/sawyer-teleop/feedback.jsonl`，
启动时打印实际路径。每 0.5 秒记录一次缓存反馈：实际 q/dq/位姿、急停枚举、反馈失效原因和暂停上下文；
每次成功 arm 另记 `control_configuration`（执行模式、参数及 SHA-256）。每份约 5 MiB，保留三份备份。
出现问题先复制 `feedback.jsonl*` 和终端原始输出，避免滚动淘汰。

只读 IK 延迟排查（不 arm、不发送运动），在已 source ROS 环境的终端运行：

```bash
PYTHONPATH=src python3 scripts/probe_intera_ik.py --samples 30
```

## 验收状态

`--check` 通过不代表网络、机器人或运动已验收。`DEPLOYMENT-MANIFEST.json` 记录逐文件 SHA256 和
`release_accepted=false`；实际跟随、停止和原生 action 衔接须在现场授权后验证。
