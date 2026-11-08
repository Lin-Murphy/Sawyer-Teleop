# Sawyer Quest 遥操

用 Meta Quest 右手柄遥操 Rethink **Sawyer** 机械臂，并录制用于机器人学习的演示数据。
同一套代码支持 **Gazebo 仿真** 和 **实体 Sawyer**（Intera SDK）。

```mermaid
flowchart LR
    Q[Quest 右手柄] -->|USB / adb 120 Hz| A[A 操作端<br/>Web 操作台 · 校准 · IK · 录制]
    C[腕部 / RealSense 相机] --> A
    A -->|TCP 目标| B[B 执行端<br/>校验 · 100 Hz 轨迹 · 保护]
    B --> R[Gazebo / 实体 Sawyer]
    R -->|关节 / 末端反馈| B -->|反馈| A
```

| 端 | 运行在 | 负责 |
| --- | --- | --- |
| **A 操作端** | Windows 或 Ubuntu | 读取 Quest 手柄、校准与握持映射、平滑、IK、本机 Web 操作台、按需录制 HDF5 / MP4 |
| **B 执行端** | Docker 中的 Gazebo，或实验室 Ubuntu 实机桥接 | 目标校验、100 Hz 受限关节轨迹、可选 Intera 原生 Cartesian、夹爪、反馈与保护 |

## 当前状态

- ✅ Gazebo 中 Standard / Fast follow 手感已获用户认可；离线回归与固定 7,177 条实验室输入门通过。
- ✅ 录制：原生频率 raw 数据（关节、末端位姿、两路相机，HDF5 无损 + MP4），掉帧只标记不补帧。
- ✅ Reset 回中时自动张开夹爪并等待确认。
- ⏳ 待验收：实机 60 s 双相机录制、手动 / 批间 Reset 开夹、停止、完整 VR 用例、耐久与**真机验收**。

最新进度与决定只看 [PLAN](sawyer_teleop/docs/PLAN.md)。

## 快速开始（Windows 本机仿真）

前提：Windows 10/11、[Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)（WSL 2 / Linux engine）、
开启开发者模式的 Quest 与 USB 线。

1. 双击 **`Setup.cmd`**：下载 Python 3.11 与 adb 到 `windows-runtime/`，安装依赖并自检。
2. Quest 用 USB 连接后运行 `Setup.cmd quest`，安装头显输入 App。
3. 双击 **`Start-B.cmd`**，等待出现 `backend=gazebo`。
   首次运行会从本仓库 Releases 下载仿真镜像（约 1 GB），校验后导入 Docker。
4. 双击 **`Start-A.cmd`**，浏览器自动打开操作台：选 **Gazebo Simulation / `127.0.0.1`** → **Connect** → **Calibrate**。
5. 按住右手柄握键开始遥操，松开即停。需要数据时点 **Start Recording**（普通遥操不写盘）。

没有 Quest 也可以先运行 `Test.cmd` 跑离线测试。
Ubuntu 操作端和实验室实机 B 的安装见 [SETUP](sawyer_teleop/docs/SETUP.md)，日常操作见 [USAGE](sawyer_teleop/docs/USAGE.md)。

## 入口

**Windows**（仓库根目录）

| 文件 | 作用 |
| --- | --- |
| `Setup.cmd` | 下载运行环境、安装依赖、自检；`Setup.cmd quest` 安装 Quest APK |
| `Start-A.cmd` | 启动 A（Web 操作台）；可加 `--port N`、`--no-browser` |
| `Start-B.cmd` | 启动本机 Gazebo B；`--headless` 无画面，`--stop` 停止，`--check` 只检查 |
| `Stop-B.cmd` | 停止本机 Gazebo B（同 `Start-B.cmd --stop`） |
| `Test.cmd` | 全套离线测试；`ik` / `lab-scene` / `feedback-delay` / `env` 为专项检查 |

**Ubuntu**（`sawyer_teleop/scripts/ubuntu/`）：`Start-A.sh`、`Start-B-Sim.sh`、`Start-B-Real.sh`。

## 目录

```text
Setup.cmd … Test.cmd   Windows 入口
sawyer_teleop/         源码、测试、配置、文档、Quest App
realsense_camera/      RealSense D435 预览脚本（Linux 参考）
sandberg_camera/       腕部相机采集与预处理（Linux 参考）
gazebo-image/          仿真镜像 manifest；镜像包由 Start-B.cmd 自动下载
── 以下本机生成，不入库 ──
windows-runtime/       Setup.cmd 下载的 Python 与 adb
windows-local/         本机配置、录制、日志、测试报告
deploy/、deploy-*/     导出给实验室的 B-Lab-Ubuntu 目录
```

更新代码只需 `git pull`（或替换 `sawyer_teleop/`），`windows-local/` 里的本机设置和录制不会被覆盖。

## 文档

| 我想… | 看这里 |
| --- | --- |
| 安装 Windows / Ubuntu / 实验室实机 B | [SETUP](sawyer_teleop/docs/SETUP.md) |
| 启动、遥操、切换运动选项、录制、Reset | [USAGE](sawyer_teleop/docs/USAGE.md) |
| 处理报错、看懂暂停原因和诊断字段 | [TROUBLESHOOTING](sawyer_teleop/docs/TROUBLESHOOTING.md) |
| 了解架构、线程和代码位置 | [ARCHITECTURE](sawyer_teleop/docs/ARCHITECTURE.md) |
| 改代码、跑测试、导出 B | [DEVELOPMENT](sawyer_teleop/docs/DEVELOPMENT.md) |
| 查看录制数据格式 | [DATASET](sawyer_teleop/docs/reference/DATASET.md) |
| 浏览全部文档与专项参考 | [文档索引](sawyer_teleop/docs/README.md) |

AI 助手请先读 [AGENTS.md](AGENTS.md)。

## 安全

- 实机 B **不会**自动使能、回中或清除急停；请按实验室流程操作，并保证同一时间只有一个运动控制者。
- 仿真和软件测试结果**不能**代替真机验收。
