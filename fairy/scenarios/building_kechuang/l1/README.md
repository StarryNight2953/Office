# Building L1 正式能力目录

本目录是 Building 两层结构中的正式 L1 层。L1 以可复用业务能力为单位：同一业务目标和同一主 object 只保留一个正式场景；执行时间、房间、设备初态、目标参数、队列和流程分支保存在覆盖变体中，不再复制 Scenario ID。

## 当前 12 个 L1

| 类型 | 正式能力 | 说明 |
|---|---|---|
| 单 object | hvac_control | 空调从任意合法初态调到需求配置并确认 |
| 单 object | ventilation_control | 新风机开关/档位控制并确认 |
| 单 object | humidifier_control | 加湿器控制并确认 |
| 单 object | air_purifier_control | 空气净化器控制并确认 |
| 单 object | lighting_control | 灯或灯组的开关、亮度、色温和场景控制 |
| 单 object | projector_control | 投影仪开关和输入源控制 |
| 单 object | audio_system_control | 音量与麦克风控制 |
| 单 object | printer_control | 需求、设备/队列检查、提交、等待和作业确认 |
| 业务闭环 | meeting_room_booking | 人员/日程/房间检查、选房、创建与确认 |
| 业务闭环 | meeting_requirement_change | 检查旧承诺、取消/释放或修订、重建与确认 |
| 业务闭环 | meeting_room_recovery | 会议结束后结合后续承诺恢复房间状态 |
| 业务闭环 | power_constrained_adjustment | 在共享功率预算内协调多个对象并复核 |

这 12 个能力承接全部 20 个 L3 的 252 个诊断候选、711 个候选内写动作、5 个公共打印机收尾动作和 389 条业务义务。来源出现次数是 coverage，不是 L1 数量。

## 文件职责

    l1/
    ├── manifest.json       # schema v2：12 个能力、代表夹具、来源变体与义务映射
    ├── manifest.py         # 结构、动作唯一覆盖与诊断来源校验
    ├── base.py             # 设备、打印、预约和需求变更的独立运行与终态验证
    └── generated/          # 12 个由 manifest 生成并注册的薄 Scenario 模块

generated/ 不应手工修改。能力身份、代表输入、来源映射、Oracle 或验收条件变化时，先刷新 manifest.json，再重新生成。

## 能力与变体

每个合同只有一个稳定 scenario_id，同时包含：

- source：用于执行 fresh-instance Oracle 的代表性实例；
- coverage.source_variants：所有 L3 候选/公共动作变体及其参数；
- coverage.obligation_refs：该能力承接的业务义务；
- coverage.varied_dimensions：允许变化但不改变 L1 身份的维度。

预约和需求变更在源 L3 中分别被预置或自动应用，所以它们使用 scenario_building_kechuang_room_booking 派生的补充夹具，通过真实 ScheduleApp、RoomApp 和 ResourceAllocationApp 工具验证。manifest 明确记录这一来源限制。

## 审查与生成

    python scripts/finalize_building_l1_review.py
    python scripts/finalize_building_l1_review.py --apply
    python scripts/generate_building_l1_scenarios.py --force --prune --validate --report workflow_exports/building_l3_to_l1_formal/generation_report.json

--prune 只删除不再存在于 manifest 且仍带自动生成标记的旧模块；遇到手工文件会拒绝删除。日常只检查一致性时使用：

    python scripts/generate_building_l1_scenarios.py --check --validate

按来源 L3 查看其涉及的能力仍受支持，例如：

    python scripts/generate_building_l1_scenarios.py --scenarios L3-06 --validate

## 验证边界

- 单 object 能力检查目标配置、无关设备、会议和预约未被破坏，并要求动作后的查询与报告。
- 打印能力检查 request/job 唯一关联、参数、完成事件、deadline、重复提交和结果查询。
- 预约能力检查会议与 active reservation 一致、冲突会议未被覆盖、设备未被误操作。
- 需求变更检查旧会议/预约已取消、替代承诺有效、无关承诺未被破坏。
- 会后恢复和共享功率调整保留多对象业务整体，不再按每台设备拆散。

代表 Oracle 验证一组具体输入；所有来源变体的参数与映射由 manifest/账本校验。若未来需要逐变体动态执行，应增加 case fixture 运行器，而不是重新把变体扩成大量 L1 ID。

## 测试

    python -m unittest tests.test_building_l1_generation -v
    python -m unittest tests.test_building_l1_diagnostic -v

正式测试覆盖 manifest schema、跨 L3 来源链接、716 个源动作的唯一能力映射、幂等重建、生成文件一致性、12 个 fresh-instance Oracle，以及 do-nothing、漏关键动作和漏确认负例。

## Qwen 真实模型手动实验

从仓库根目录运行。Runner 使用与 Building L3 相同的 `building_baseline_react`、vLLM、
串行工具调用、独立子进程和 Validation 汇总协议。默认 endpoint 和 model 与 L3 runner
一致，也可用 `--endpoint`、`--model` 覆盖。请先进入此前运行 L3 所用的 Python/Conda
环境；该环境需要能够 `import openai`。

先只做模型发现和 function-call 预检，不运行场景：

```powershell
python -m scripts.building_l1_live_runner --preflight-only
```

然后运行单场 HVAC smoke：

```powershell
python -m scripts.building_l1_live_runner `
  --stage smoke `
  --output-root runs/building_qwen_l1_smoke
```

建议在 smoke 通过后运行 6 场 diagnostic；它覆盖单设备控制、打印、预约、需求变更、
会后恢复和共享功率约束：

```powershell
python -m scripts.building_l1_live_runner `
  --stage diagnostic `
  --output-root runs/building_qwen_l1_diagnostic
```

若只想复测此前失败的通风控制和共享功率约束两个场景，可运行：

```powershell
python -m scripts.building_l1_live_runner `
  --stage repair `
  --output-root runs/building_qwen_l1_repair
```

若只想运行会议预约和会议需求变更两个端到端场景，可运行：

```powershell
python -m scripts.building_l1_live_runner `
  --stage meeting `
  --output-root runs/building_qwen_l1_meeting_fixed
```

最后运行 manifest 中全部 12 场：

```powershell
python -m scripts.building_l1_live_runner `
  --stage all `
  --output-root runs/building_qwen_l1_all
```

不传 `--output-root` 时，也会自动写入带 UTC 时间戳的
`runs/building_qwen_l1_<timestamp>/`。同一命令再次运行时会复用已有
`run_report.json`，适合中断后继续；要强制重跑已有场景可加 `--rerun`。已经单独完成
preflight 后，可在后续批次加 `--skip-preflight`。

常用的完整参数示例：

```powershell
python -m scripts.building_l1_live_runner `
  --stage all `
  --endpoint http://100.115.106.71:8000/v1 `
  --model Qwen3.6-35B-A3B-FP8 `
  --max-tool-calls 80 `
  --timeout-seconds 900 `
  --max-output-tokens 4096 `
  --output-root runs/building_qwen_l1_all
```

批次完成或中断后，先查看 `summary.md`。每个场景目录包含
`<scenario_id>.run_report.json`、`progress.jsonl`、`runtime_log.jsonl`、
`agent_workflow.json`、`stdout.log` 和 `stderr.log`。自然语言最终回答仅供阅读，是否通过
以 run report 中的 `evaluation.validation.success` 为准。
