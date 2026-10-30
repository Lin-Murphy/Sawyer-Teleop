# Quest 120 Hz 应用源码

唯一默认 APK 是 `sawyer-quest-input-120hz.apk`，versionCode 9 / `4.3-input120`，
包名 `org.sawyer.questinput`，protocol 3；SHA256 和签名指纹见 `apk.json`。
安装与操作统一见 [A 部署](../docs/USAGE.md)。

2026-10-22 从已保留的 `sawyer-transfer-lab-20261019/project.tar.gz` 恢复 OpenXR 源码、
固定的 1.1.36 loader/头文件和原开发签名，再补齐现用 protocol 3 的 A/B 按钮位及 120 Hz 请求。
这是可重建的新版，不冒称缺失的 versionCode 7 原构建；旧 APK 已由同签名覆盖升级替换。

2026-10-23 修复 versionCode 8 的长输入包截断：完整 JSON 改用 `__android_log_write`，
避开 `__android_log_print` 的 1024 字节格式化缓冲。保留所有字段和精度；超过 4000 字节直接拒绝，
不发送半包。版本与原开发签名已校验，主机侧边界回归通过；versionCode 9 的头显 / Windows 实测仍待完成。
不能只替换 PC 源码，必须运行 `Setup.cmd quest`（Ubuntu：`scripts/install-quest.sh`）将头显应用同签名升级。

`src/main.cpp` 保留现实透视、STAGE 坐标、一次 OpenXR 帧一次数据包、佩戴/焦点/追踪有效性与重定位标识。
右 A / B / 摇杆按钮位分别为 1 / 2 / 4；左 X / Y 对应 1 / 2。
新增 `refresh` 诊断字段，区分请求 120 Hz、实际 DISPLAY 与实际 INPUT，HUD 也显示实际值。
每次会话开始只请求枚举中存在的 120 Hz，不主动选择 72/80/90 Hz，不循环覆盖系统降频；
无法获得 120 Hz 时明确报告，不把低频标为 120。
刷新请求与实际值的区分遵循 [Khronos OpenXR 接口说明](https://registry.khronos.org/OpenXR/specs/1.1/man/html/xrRequestDisplayRefreshRateFB.html)。

离线构建：项目根目录执行 `bash scripts/build_quest_120.sh`，复用本机
`sawyer-quest-builder:openxr-1.1.36`，不下载或安装系统依赖。
`test_policy.cpp` 检查不支持 120 Hz 的处理和全部按钮位组合；`test_input_log.cpp` 检查长包完整写入与超限拒绝。
versionCode 8 的实际头显输入统计见
原作者本机的实测记录，不等于 versionCode 9 或完整遥操运动验收。
