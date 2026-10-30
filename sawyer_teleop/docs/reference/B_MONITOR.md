# B 状态监视

monitor 读取 B 生成的单份 JSON 快照，不建立第二个控制连接、不发送动作。
启动命令统一在[使用指南](../USAGE.md)；旧桌面服务名称不作为当前入口。

| 指标 | 含义 |
| --- | --- |
| Backend / PID / Status | 后端、进程与快照是否仍更新 |
| A connected / Armed / Reason | 会话、执行权限和暂停原因 |
| Last valid target | 最近通过校验的 A 目标，不等于已提交 |
| Last backend command | B 最近提交的参考，不等于到位 |
| Actual feedback | 实测关节；fake 下仅为软件模拟 |
| Received / Submitted / Rejected | 当前 B 生命周期计数；一个源目标可以产生多条轨迹提交 |
| Receive rate | 两个快照间的有效目标接收率，不是全部 TCP 帧率 |
| Feedback age | B 所见源年龄，不含 B→A 的传输等待 |
| Receive-to-submit | 该源目标收到至本次提交的时间，不是单纯计算耗时或端到端延迟 |
| Recent events | 最近五条暂停/保持/断线事件 |

关节顺序 `right_j0 … right_j6`，单位 rad。RTT 和 A 所见年龄看 A；B 不凭收包时刻推断单向网络延迟。
历史目标值会保留，不能据它判断机器人仍在运动。快照超过 3 秒未更新时显示 stopped/stale。

默认每 0.5 秒展示一次，Ctrl+C 只退出监视。状态写入为原子替换，内存文件只保留一份；
写入故障报告到 stderr，不阻塞控制线程。完整数据仍由 A 显式录制。
详细日志边界见 [TROUBLESHOOTING](../TROUBLESHOOTING.md)。
