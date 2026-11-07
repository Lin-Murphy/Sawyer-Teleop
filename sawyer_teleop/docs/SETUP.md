# 安装

本项目分两端：**A**（操作端：Quest 输入、Web 操作台、录制）和 **B**（执行端：Gazebo 仿真或实体 Sawyer）。
按自己的场景选一节即可。目录约定见[仓库 README](../../README.md#目录)。

| 场景 | A 在哪 | B 在哪 | 需要准备 |
| --- | --- | --- | --- |
| 本机仿真试用 | Windows | 同一台 Windows 的 Docker | Setup.cmd + Docker Desktop + 仿真镜像 |
| Ubuntu 仿真 | Ubuntu 或另一台 Windows | Ubuntu 的 Docker | Docker + 仿真镜像 |
| 实体 Sawyer | Windows / Ubuntu | 实验室 Ubuntu 20.04 + ROS Noetic + Intera | 导出的 `B-Lab-Ubuntu` 目录 |

## Windows A（必做）

前提：Windows 10/11 x64、可以访问 nuget.org、dl.google.com 和 PyPI。

1. 克隆或下载本仓库，路径可以有空格或中文。
2. 双击仓库根目录的 **`Setup.cmd`**，它会：
   - 下载 Python 3.11 x64（python.org 官方 NuGet 包）到 `windows-runtime/python/`；
   - 下载 Android platform-tools（adb）到 `windows-runtime/platform-tools/`；
   - 按 `sawyer_teleop/pyproject.toml` 安装固定版本依赖（numpy、scipy、h5py、opencv、ruckig、pytest），并以可编辑方式安装源码；
   - 最后在临时目录导入一次本项目，确认指向当前源码。
3. 双击 **`Test.cmd`** 跑全套离线测试，确认环境可用（Linux 专属用例会自动跳过）。

已有的 `windows-runtime/` 不会被重复下载。整个目录搬家或改名后，再运行一次 `Setup.cmd`。
`windows-runtime/` 和 `windows-local/`（本机配置、录制、日志）都不入 git。

### Quest 头显

1. 头显开启开发者模式，USB 连接电脑并在头显里允许 USB 调试。
2. 双击运行 `Setup.cmd quest`（命令行：`Setup.cmd quest --serial <序列号>`）。
   安装器先核对 `quest/apk.json` 里的 SHA256，覆盖安装后读取设备上的版本，不卸载、不清数据。
3. 头显里打开 **Sawyer Quest Input**。

当前 APK 为 versionCode 9 / `4.3-input120`（120 Hz 输入）。重建 APK 见 [quest/README.md](../quest/README.md)。

## Windows 本机仿真 B

1. 安装 [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)，使用 WSL 2 / Linux engine，
   General 中保留 **Use containerd for pulling and storing images**。要看三维画面还需要
   [WSLg](https://learn.microsoft.com/en-us/windows/wsl/tutorials/gui-apps)；`--headless` 模式不需要。
2. 运行 `Start-B.cmd --headless --check`：检查 Docker，准备并导入镜像，不启动仿真。

Docker 里没有镜像时，`Start-B.cmd`（含 `--check`）会按下面的顺序准备：

1. `gazebo-image/sawyer-simulation-20261017.tar.gz` 不存在就自动下载（约 1 GB），地址是
   `https://github.com/Lin-Murphy/sawyer-teleop/releases/download/sim-20261017/sawyer-simulation-20261017.tar.gz`；
   下载先写到 `.part` 文件，完成后再改名，中断不会留下半截压缩包。
2. 按入库的 `gazebo-image/manifest.json` 校验 SHA256。新下载的文件校验失败会被删除；已有的文件校验失败就报错，提示你删掉后重试。
3. `docker load` 后再核对镜像 ID
`sha256:f5e1e474226438833d57352952d9c347b952a206366b02bb8311477bde665046`，不一致就拒绝启动。
网络不通时，可以手动下载上面的文件放进 `gazebo-image/`，或用环境变量 `SAWYER_SIM_IMAGE_URL` 指定镜像站地址。
镜像导入 Docker 后，`gazebo-image/*.tar.gz` 可以删掉；以后重装 Docker 时会再自动下载。

镜像内容：Ubuntu 20.04、ROS Noetic、Gazebo、Intera SDK、sawyer_simulator 及其控制器。
项目源码不在镜像里，启动时以只读方式挂载当前 `sawyer_teleop/`，所以改源码不需要重建镜像。

## Ubuntu 仿真 B

前提：Ubuntu x86_64，已安装 Docker Engine，当前用户能运行 `docker`。

```bash
curl -L -o gazebo-image/sawyer-simulation-20261017.tar.gz \
  https://github.com/Lin-Murphy/sawyer-teleop/releases/download/sim-20261017/sawyer-simulation-20261017.tar.gz
sha256sum gazebo-image/sawyer-simulation-20261017.tar.gz   # 与 manifest.json 的 archive_sha256 对比
docker load --input gazebo-image/sawyer-simulation-20261017.tar.gz
cd sawyer_teleop
bash scripts/ubuntu/Start-B-Sim.sh --headless --check
```

`--check` 只核对 Docker 和镜像 ID，不创建容器。启动方式见 [USAGE](USAGE.md#ubuntu-仿真-b)。

## Ubuntu A（可选）

A 也可以运行在 Ubuntu（需要图形会话以打开浏览器，以及 `adb`）：

```bash
cd sawyer_teleop
python3 -m venv .venv
.venv/bin/pip install -e '.[operator,test]'
sudo apt install adb          # 或把 platform-tools 加入 PATH
```

`scripts/ubuntu/Start-A.sh` 默认使用 `sawyer_teleop/.venv/bin/python`（可用 `SAWYER_PYTHON` 覆盖），
本机配置与录制放在同级 `ubuntu-local/`（可用 `SAWYER_LOCAL_HOME` 覆盖）。
Quest APK 安装（需先装 adb，如 `sudo apt install adb`）：`bash scripts/install-quest.sh --serial <序列号>`。

## 实体 Sawyer B（实验室 Ubuntu）

实验室电脑只运行实机 B，**不需要 Docker、仿真或 pip 安装**，复用现有 ROS Noetic / Python 3.8 / Intera 工作空间。

### 在实验室电脑从 Git 仓库直接运行

仓库直接提供 [实验室实机配置](../config/real-b.env)，包含现场主机名、IP 和夹爪串口及注释。
同一台实验室电脑无需复制模板，在仓库根目录执行：

```bash
cd sawyer_teleop
bash scripts/ubuntu/Start-B-Real.sh --check
```

配置使用 `sawyer.local` 和实验室 B 电脑地址 `10.101.119.182`；工作空间使用 `$HOME/sawyer_ws`。
换电脑、网卡地址变化或更换夹爪适配器时，先按文件注释修改 IP、ROS / 工作空间路径和串口。
没有外接夹爪时将 `SAWYER_GRIPPER_PORT` 设为空。该配置受 Git 跟踪，更新代码时需审查本地配置改动；
其他电脑也可复制通用 `config/real-b.env.example` 到仓库外，用启动脚本的 `--config` 指定独立配置。

检查通过后按实验室流程准备机器人，停止旧 B，再运行：

```bash
bash scripts/ubuntu/Start-B-Real.sh
```

A 选择 Real Sawyer，填写本机配置的 `SAWYER_BIND_IP`，重新连接 / 校准。
启动会初始化已配置的夹爪，不自动执行机械臂 Reset。

### 导出独立部署包

1. 在开发电脑导出独立目录（只写新目录，不覆盖旧的）：

   ```bat
   sawyer_teleop\scripts\windows\run-python.cmd -I sawyer_teleop\scripts\export_deployment.py --output ..\deploy-next
   ```

   Ubuntu 上：`python3 scripts/export_deployment.py --output ../deploy-next`。
   生成 `deploy-next/B-Lab-Ubuntu/`，含源码、启动器、Reset 姿态、固定的 Python 3.8 Ruckig 和逐文件 SHA256 清单。
2. 把 `B-Lab-Ubuntu/` 复制到实验室电脑的新目录。
3. 编辑其中的 `config/real-b.env`（三项地址必填，ROS / 工作空间路径按实际修改）：

   | 配置 | 含义 |
   | --- | --- |
   | `SAWYER_ROBOT_HOST` | 机器人 ROS master 的主机名或 IPv4，不含 `http://` 和端口 |
   | `SAWYER_ROS_IP` | B 电脑连接机器人那张网卡的 IPv4，机器人必须能回连 |
   | `SAWYER_BIND_IP` | A 能访问到的 B 电脑 IPv4，A 界面填这个；可与上一项相同 |
   | `SAWYER_ROS_SETUP` | 默认 `/opt/ros/noetic/setup.bash` |
   | `SAWYER_WORKSPACE` | 已编译、含 Intera SDK 的 catkin 工作空间，默认 `$HOME/sawyer_ws` |
   | `SAWYER_B_PYTHON` | 可选；默认 `.venv-b/bin/python`，否则 `python3` |

4. `bash Start-B.sh --check`：只检查本地配置和依赖，不访问机器人。

源码中的 `config/real-b.env` 直接入库；其他位置的同名本机配置仍被忽略。
导出时使用 `python3 scripts/export_deployment.py --output ../deploy-next --config config/real-b.env`
携带这份实验室配置；不指定 `--config` 时保留原有导出选择规则。
`runtime-wheels/` 里是 Linux x86_64 / Python 3.8 的 Ruckig 0.12.2 及其校验清单，必须随目录一起复制。

## 下一步

日常启动与操作见 [USAGE](USAGE.md)。出问题看 [TROUBLESHOOTING](TROUBLESHOOTING.md)。
