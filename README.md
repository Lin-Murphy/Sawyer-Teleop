# Sawyer Quest 遥操

用 Meta Quest 右手柄遥操 Rethink **Sawyer** 机械臂，并录制演示数据。支持 Gazebo 仿真和实体 Sawyer（Intera SDK）。

- **A（操作端）**：Windows 或 Ubuntu。通过 USB/adb 读取 Quest 120 Hz 手柄输入，校准、握持映射、平滑、IK，
  本机 Web 操作台，按需录制 HDF5 / 视频。
- **B（执行端）**：Gazebo 仿真（Docker）或实验室 Ubuntu 上的实机桥接。负责目标校验、100 Hz 受限关节轨迹、
  可选 Intera 原生 Cartesian 执行，以及反馈与保护。

```mermaid
flowchart LR
    Q[Quest 右手柄] -->|USB / adb| A[A：Web 操作台、校准、IK、录制]
    A -->|TCP 目标| B[B：校验、100 Hz 轨迹]
    B --> R[Gazebo / 实体 Sawyer]
    R -->|关节反馈| B -->|反馈| A
```

当前状态：用户已在 Gazebo 中认可 Standard / Fast follow 的手感；停止、完整 VR、耐久和**真机验收尚未全部通过**。
详情见 [PLAN](sawyer_teleop/docs/PLAN.md)。

## 快速开始（Windows 本机仿真）

1. 安装 [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)（WSL 2 / Linux engine）。
2. 双击 **`Setup.cmd`**：自动下载 Python 3.11 与 adb 到 `windows-runtime/`，并安装依赖。
3. Quest 开启开发者模式并用 USB 连接，运行 `Setup.cmd quest` 安装头显 App。
4. 双击 **`Start-B.cmd`**，等待 `backend=gazebo`。首次运行会自动从本仓库 Releases 下载仿真镜像（约 1 GB）并校验、导入。
5. 双击 **`Start-A.cmd`**，在网页里选 Gazebo Simulation / `127.0.0.1` → Connect → Calibrate，握住右侧握键开始遥操。

没有 Quest 也可以先运行 `Test.cmd` 跑离线测试。Ubuntu 和实体 Sawyer 的安装见 [SETUP](sawyer_teleop/docs/SETUP.md)。

## Windows 入口

| 文件 | 作用 |
| --- | --- |
| `Setup.cmd` | 下载运行环境、安装依赖、自检；`Setup.cmd quest` 安装 Quest APK |
| `Start-A.cmd` | 启动 A（Web 操作台）；可加 `--port N`、`--no-browser` |
| `Start-B.cmd` | 启动本机 Gazebo B；`--headless` 无画面，`--stop` 停止，`--check` 只检查 |
| `Stop-B.cmd` | 停止本机 Gazebo B（同 `Start-B.cmd --stop`） |
| `Test.cmd` | 全套离线测试；`ik` / `lab-scene` / `feedback-delay` / `env` 为专项检查 |

Ubuntu 入口在 `sawyer_teleop/scripts/ubuntu/`：`Start-A.sh`、`Start-B-Sim.sh`、`Start-B-Real.sh`。

## 目录

```text
Setup.cmd / Start-A.cmd / Start-B.cmd / Stop-B.cmd / Test.cmd   Windows 入口
sawyer_teleop/      源码、测试、文档、Quest App（入库）
gazebo-image/       manifest.json 入库；镜像压缩包由 Start-B.cmd 从 Releases 自动下载（不入库）
windows-runtime/    Setup.cmd 下载的 Python 与 adb（不入库）
windows-local/      本机配置、录制、日志、测试报告，自动生成（不入库）
deploy-*/           导出的实验室 B 目录（不入库）
```

更新代码只需替换 `sawyer_teleop/`（或 `git pull`）；`windows-local/` 里的本机设置和录制不会被覆盖。

## 文档

| 文档 | 内容 |
| --- | --- |
| [SETUP](sawyer_teleop/docs/SETUP.md) | 安装：Windows、Ubuntu、实验室实机 B |
| [USAGE](sawyer_teleop/docs/USAGE.md) | 使用：启动、遥操、运动选项、录制、Reset |
| [TROUBLESHOOTING](sawyer_teleop/docs/TROUBLESHOOTING.md) | 排障：常见报错、暂停原因、诊断字段 |
| [ARCHITECTURE](sawyer_teleop/docs/ARCHITECTURE.md) | 架构与代码地图 |
| [DEVELOPMENT](sawyer_teleop/docs/DEVELOPMENT.md) | 开发、测试、发布门、导出 B |
| [全部文档](sawyer_teleop/docs/README.md) | 索引与专项参考 |

AI 助手请先读 [AGENTS.md](AGENTS.md)。

## 安全

实机 B 不会自动使能、回中或清除急停；请按实验室流程操作并保持唯一运动控制者。
仿真与软件测试结果不能代替真机验收。
