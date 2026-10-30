# 数据训练与策略适配边界

训练环境与 A 遥操环境分开，不为训练升级 A 的固定依赖，B 不安装训练软件。
先按[使用指南](../USAGE.md)导出数据，格式与因果时间规则见 [DATASET](DATASET.md)。

## 当前目录实际提供什么

| 能力 | 入口 / 边界 |
| --- | --- |
| 导出 robomimic HDF5 | `sawyer_teleop.cli export`，七维绝对关节动作的归一化编码 |
| 低维 Gazebo 策略环境 | `policy_env.JointTargetEnvironment` |
| robomimic 0.3 适配 | `robomimic_env.EnvSawyerGazebo`，显式 register()，普通 A/B 不依赖它 |
| 一键训练/训练容器脚本 | 当前副本未包含；旧说明不能当成可执行入口 |

旧文档提到的 training/Dockerfile、robomimic_smoke.py、make_bc_config.py、train_sim_bc.py
不在当前目录。历史训练环境/结果见归档；本轮不补造脚本或宣称已重新训练。

## 数据要求

同一个原始 episode 不跨训练/验证集合。保持数据的 joint_names、动作 center/scale 和来源标记，
不能换编码后继续使用旧模型。只有一个 episode 时验证集为空，不能据此报告泛化效果。
任务成功由任务定义或人工标注，不从保存完成、末帧 dones 或训练 loss 推断。
fake/合成数据只作管线自检；真实、仿真数据分别标识。

## 在线适配

环境只提供 joint_pos/joint_vel/eef_pos/eef_quat 和七维关节目标；不提供 RGB、奖励或任务成功判定。
step 解码动作、等待提交，再取后续反馈。reset 回到示范参考关节姿态；get_state/reset_to 明确不支持完整世界状态恢复。
UI 的 Reset scene 不是策略环境的任意物理状态序列化接口。
该环境拒绝真实 Intera，真机策略部署未实现验收。

原接口参考：[robomimic 环境说明](https://robomimic.github.io/docs/modules/environments.html)。
当前适配针对已核对的 0.3 行为，不默认后续版本兼容。
