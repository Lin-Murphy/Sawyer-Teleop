# Gazebo 仿真镜像

镜像压缩包太大（约 1 GB），不放进 git，发布在本仓库 GitHub **Releases**（tag `sim-20261017`）：
https://github.com/Lin-Murphy/sawyer-teleop/releases/download/sim-20261017/sawyer-simulation-20261017.tar.gz

`manifest.json` 记录了它的 SHA256 和镜像 ID，启动器会自动校验。

- Windows：`Start-B.cmd` 首次运行时缺压缩包会自动下载到本目录，校验后 `docker load`。
- Ubuntu：手动下载到本目录，再 `docker load --input gazebo-image/sawyer-simulation-20261017.tar.gz`。

导入 Docker 后压缩包可以删除。详见 [SETUP](../sawyer_teleop/docs/SETUP.md#windows-本机仿真-b)。
