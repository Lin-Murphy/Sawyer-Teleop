# RealSense D435：实时 RGB 预览

当前按用户要求先使用 **RGB 单流**：`rgb_preview.py` 只启用彩色传感器，不等待深度帧。界面使用 Tkinter/Pillow，不调用 OpenCV Qt，因此不会触发此前的 Qt 线程 / 字体目录报错。默认 640×480、30 FPS，按 Q / Esc 或关闭窗口退出，不保存图像。

```bash
bash /home/endongsun/Desktop/sawyer_teleoperation/Preview-RealSense-Camera.sh
```

该入口自动使用 `arcap`。也可直接运行：

```bash
conda activate arcap
python /home/endongsun/Desktop/sawyer_teleoperation/sawyer-teleop/realsense_camera/rgb_preview.py
```

循环里的 `rgb` 是独立内存的 `(480, 640, 3)` / `uint8` 数组，通道顺序为 RGB。不打开窗口、读取 120 帧后退出：

```bash
bash /home/endongsun/Desktop/sawyer_teleoperation/Preview-RealSense-Camera.sh --headless --frames 120
```

## RGB + Depth（保留代码，当前深度无帧问题尚未解决）

使用已配置好 OpenCV、NumPy、pyrealsense2、Tkinter、Pillow 的 `arcap` 环境，直接运行：

```bash
conda run -n arcap python /home/endongsun/Desktop/sawyer_teleoperation/sawyer-teleop/realsense_camera/rgb_depth_preview.py
```

或在已激活的环境中运行 Python：

```bash
conda activate arcap
python /home/endongsun/Desktop/sawyer_teleoperation/sawyer-teleop/realsense_camera/rgb_depth_preview.py
```

两路均配置为 **640×480、30 FPS**。左侧彩色画面，右侧深度伪彩图（显示范围 0–3 米，无效深度为黑色）；中心十字显示该像素的深度。按 **Q / Esc** 或关闭窗口退出；脚本只读取相机，不写盘。

预览采用 Tkinter/Pillow，不调用 OpenCV 的 Qt 窗口接口，避免本机 `arcap` 中的 `QObject::moveToThread` 和 Qt 字体目录错误。窗口使用非阻塞取帧，等待图像时仍可退出。

循环内可直接使用：

| 数组 | 类型 / 形状 | 含义 |
| --- | --- | --- |
| `rgb` | `uint8` / `(480, 640, 3)` | 真正的 RGB 通道顺序 |
| `depth_raw` | `uint16` / `(480, 640)` | 对齐到 RGB 坐标的 Z16 深度值 |
| `depth_m` | `float32` / `(480, 640)` | `depth_raw × 相机实际 depth_scale`，单位为米 |

`depth_m[y, x]` 对应 `rgb[y, x]` 的深度，0 表示没有有效读数。深度是相机坐标的 Z 值，不是像素点到镜头的欧氏距离。空间对齐沿用 SDK 的 `rs.align(rs.stream.color)`，不代表两个传感器具有完全相同的曝光时刻。预览的 3 米上限不裁剪 `depth_m`。

不打开窗口、读取 60 对图像后退出：

```bash
conda run -n arcap python /home/endongsun/Desktop/sawyer_teleoperation/sawyer-teleop/realsense_camera/rgb_depth_preview.py --headless --frames 60
```

多台相机时可加 `--serial 相机序列号`。运行前退出占用同一设备的 RealSense Viewer 或其他采集程序；预览需要本地图形桌面。

参考：[RealSense 官方 Python 对齐示例](https://github.com/realsenseai/librealsense/blob/master/wrappers/python/examples/align-depth2color.py)。
