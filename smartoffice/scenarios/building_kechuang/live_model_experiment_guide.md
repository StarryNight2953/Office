# Qwen3.6-35B Building L3 实验执行指南

## 实验目的

首轮真实模型实验用于暴露 Controller、Prompt、工具接口、长周期规划、物理反馈和
终止行为的问题，不以追求 20/20 成功率为目标。Oracle 结果只作为确定性参考，不向
模型提供 Oracle 工具序列。

默认服务：

```text
endpoint: http://100.115.106.71:8000/v1
model: Qwen3.6-35B-A3B-FP8
provider: vllm
controller: building_baseline_react
parallel_tool_calls: false
thinking: false
```

如果 `/v1/models` 返回的模型 ID 与上述名称不同，必须通过 `--model` 使用服务实际
返回的 ID，不要在客户端猜测别名。

## 1. 模型启动后的预检

预检同时验证 `/models` 和一次带严格参数的 function call：

```bash
conda run -n are env PYTHONPATH=. python scripts/building_l3_live_runner.py \
  --preflight-only
```

仅能普通聊天但不能返回 `ping_tool` 的服务不能进入 Scenario 实验。

## 2. 单场景 Smoke

Smoke 使用 L3-06，检查传感观测、人数变化、时间推进、环境控制和日终停止：

```bash
conda run -n are env PYTHONPATH=. python scripts/building_l3_live_runner.py \
  --stage smoke \
  --output-root runs/building_qwen_l3_smoke
```

## 3. 诊断批次

诊断批次包含 L3-06、11、12、16、18、20，依次覆盖占用反馈、缩小/升级房间、功率
限制、打印队列和多承诺综合规划：

```bash
conda run -n are env PYTHONPATH=. python scripts/building_l3_live_runner.py \
  --stage diagnostic \
  --output-root runs/building_qwen_l3_diagnostic
```

相同命令默认复用已有 `run_report.json`，可在中断后继续；使用 `--rerun` 强制重跑。

## 4. 全量 20 场景

只有 Smoke 和诊断批次的工具协议稳定后才运行：

```bash
conda run -n are env PYTHONPATH=. python scripts/building_l3_live_runner.py \
  --stage all \
  --output-root runs/building_qwen_l3_all
```

## 5. 结果查看顺序

1. `summary.md`：批次状态、停止原因、Tool/LLM/Token 和 Validation；
2. `<scenario>/<scenario>.run_report.json`：最终指标与错误；
3. `<scenario>/<scenario>.progress.jsonl`：运行时心跳，适合观察卡住位置；
4. `<scenario>/<scenario>.runtime_log.jsonl`：完整 LLM 与工具调用事件；
5. `<scenario>/<scenario>.agent_workflow.json`：Agent 实际工作流；
6. `<scenario>/stdout.log`、`stderr.log`：进程级诊断。

## 6. 首轮重点记录的问题

- 是否误把已接受命令视为环境已经达标；
- 是否在计划事件发生前提前使用未来信息；
- 是否正确响应换房、提前或设备需求变化；
- 是否重复发送相同设备命令；
- 是否无限执行 `advance_time` 或环境调节；
- 是否为满足功率限制而破坏有人环境；
- 是否按优先级和 deadline 处理打印；
- 是否在活动结束后释放预约、清空房间并关闭设备；
- 是否因上下文增长出现工具参数退化、遗忘或错误终止；
- Validation 失败时，失败原因是否与 trace 一致。

首轮不要同时修改 Prompt、Controller、物理参数和 Scenario。先保留失败 trace，再按
问题归属分批修改，否则无法判断改善来自哪一层。
