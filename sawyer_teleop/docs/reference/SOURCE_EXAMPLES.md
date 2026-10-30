# 官方源码依据与取舍

这里汇总已经核对过的固定源码；逐文件 SHA、完整 27 个 SDK 示例清单和历次研究在
归档。当前实现/验收以 [PLAN](../PLAN.md) 为准。

| 来源 | 采用的思路 | 不作出的推断 |
| --- | --- | --- |
| Intera joint position / trajectory 示例 | 持续更新最新目标、当前反馈起点、明确取消 | 阻塞路点示例不能逐 VR 帧调用 |
| Intera IK 示例 | 种子、工具坐标与结果检查 | 不增加固定零空间目标或新复杂 IK |
| MotionTrajectory / MotionWaypoint | 原生轨迹调用与有限路点对照 | 异步返回不保证目标接替连续 |
| Gazebo 原镜像 | 现有 PID/动力学前馈，区分参考与执行 | 仿真模式不直接照搬到真机 |
| DROID / Polymetis | 持续目标更新，高层与底层伺服分离 | 没有移植 Franka 整套混合关节阻抗 |

## 官方 Cartesian

`go_to_cartesian_pose.py` 依赖 `/motion/motion_command` 的 `MotionCommandAction` 服务端。
当前 ROG Gazebo 的 goal/cancel/status/feedback/result 五个 action 接口均缺失，N1/N2/N3 未运行。
客户端 SDK 文件存在不能代替服务端；不创建同名自制服务冒充官方效果。

默认 `corner_distance=0` 过点速度为零；批量非零圆角可能连续，但使用了未来点且不严格过点。
`append_waypoint` 只修改本地轨迹，发送后追加不自动续接远端。在线最新点能否平顺接替必须实测。
只读探针为 `scripts/probe_intera_motion.py`，现场证据在 rog-native。

## DROID

已核对 DROID `33ae6a6` 与其 Fairo/Polymetis `0a01a7f`：15 Hz 高层、50 Hz VR、1 kHz Franka 底层。
`start_cartesian_impedance` 对应混合关节阻抗，不能把名字理解为本项目现有位置流的等价替换。
本项目最小阻抗对照使三个场景跟踪退步，因此未采用，也未继续扫增益。
固定源码与结论在 rog-droid-reference，失败在
rog-impedance。

## 原始来源

- [Intera Cartesian 示例](https://github.com/RethinkRobotics/intera_sdk/blob/master/intera_examples/scripts/go_to_cartesian_pose.py)
- [Intera SDK](https://github.com/RethinkRobotics/intera_sdk)
- [DROID](https://github.com/droid-dataset/droid)

这些链接用于查原实现；项目结论绑定本地已保存源码和当次实测，不默认上游新版本已验证。
