# AI 助手工作约定

本文件给 Codex / Claude 等 AI 助手使用；人类读者请看 [README](README.md)。

## 先读什么

1. [README](README.md)：项目是什么、目录、入口。
2. [docs/PLAN.md](sawyer_teleop/docs/PLAN.md)：当前状态与最新决定（**唯一进度入口**）。
3. [ARCHITECTURE](sawyer_teleop/docs/ARCHITECTURE.md)：职责、线程、代码地图；接口看 [PROTOCOL](sawyer_teleop/docs/reference/PROTOCOL.md)。
4. 操作相关看 [SETUP](sawyer_teleop/docs/SETUP.md) / [USAGE](sawyer_teleop/docs/USAGE.md)，排障看 [TROUBLESHOOTING](sawyer_teleop/docs/TROUBLESHOOTING.md)。

## 仓库布局

- 代码根是 `sawyer_teleop/`（`pyproject.toml` 所在），产品代码在 `src/sawyer_teleop/`，测试在 `tests/`。
- 根目录五个 `.cmd`（含 2026-10-30 按用户要求加回的 `Stop-B.cmd`）是 Windows 唯一对外入口，都经 `sawyer_teleop/scripts/windows/run-python.cmd` 调用
  `windows_dev.py` / `windows_launcher.py` / `windows_sim_launcher.py`。`.cmd` 文件保持 ASCII + CRLF。
- 不入库：`windows-runtime/`、`windows-local/`、`ubuntu-local/`、`gazebo-image/*.tar.gz`（及下载中的 `*.part`）、`deploy*/`、`real-b.env`。

## 测试

```bash
# Windows
Test.cmd                     # 或 windows-runtime\python\python.exe -m pytest（在 sawyer_teleop\ 下）
# Linux
cd sawyer_teleop && OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -p no:cacheprovider
```

运动相关改动必须跑 `tests/fixtures/lab_scene_20261022` 全部 7,177 条输入，旧横移 / 抬手和停手场景继续必过。
按改动范围验证，规则见 [DEVELOPMENT](sawyer_teleop/docs/DEVELOPMENT.md#按改动范围验证)。软件 / 替身结果不能当作物理或真机验收。

## 改动边界

- 每次改动说明影响：“仅更新 A”“仅更新 B（A 兼容）”或“A、B 都需更新”，以及代码、配置、依赖、重启、重校准影响。
- A：输入、映射、一个滤波器、异步 IK、界面、录制。B：模型、目标验证、100 Hz 轨迹、执行、反馈。
  改协议、参数语义或执行逻辑必须检查两端。
- 一个问题、一个假设、一项改动；保留失败与淘汰理由。不为代码形式新增复杂 IK、预测或控制层。
- 实机操作需要人类明确授权。默认只做仿真、离线和只读检查；只停止自己创建的实例。
- 导出用 `scripts/export_deployment.py`，只写新目录、不覆盖；导出不代表发布验收。

## 不变约束

- 最新目标覆盖，不排队追赶；松手优先取消；过期的输入 / IK / 反馈引用不得续期。
- 期限：A 输入 200 ms；B 最新反馈 100 ms；实机源引用 / 命令 250 / 250 ms；Gazebo 由 `config/gazebo-reference-timeout-ms`（当前 250）决定。
  不在整理或重构时改数值。
- 本地 IK 只用于 Gazebo；实机 / fake 使用远端 IK。实机不继承仿真快速参数或放宽期限。
- Quest 默认 APK 为 versionCode 9 / `4.3-input120`；不要恢复会截断长包的旧版本，不能用配置值冒充头显实测速率。
- 普通遥操不写录制；只有 Start Recording 才采集。日志展示只影响界面，不改变原始数据精度。
- 发布门见 [ROG_TRACKING_EXPERIMENT](sawyer_teleop/docs/reference/ROG_TRACKING_EXPERIMENT.md)：不删点、不减速、不移时、不放宽门槛。

## 记录

每项实质工作在 PLAN 的“当前决定”里补一段：影响范围、做了什么、结果、失败、下一步。
原始输出放 `windows-local/artifacts/<run-id>/`（不入库），PLAN 不贴长篇输出。
