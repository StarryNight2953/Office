# Kechuang Building Scenarios

项目全貌、关键决策、实验结论和后续接手清单见
[`PROJECT_MEMORY.md`](PROJECT_MEMORY.md)。

Building 两层结构（L3 综合运行 → L1 可复用业务能力）的新拆分标准见
[`l3-to-l1-split-standard.zh.md`](l3-to-l1-split-standard.zh.md)。该标准以业务闭环确定
边界，不因时间、房间、初态或参数差异复制场景；配套诊断脚本输出来源变体和验证证据。
20 个 L3 的全量正式审查结果、12 个能力 L1、生成命令和维护流程见 [`l1/README.md`](l1/README.md)。
首轮多行为试验结果和温控反例见
[`l3-to-l1-diagnostic-results.zh.md`](l3-to-l1-diagnostic-results.zh.md)。

本包是科创空间智慧建筑场景的统一入口。房间静态事实来自
`smartoffice/configs/rooms/`，Scenario 只描述任务、时间线、工具路径和最终验证。

## 目录结构

```text
building_kechuang/
├── base.py                     # 三房间 Runtime 与 Apps 共享装配
├── meeting_lifecycle.py        # 可复用会议阶段与状态转换
├── scenario_room_booking.py    # 跨房间预约与选择
├── l1/
│   ├── manifest.json           # 人工审核后的正式 L1 合同
│   ├── base.py                 # 独立初态与业务终态验证
│   └── generated/              # 生成并注册的原生 L1 Scenario
├── l3/
│   ├── specs.py                # L3 Spec 与动态室外 Profile
│   ├── catalog.py              # 20 个场景的声明式目录
│   ├── base.py                 # 共享工作流、Oracle 与最终验证
│   └── scenarios.py            # 20 个独立注册的薄 Scenario 类
├── k1324/
│   ├── scenario_climate_coordination.py
│   └── scenario_occupancy_ramp.py
├── k1316/                      # 研讨室专属场景入口
│   └── scenario_seminar_standard.py
└── k1315/
    └── scenario_conference_standard.py
```

## 当前可运行场景

20 个 L3 已全部完成候选处置并归并为 12 个正式能力 L1；完整 ID、来源变体、审查账本和复现
命令见 [`l1/README.md`](l1/README.md)。下表是原有手写 Building 场景入口。

| Scenario ID | 房间范围 | 目标 |
|---|---|---|
| `scenario_building_kechuang_room_booking` | 跨房间 | K1315 冲突后选择 K1316，并证明 K1324 不可预约 |
| `scenario_building_kechuang_k1324_climate_coordination` | K1324 | 办公室温湿度观测反馈控制 |
| `scenario_building_kechuang_k1324_occupancy_ramp` | K1324 | 工作日人数变化、CO₂ 上升与空房恢复 |
| `scenario_building_kechuang_k1315_conference_standard` | K1315 | 12 人正式会议的环境、打印、灯光、投影音响与会后清理 |
| `scenario_building_kechuang_k1316_seminar_standard` | K1316 | 8 人研讨的环境预处理、投影准备、会中通风与会后清理 |

每个运行型场景同时包含：

1. Runtime Event Queue 中真实发生的时间事件；
2. `build_events_flow()` 中供 Agent/Oracle 执行的工具 DAG；
3. `validate()` 中不可由自然语言报告替代的最终状态判定。

## 20 个 L3 场景

当前新增 20 个独立注册的完整工作日 L3，统一使用：

```text
Observe → Reconcile → Prioritize → Act → Wait → Verify → Commit → Monitor
```

共享工作流不是一条只在开头读取状态、结尾统一关机的固定长链。它把完整工作日合并为
一组有业务含义的检查点：占用变化、按运行模式提前 30/60/90 分钟准备会议、会议开始/结束、用户计划
修改、打印提交/截止、小时巡检和日终审计。每个检查点依次完成：

```text
推进到检查点
→ 读取已经发生的 Building Events
→ 读取传感器
→ 根据当前占用、会议承诺、天气和资源限制更新局部策略
→ 仅发送发生变化的设备命令
→ 推进 10 分钟物理时间并复查
→ 高负载首次出现时增强通风/净化，再等待 5 分钟复查稳定性
```

打印任务按各自的提交时刻和 deadline 分批执行；答辩场景会按材料准备、报告、问答、
闭门讨论和总结阶段切换照明/会议设备模式。功率受限场景优先保障会议房间，严格无人
能耗场景则由独立预算进行验收。

| 编号 | 场景族 | 数量 | 主要变量 |
|---|---|---:|---|
| L3-01～05 | 季节与室外环境 | 5 | 高温高湿、寒冷干燥、过渡季、太阳峰值、室外 PM2.5 |
| L3-06～10 | 空间、占用与活动 | 5 | 分批到达、连续研讨、答辩、并行会议、三房间高占用 |
| L3-11～15 | 用户互动与重规划 | 5 | 换小房、换大房、提前、追加设备、提前结束 |
| L3-16～20 | 资源约束与权衡 | 5 | 功率、无人能耗、打印、冬季通风、综合优先级 |

注册 ID 采用
`scenario_building_kechuang_l3_<两位编号>_<slug>`；完整 ID、研究问题和指标见
`l3/catalog.py`。它们共享同一个执行内核，但 Spec 分别定义动态天气、人数时间线、
预约、运行中互动、设备策略、打印批次、功率约束和 Primary Metrics。

Runtime 的未来事件不会出现在初始 briefing；Agent 在每个阶段通过
`BuildingWorldApp.get_recent_building_events()` 只读取已经发生的事件，再进行局部
重规划。所有 L3 在结束时统一验证物理演化、会前设备 readiness、预约释放、人员
清空、打印 deadline、CO₂/PM2.5、有人舒适度、无人能耗、功率约束和设备关闭。

当前 20 个 Oracle DAG 的节点数为 58–173，中位数为 91.5。农业 full-season L3 的
214–383 步包含大量按日推进和田间分块操作，不能直接作为建筑场景的步数下限；建筑
L3 以闭环覆盖和必要决策数量为准，禁止通过重复查询或日终逐设备关机人为填充步数。

需要注意：Oracle DAG 是可回放的参考策略，因此控制分支由 Spec、占用和已知约束
确定；传感器读取在 DAG 中形成真实的物理复查，但 capture 阶段不能依据未来工具返回
值动态改写 DAG。真实模型运行时的 Building Controller/Agent 才负责根据实际传感值
选择是否继续调节。后续可在独立 Controller 中加入阈值、迟滞和最大反馈迭代。

本轮批判性代码审查、已修复问题及仍需校准的边界见
`l3_implementation_audit.md`。

候选场景与优先级见 `scenario_catalog.md`。
真实模型运行历史见 [`experiment_status.md`](experiment_status.md)；2026-08-25 的 20 个
L3 全量逐场结果与问题归因见
[`l3_live_model_audit_20260825.md`](l3_live_model_audit_20260825.md)。

## 命名迁移

本次重构删除了 `smartoffice/scenarios/building_k1324/` 源码目录，不保留容易造成房间归属
误解的旧注册 ID。调用脚本需要按下表更新：

| 旧 ID | 新 ID |
|---|---|
| `scenario_building_k1324_meeting_booking` | `scenario_building_kechuang_room_booking` |
| `scenario_building_k1324_conference_standard` | `scenario_building_kechuang_k1315_conference_standard` |
| `scenario_building_k1324_climate_coordination` | `scenario_building_kechuang_k1324_climate_coordination` |
| `scenario_building_k1324_occupancy_ramp` | `scenario_building_kechuang_k1324_occupancy_ramp` |

## 测试与真实模型运行

导出 20 个 L3 的人工审核 Markdown 和完整 JSON：

```bash
conda run -n are env PYTHONPATH=. python -m \
  smartoffice.scenarios.building_kechuang.export_l3_workflows
```

默认入口为 `workflow_exports/building_kechuang_l3_review/README.md`。该目录属于生成物，
已由 `.gitignore` 排除；修改场景后重新运行命令即可刷新。每份 Markdown 中由
`REVIEW_NOTES` 标记包围的人工勾选和审核意见会在重新导出时保留。

```bash
conda run -n are env PYTHONPATH=. pytest -q tests/test_building_*.py
```

真实模型实验使用分阶段运行器，先验证模型发现与 function call，再依次运行 smoke、
诊断批次和全量 20 场景。命令、输出文件和故障分类见
[`live_model_experiment_guide.md`](live_model_experiment_guide.md)。

```bash
conda run -n are env PYTHONPATH=. python scripts/building_l3_live_runner.py \
  --preflight-only
```
