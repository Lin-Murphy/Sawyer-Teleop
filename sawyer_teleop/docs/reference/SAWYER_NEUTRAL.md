# 实机 Sawyer：查看与回到官方中立姿态

在**连接真实 Sawyer 的 B 电脑**上操作，先加载实验室已验证可用的 ROS / Intera 环境。
下列命令适用于项目使用的 ROS Noetic / Python 3 环境，不使用 A 端运行包的 Python。

## 1. 查看官方中立姿态（只读，不运动）

```bash
rosparam get /named_poses/right/poses/neutral
```

输出为当前机器人 ROS 环境配置的中立关节角列表，单位为弧度。
SDK 按 `Limb("right").joint_names()` 的顺序配对，Sawyer 通常为 `right_j0` 至 `right_j6`。
它不是末端 XYZ，也不是当前实测关节角；这里读取现有配置，不写入自定义值。
若提示参数不存在，先检查机器人连接与 ROS 环境，不要自行填一组角度代替。

## 2. 回到官方中立姿态（会使真实机械臂运动）

先保存或丢弃 A 的录制，关闭 A，并停止真机 B 桥接及其他运动控制程序，保留机器人 ROS 环境。
按实验室流程使能机器人，确认回位路径无障碍、急停可用；此动作不做环境避障规划。

```bash
ROS_NAMESPACE=/ python3 -c 'import sys, rospy, intera_interface; rospy.init_node("sawyer_return_to_neutral", anonymous=True); enabled = intera_interface.RobotEnable().state().enabled; enabled or sys.exit("Robot is disabled; enable it using the lab procedure first."); limb = intera_interface.Limb("right"); limb.move_to_neutral(timeout=30.0, speed=0.3)'
```

命令调用官方 `move_to_neutral()`，读取同一个中立姿态参数；不修改中立姿态，也不自动使能机器人。
`speed=0.3` 为 SDK 默认的最大关节速度比例，`timeout=30.0` 为最长等待秒数，不保证恰好 30 秒完成。
若出现超时、错误或急停，不能将命令退出当作已到位；确认实际姿态和反馈后再继续。
完成后重新启动真机 B、连接 A，按正常校准与松开/握持步骤接管。

## 核对依据

已核对项目随附的 `sawyer-lab-source-20261012/sawyer_ws/src/intera_sdk/intera_interface/src/intera_interface/limb.py` 中 `move_to_neutral()` 实现。
官方接口说明：[Intera Limb.move_to_neutral](https://rethinkrobotics.github.io/intera_sdk_docs/5.0.4/intera_interface/html/intera_interface.limb.Limb-class.html#move_to_neutral)。
本次仅新增文档并检查命令语法，未在真实机械臂上执行回位。
