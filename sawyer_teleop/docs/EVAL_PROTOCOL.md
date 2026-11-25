# 抽屉放物体策略评测协议

> 状态：**草案，未定稿**。开跑前要补齐 §9 的待定项，算出本文件的 SHA-256，并记入 `trials.csv` 同目录的 `protocol_sha256.txt` 和 PLAN.md。
> 定稿后不再修改，如需修改须升版本号（v2），并写明修改原因和修改前已跑的试验。

## 1. 任务与成功判定

任务名沿用录制 `open drawer and pick cube to drawer`：机械臂从固定起始位姿出发，先拉开抽屉，再抓起物体放入抽屉。
录像核对（2026-11-25）：实物是**红色五角星**（下文称"物体"），放入抽屉内的绿色碗中。

评测分三类（2026-11-25 用户决定）：**ID**、**OOD**（只改物体和碗的位置）、**抗干扰**（执行过程中人为改变抽屉、物体或碗的位置）。
朝向、静态干扰物和换物体都不评测。

| 阶段 `stage_reached` | 判定 |
|---|---|
| 0 | 未完成阶段 1 |
| 1 抽屉已开 | 抽屉开口足以放入物体（开度阈值见 §9） |
| 2 已抓起 | 物体离开桌面且由夹爪夹持 ≥1 s |
| 3 已放入 | 物体松开后位于**碗内**（2026-11-25 用户确认；与 VLA 任务指令 "place it in the green bowl inside the drawer" 一致）。只进抽屉、没进碗算失败，失败模式记 `place_miss` |

- **成功（`success=1`）**：在**时间上限 60 s 内**达到阶段 3。结束时物体仍在碗内，过程中没有人工干预和安全停止。
- 60 s 与录制批次的单条时长一致；训练示范的耗时为 15–45 s。
- 判定员依据现场情况和录像判定。全部录像由第二人独立复判，报告两人一致率；不一致时以复看录像为准，并在 `notes` 中记录。

## 2. 评测对象

每个方法开跑前固定以下信息，记入 `trials.csv` 每一行：
- `checkpoint_sha256`：checkpoint 文件哈希；
- `code_commit`：推理代码提交；
- `config_sha256`：推理配置哈希；
- 训练数据：HF `Murphy-Lin/Sawyer_Pull_Pick_Place` 的提交号，以及排除的录制。

checkpoint 只能用验证集挑选，不得用本协议的测试条件挑选。

## 3. 初始条件

- 来源：`windows-local/artifacts/eval-init-conditions-20261125-v3/init_conditions.csv`，种子 `20261125`，共 85 条（2026-11-25 用户确认）。
  - v3 中 ID 和训练原位共 40 条与 v2 相同。
  - 物体 OOD 在"抓取坐标距训练凸包 2–6 cm"之外，还要求目标投到 RealSense 图像后，距训练星中心像素凸包 ≥ 14.3 px（判定阈值 6.35 px + 单应误差中位数约 7.9 px）。
    结果是物体 OOD 几乎都位于训练分布沿 y 方向两端之外（y 约 0.18–0.23 或 0.59–0.63 m），因为相机视角下 x 方向（纵深）的变化很小。
  - v2 作废：物体 OOD 投影后有 7/10 条与训练位置相近。v1 作废：朝向 OOD 失效，评测轴已调整。
**定位方式（2026-11-25 用户决定）：ID 和 OOD 的物体与碗都用机械臂定位；抗干扰组的初始摆放就是 ID 的 C001–C005，干扰本身手动施加，不需要机械臂定位。**
相机不用于坐标换算，只用于**图像对照**（2026-11-25 用户要求）和保存参考图。用户也可以选择**跳过机械臂**（`--no-arm`），直接对照图像手动摆放。

**图像对照（`scripts/eval_vision.py`，参考数据在 `windows-local/artifacts/placement-vision-20261125/`）：**
- 参考数据来自 60 条训练录制的 RealSense 首帧：
  - 训练星中心的像素凸包；
  - 训练星覆盖区；
  - 逐像素中位数背景；
  - 由"闭爪末端 xy ↔ 星像素中心"拟合的单应矩阵。
- **判定在像素空间完成**：
  - ID：实时星中心在训练凸包内（容差 1 px）；
  - OOD：实时星中心在训练凸包外 ≥ 6.35 px（按凸包中心处尺度约等于 2 cm）。

  单应矩阵的留一法误差中位数约 2.5 cm、最大约 11 cm，原因是抓取点相对星中心的偏差不固定。所以它只用于画约 ±2 cm 的品红引导圈和估计坐标，**不用于判定**。
- 背景差（星区域外与中位数背景的平均差）> 12 时提示相机可能移动，需先用相机对比工具恢复相机位姿。
- 对照窗口：
  - 上排：实时画面、与背景的差异热图；
  - 下排：柜面放大图，分别是实时画面、与最近训练首帧的 50% 叠加。

  按键：判定通过时 Enter / 空格确认；未通过时只能按 F 强制确认（记入日志），或按 Q 跳过本条。
  机械臂模式下，对照在机械臂退回后进行；未通过时可以直接微调物体，直到通过。
- 每条保存 `reference/<cond_id>_diff.png`，判定结果和各项数值写入 `placement_log.jsonl`。
- 碗在抽屉内，评测开始时抽屉关着，看不到碗，所以碗不做图像对照。
- 训练首帧的中位数背景显示，**训练时抽屉的初始状态是拉开一小段**，不是完全关闭（开度见 §9）。

- **物体**：条件表中的 `eef_x_m` / `eef_y_m` 是抓取时末端（`right_hand`）在 base 系下的 xy。
  机械臂悬停在该 xy、抓取高度 +2–3 cm 处，夹爪张开，偏航按 `grasp_yaw_deg`；把物体放在两指正中，然后机械臂退回起始位。
  五角星与夹爪合起来的朝向周期为 36°，任一等价朝向均可。到不了悬停点的条件，视为不可达，按下文 `requires_check` 规则替换。
- **碗**：`bowl_dx_cm` / `bowl_dy_cm` 是抽屉坐标系下相对训练时碗位置（home）的偏移，dx 沿拉出方向为正，dy 面向抽屉向右为正。
  通过一次标定换算到 base 系：
  1. 把抽屉拉到固定、可重复的位置（如拉到底的限位处，记为 `drawer_ref`），在抽屉底部标出 home 中心。
  2. 机械臂对准 home 标记，记下末端 xy 作为 `bowl_home`。
  3. 抽屉从 `drawer_ref` 退回约 10 cm，再对准同一标记记一次。两点之差归一化后为拉出方向 u，横向方向 v 与 u 垂直，按"面向抽屉向右"取号。
  4. 碗目标 = `bowl_home` + dx·u + dy·v（cm 换算为 m）。
  
  标定结果（`bowl_home`、u、v、`drawer_ref` 的描述、标定时间）写入评测目录的 `bowl_calibration.json`，并记录其哈希。抽屉或桌子移动后必须重新标定。
  摆放时抽屉保持在 `drawer_ref`，机械臂悬停在碗目标上方，把碗中心对准夹爪正下方放好，机械臂退回，再把抽屉推回初始状态。
- 抽屉初始状态与训练一致（`drawer=as_train`）：根据训练首帧，是拉开一小段，具体开度见 §9。
- **跳过机械臂（`--no-arm`）**：碗按尺子量 `bowl_dx_cm` / `bowl_dy_cm` 偏移；物体对照窗口手动摆放，直到判定通过。
  这时记录的 `est_xy_approx` 只是估计值（约 ±2 cm），论文中物体位置以像素判定和 diff 图为准。
- 每个条件第一次摆好后，保存 RealSense 和腕相机参考图，存为 `reference/<cond_id>_{realsense,wrist}.png`。
  同一条件下后续试验（其他方法）复位时，可以用相机对比工具对照参考图摆放，不用再动机械臂；对不齐时重新用机械臂定位。
- 放好后拍一张俯视照片，存为 `photos/<trial_id>.jpg`。
- **`requires_check` 条目**：开跑前空跑确认。
  - `workspace/IK`：能否到达；
  - `drawer_clearance`：物体离抽屉太近；
  - `drawer_interior`：碗偏移是否超出抽屉内部。碗的偏移范围（dx ±6、dy ±8 cm）是暂定值，量好抽屉内尺寸后需复核。
  
  确认不可行的，在 `excluded_conditions.csv` 登记原因后，
  用同档位同规则重新采样替换，同一位置不能换掉两次以上。论文中报告替换了几条、原因是什么。

## 4. 评测范围与优先级

每个方法的 P1 共 120 次：

| 类别 | 档位 | 条数 | 规则 |
|---|---|---|---|
| ID | `id_unseen` | 30 | 物体在训练范围内、但不在训练原位；碗在 home |
| ID（P2，可选） | `id_seen` | 10 | 训练原位，作为记忆上限 |
| OOD | `obj_pos` | 15 | 物体在训练范围外 2–6 cm（随机方向）；碗在 home |
| OOD | `bowl_pos` | 15 | 物体取 ID 位置；碗随机偏移 ≥4 cm |
| OOD | `obj_bowl_pos` | 15 | 物体和碗同时在训练范围外 |
| 抗干扰 | `pull_drawer` / `displace_object` / `move_bowl` | 各 15 | 在 `id_unseen` 前 15 个条件上执行，与同条件的无干扰 ID 试验配对比较 |

**试跑（pilot，2026-11-25 用户决定）**：先用小规模验证并跑通整套流程，每个方法 35 次：
- ID `id_unseen` 前 10 条（C001–C010）；
- OOD 只做 `obj_bowl_pos` 前 10 条（C071–C080）；
- 三种抗干扰各 5 次，使用 C001–C005。

```
python scripts/eval_success.py run-order --conditions <v3 表> --methods <方法> --seed <种子> \
  --take id_unseen:10,obj_bowl_pos:10 --perturb-n 5 --out <eval_dir>/trials.csv
```

试跑条件是完整评测条件的子集，扩大规模时原样包含在内。n=10 时 Wilson 区间约 ±28 个百分点，n=5 时约 ±35–40 个百分点，
所以试跑结果只用于检验流程和粗略看趋势，不作为论文结论；如果写进论文，要明确标注为 pilot。

抗干扰的触发按阶段事件，不按时间；参数按序号交替，事先写入执行表的 `perturb_param` 列：

| `perturb_type` | 触发 | 动作 | `perturb_param` |
|---|---|---|---|
| `pull_drawer` | 机械臂已开始运动、尚未接触抽屉把手 | 手动把抽屉再拉出一段，改变把手位置 | `drawer_pull_cm=+5` / `+10` |
| `displace_object` | 阶段 1 完成（抽屉已开）后、夹爪闭合前 | 将物体沿 base 系 y 平移，朝向不变 | `object_dy_cm=+5` / `-5` |
| `move_bowl` | 阶段 2 完成（已夹住物体）后、松爪前 | 在抽屉内横向挪动碗 | `bowl_dy_cm=+5` / `-5` |

每次干扰由同一个人用同样的方式施加，记录 `perturb_applied`（是否真的施加）、`perturb_t_s`（相对试验开始的秒数）和 `recovery_time_s`（扰动后到成功的时间）。
策略没走到触发阶段的，`perturb_applied=0`，这次试验本身已经失败。干扰施加后，物体或碗出现超出参数的偏差的，在 `notes` 中记下实际位移。

## 5. 执行顺序

- 用 `scripts/eval_success.py run-order` 生成执行表，种子单独登记。
- 条件整体随机排序；同一条件下，各方法按随机顺序连续执行，所有方法共用一次摆放，以减少摆放误差，并消除时间漂移带来的偏差。
- 次数事先固定，不得因为中途结果提前停止或追加。
- 每次试验前记录日期时间、操作员和判定员。判定员尽量不知道当前运行的是哪个方法，执行表的 `method` 列只由操作员查看。

## 6. 排除与重跑

- 只有**非策略原因**才能排除：急停误触、相机掉线、B 端服务崩溃、摆放错误、外部人员碰撞。
  策略自身的碰撞、卡住、超时一律算失败，不能排除。
- 排除的试验填 `excluded=1` 和 `exclude_reason`。在执行表末尾追加一行，`rerun_of` 填原 `trial_id`，用同一条件重跑。
- 论文中报告每个方法的排除次数和原因。

## 7. 每次试验的记录

执行表 `trials.csv`（由脚本生成，现场逐行填写，原始文件只追加不改，修正写在 `notes` 中）：

| 列 | 内容 |
|---|---|
| 预填 | `trial_id`、`run_index`、`method`、`cond_id`、`split`、`axis`、`level`、`priority`、`paired_ref`、`perturb_type`、`perturb_trigger`、`perturb_param`、物体摆放坐标和偏航、`bowl_dx_cm`、`bowl_dy_cm`、`drawer` |
| 开跑填 | `datetime`、`operator`、`judge`、`checkpoint_sha256`、`code_commit`、`config_sha256`、`recording_id` |
| 结果 | `success`（0/1）、`stage_reached`（0–3）、`failure_mode`、`duration_s`、`intervention`（0/1）、`perturb_applied`、`perturb_t_s`、`recovery_time_s` |
| 排除 | `excluded`、`exclude_reason`、`rerun_of` |
| 证据 | `video_path`、`photo_path`、`notes` |

`failure_mode` 取以下之一：`drawer_fail`（没拉开抽屉）、`grasp_miss`（没抓到）、`grasp_drop`（抓起后掉落）、`place_miss`（没放进目标）、
`collision`（碰撞且无法继续）、`stall`（停住或来回振荡直到超时）、`other`（在 `notes` 中说明）。

## 7a. 过程录像与检查图

- **每次试验都要完整录制**，包括失败的和被排除的试验。
  - 用项目的录制功能，在启动策略前开始录制，判定完成后保存，录制目录名填入 `recording_id`。录制本身保存两路相机的原始帧、时间戳、每路的 `video.mp4`、命令和状态。
  - 不走项目录制的（例如仿真录屏、外置相机），把视频文件路径填入 `video_path`。
  - `duration_s` 从策略启动开始计时，不从录制开始计时。
- 每个阶段结束后统一生成证据：

  ```
  python scripts/eval_success.py evidence --trials <eval_dir>/trials.csv --data-root <录制根目录> --out <eval_dir>/evidence --gripper-events
  ```

  每次试验生成 `evidence/<trial_id>/` 目录，内含：
  - `overview.mp4`：两路相机按时间戳同步并排，叠加 trial 编号和时间；有 ffmpeg 时为 H.264；
  - `contact_sheet.jpg`：每路相机一行，列为首帧 / 中间帧 / 尾帧，加 `--gripper-events` 时再加闭爪和松爪时刻；
  - `evidence.json`：使用了哪些帧（序号、时间）、输入和输出的哈希、脚本哈希。
- 检查图和总览视频默认**不写方法名**，复判员只看 `evidence/` 做盲评。
- 统计时传入 `--evidence`，会列出缺少检查图的有效试验。论文定稿前必须补齐。
- 原始录制只读，不在录制目录中写入任何文件；证据只写在 `evidence/` 目录下。

## 8. 统计分析（预先确定）

由 `scripts/eval_success.py analyze` 生成，输出 `results.json` 和 `results.md`（论文表格），并记录输入哈希。
- 每个方法 × 每个档位：`k/n` 和 Wilson 95% 置信区间。
- 方法之间：在同一组条件上用 McNemar 精确检验（两侧）。
- 同一方法内与 ID 比较：
  - 抗干扰组与其 `paired_ref` 对应的 ID 试验配对，用 McNemar 检验；
  - OOD 组与 ID 组不配对，用 Fisher 精确检验。
- 多重比较：每一族检验（方法之间一族，与 ID 比较一族）分别做 Holm 校正，报告原始 p 和校正后 p，显著性水平 α = 0.05。
- 抗干扰组同时报告**条件成功率**（只统计 `perturb_applied=1` 的试验）和**意向成功率**（统计所有计划扰动的试验）。
- 附加报告：各阶段的到达率（漏斗）、失败模式计数、成功试验耗时的中位数，以及排除和替换的统计。

## 9. 开跑前待定项

- [ ] 抽屉开度：阶段 1 的阈值，以及训练示范中抽屉初始开度的测量（相机首帧或现场测量）。
- [ ] 碗的 home 位置标记、抽屉内尺寸，并据此复核碗偏移范围（暂定 dx ±6、dy ±8 cm）。
- [ ] 碗定位标定：确定 `drawer_ref`，完成 `bowl_calibration.json`（仅机械臂模式需要）。
- [ ] 机械臂摆放辅助脚本（悬停定位、保存参考图）；当前 AGENTS.md 只授权仿真，真机运行需另行授权。
- [ ] `pull_drawer` 的拉出量（+5 / +10 cm）是否会超出抽屉行程。
- [ ] 对比方法列表、各方法的 checkpoint 哈希，以及执行表种子。
- [ ] `requires_check` 条目完成空跑确认（v3 中：`workspace/IK` 30 条、`drawer_interior` 30 条、`drawer_clearance` 0 条，有重叠）。物体 OOD 靠近 y 方向桌边，需重点确认。
- [ ] 判定员和复判员人选。
- [ ] 本次在仿真还是真机上运行（AGENTS.md 当前只授权仿真）。仿真时把"摆放"换成场景脚本设定物体位姿，并记录场景种子。
