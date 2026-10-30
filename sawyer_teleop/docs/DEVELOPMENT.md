# 开发与验证

## 代码在哪

```text
sawyer_teleop/
  src/sawyer_teleop/   A/B 产品代码（职责见 ARCHITECTURE）
  src/.../web/         Web 操作台静态文件（无需前端构建）
  config/              operator.json 模板、实机 env 模板、实验室 Reset 姿态、Gazebo 期限档
  scripts/             启动器、仿真入口、导出、离线验证与只读探针
  scripts/windows/     run-python.cmd（内部）、bootstrap.ps1（Setup 下载运行环境）
  scripts/ubuntu/      Start-A / Start-B-Sim / Start-B-Real
  packaging/           B-Lab-Ubuntu 导出模板
  runtime-wheels/      Linux x86_64 / Python 3.8 的固定 Ruckig（仿真容器与实机 B 使用）
  quest/               Quest 输入 App 源码、当前 APK 与重建材料
  tests/               pytest；tests/fixtures 为固定回归数据
```

运行时的本机状态不进源码：Windows 在 `../windows-local/`（`config/`、`data/`、`logs/`、`artifacts/`），
Ubuntu A 在 `../ubuntu-local/`。首次启动 A 从 `config/operator.json` 复制一份本机配置，之后不再覆盖。

VS Code 打开 `sawyer_teleop/` 即可，`.vscode/settings.json` 已指向 `../windows-runtime/python/python.exe`。

## 测试

| 命令 | 内容 |
| --- | --- |
| `Test.cmd` | 全套 pytest，报告另存到 `windows-local/artifacts/software-tests/*.xml` |
| `Test.cmd ik` | 本地 IK 离线检查 |
| `Test.cmd lab-scene` | 固定实验室录制全部 7,177 条输入的离线回放参考门 |
| `Test.cmd feedback-delay` | 反馈期限离线对照 |
| `Test.cmd env` | Python、数值库、HDF5、视频编码、adb 和合成录制自检 |

`Test.cmd` 之后的额外参数会传给 pytest，例如 `Test.cmd all -k quest`。
Linux / Ubuntu：

```bash
cd sawyer_teleop
OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -p no:cacheprovider
```

Ubuntu 专属的 shell 启动器测试（`test_launchers.py`）在 Windows 上自动跳过；ROS、Docker、Quest 均用替身，不接触硬件。
仿真容器内的自检：`scripts/validate_sim_motion.py`（需在运行中的仿真里执行）。

## 按改动范围验证

| 改动 | 必要检查 |
| --- | --- |
| 文档 / 目录 | 本地链接、文中入口与实际文件一致 |
| 网页 / 日志 | 相关 Web 用例 + 浏览器实际查看；确认不改变控制和录制 |
| A 输入 / 录制 | 队列、时间身份、完整保存、协议集成 |
| 运动 / 保护 / 模型 | 全套软件 + 固定录制 + 隔离的物理仿真 / 完整 VR 链路 |
| A/B 协议或场景 | 两端兼容、取消 / 断线、实际后端验证 |

**每次运动相关改动必须包含 `tests/fixtures/lab_scene_20261022` 的全部 7,177 条输入**，保留失败 / 无提交段；
旧的横移 / 抬手（`sideways_lift_1_2_11`）和停手场景继续必过。
软件、替身或合成输入的结果不能当作物理或真机验收。

## 发布门

[固定指标与完整输入](reference/ROG_TRACKING_EXPERIMENT.md) 是发布依据，不在整理时放宽。

- 原始目标、B 参考、实际反馈分开评估；不移时、不减速、不使用未来输入，不删点。
- 停手窗口、松手取消、无非预期暂停、动作回执与数据完整均需验证。
- 完整场景三次重复与十次耐久尚未全部完成；Gazebo 结果不推定真机表现。

当前状态与未完成项见 [PLAN](PLAN.md)。

## 实验记录

每个实质实验留一个目录 `windows-local/artifacts/<run-id>/`（Ubuntu 自行选择同类位置），包含：
唯一变化、源码 / 配置 SHA、输入、环境、原始输出、失败、对照结果。原数据只读，派生结果另存。
PLAN 只写结论与下一步，不复制完整输出。实验产物不入 git。

## 导出实验室 B

```bat
sawyer_teleop\scripts\windows\run-python.cmd -I scripts\export_deployment.py --output ..\deploy-next
```

只生成 `B-Lab-Ubuntu/`，输出目录必须不存在（不会覆盖旧部署和现场配置）。生成内容来自
`export_deployment.py` 的 `B_FILES` 和 `packaging/B-Lab-Ubuntu/`，附 `DEPLOYMENT-MANIFEST.json`（逐文件 SHA256、`release_accepted=false`）。
导出不等于发布验收。只改 A（网页、Quest、录制）时不需要重新导出。

## Quest APK

当前包与 SHA256 记录在 `quest/apk.json`。重建（需要本地 Android 构建镜像）：`bash scripts/build_quest_120.sh`，
会校验固定 SDK 文件、编译并更新唯一默认包及其 SHA256。签名、源码清单见 [quest/README.md](../quest/README.md)。
APK 只影响 A，B 无需更新。

## 改动约定

- 每次改动写明影响范围：“仅更新 A”“仅更新 B（A 兼容）”或“A、B 都需更新”，以及是否需要重启 / 重新校准。
- A 负责输入、映射、一个滤波器、异步 IK、界面和录制；B 负责模型、目标验证、100 Hz 轨迹、执行与反馈。
  改协议、参数语义或执行逻辑必须检查两端。
- 一个问题、一个假设、一项改动；保留失败和淘汰理由。不为代码形式新增复杂 IK、预测或控制层。
- 最新目标覆盖，不排队追赶；松手优先取消；过期的输入 / IK / 反馈引用不得续期。
- 本地 IK 只用于 Gazebo；真机 / fake 用远端 IK。真机不继承仿真的快速参数或放宽期限。
- 跨线程边界保留时效复验；“代码重复”不意味着可以删检查点。
