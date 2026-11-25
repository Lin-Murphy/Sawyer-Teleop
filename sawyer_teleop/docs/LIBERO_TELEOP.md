# Quest 3 采集 LIBERO atomic 示范

Quest 3 沿用本仓库 protocol 3 的 APK 和 ADB 输入。A 是接 Quest 的电脑，B 是运行 LIBERO 的 Linux 主机。B 用 MuJoCo 离屏渲染，A 的浏览器通过 SSH 隧道看两路相机、校准、换场景和录制。

| `--atomic` | 使用的本地 LIBERO 任务 |
| --- | --- |
| `grasp` | `soup90_v2/grasp_target_object.bddl`，固定空爪起点，抓起 alphabet soup |
| `pull` | `open_the_middle_drawer_of_the_cabinet.bddl`，拉开中层抽屉 |
| `place` | `soup90_plane_v3/place_target_object_in_basket.bddl`，从通过原准备门槛的持物起点开始 |

B 使用包含这三个任务和 Place 初态银行的 `libero_demt` 环境。也可以用 `--bddl` 传入同一环境能加载的自定义任务。每个任务的成功由自身原生判据给出。

## B 启动仿真

在可运行 LIBERO 离屏渲染的节点上进入 Sawyer-Teleop 仓库。下面用同级的 `libero_demt` 和已激活的 LIBERO Python 环境。现有 LIBERO 配置应指向带 atomic 任务的源码和资产。没有 GPU 的节点可使用已有的 Mesa 软件 EGL（`EGL_PLATFORM=surfaceless LIBGL_ALWAYS_SOFTWARE=1`）。

```bash
export PYTHONPATH="$PWD/sawyer_teleop/src:$PWD/../libero_demt"
umask 077
if [ ! -s "$HOME/.libero-teleop-key" ]; then
  python -c 'import secrets; print(secrets.token_hex(24))' > "$HOME/.libero-teleop-key"
fi
MUJOCO_GL=egl python -m sawyer_teleop.libero_teleop serve \
  --atomic grasp --seed 0 --output "$PWD/windows-local/libero-grasp" \
  --key-file "$HOME/.libero-teleop-key"
```

换任务时把 `--atomic` 改成 `pull` 或 `place`，同时给其自己的录制目录。保持当前仿真终端运行。记录该节点的主机名。A 和 B 同机时跳过隧道步骤。

## A 接入 Quest 和浏览器

1. 在 A 通过有权限的 SSH 路径连接 B，转发本机端口 8782 到 B 的回环端口。例如 B 可通过集群登录节点跳转时，用 `ssh -N -J USER@LOGIN -L 8782:127.0.0.1:8782 USER@COMPUTE_NODE`。
2. 连接已安装现有 Quest APK 并授权 ADB 的 Quest 3。如同一设备正被 Sawyer 的 A 操作端占用，先退出该会话。进入 A 的 Sawyer-Teleop 仓库，启动 Quest 输入转发。命令提示时输入 B 的站点密钥。在 [浏览器操作台](http://127.0.0.1:8782/) 使用同一个密钥连接。

Windows PowerShell 使用 Sawyer-Teleop 已安装的运行时。

```powershell
$env:PYTHONPATH = "$PWD\sawyer_teleop\src"
.\windows-runtime\python\python.exe -m sawyer_teleop.libero_teleop quest --adb .\windows-runtime\platform-tools\adb.exe
```

Linux 或现有虚拟环境使用对应的 Python，A 只需要现有 Sawyer 操作端的 NumPy、SciPy 和 adb。

```bash
PYTHONPATH="$PWD/sawyer_teleop/src" python -m sawyer_teleop.libero_teleop quest
```

如果有多个 ADB 设备，另传 `--serial`。默认端口 8782，可在两端用 `--port` 配置 SSH 映射后的端口。

3. 面向你希望对应 Panda 世界坐标 +X 的方向，松开右握键，点 **Calibrate heading**。右握键按住后开始跟随相对手柄位姿，松手暂停。右扳机 ≥0.3 请求闭爪。接管已经闭合的夹爪时，先按下扳机匹配闭爪，再松开扳机以开爪。这保留了 Place 起点的持物状态。追踪中断后松手再握。Quest 重定位后重新校准。
4. 点 **Start recording from reset** 从当前种子的原生初态开始录制。点 **Stop and save** 保存当前尝试。查看界面的任务结果。用 **Next scene** 切到下一个种子。持物准备失败时，站点显示该种子和失败原因，可用 Next scene 继续，不自动重采。成功帧与完整保存状态在每条记录里分别标注。

头显提供真实场景透视和手柄数据。LIBERO 画面显示在 A 的浏览器。

## 录制与重放

每次明确录制生成一个 `episode_*/`。其中 `trajectory.hdf5` 含 T 条原生 Panda OSC_POSE7 动作、T+1 帧同步仿真观测及对应 frame ID。两路 RGB 按原生 OpenGL 方向保存为 128×128 uint8。浏览器预览翻转后 JPEG 编码。轨迹还保留 arm7、finger2、MuJoCo 状态、时间、末端位姿、成功标记和每个控制步选中的 Quest 包。Place 原生准备发生在第 0 帧之前，准备凭据随 `start.json` 保存。

`start.json`、任务 BDDL 副本和 `model.xml` 用来追溯起点。用相同的 LIBERO 任务资产和运行时，用保存种子初始化并恢复第 0 帧的仿真状态、控制器和 fixture 位姿，只执行保存的动作，然后核对每帧。

```bash
MUJOCO_GL=egl python -m sawyer_teleop.libero_teleop replay /path/to/episode_...
```

后续制作 DP 教材时，每条示范用 `observations` 前 T 帧对齐 `actions/native_osc_pose`。策略字段是 `agentview_rgb`、`eye_in_hand_rgb`、`joint_states` 和 `gripper_states`。

按设计普通遥操只显示预览。只有显式录制才写数据。中途重置或服务正常退出的轨迹标记为未完整结束。写入异常的目录保留 `.pending_` 前缀供排查。
