# 本机验收记录

验收日期：2026-11-24（北京时间）。以下先保留第一阶段通信验收，第二阶段 Building 接入结果见文末。

环境：Windows，CPython 3.11.14，SPADE 4.1.4，PyJabber 0.4.5；完整依赖见 `uv.lock`。

最终运行报告：[report.json](artifacts/acceptance-20261124T100447Z-34835e/report.json)。运行产物在当前机器的 artifacts 目录中，未纳入 Git。

| 验收 | 结果 |
|---|---|
| 完整真实进程验收 | 连续 3 轮，每轮 8 类，共 24/24 通过 |
| 正常任务路由 | 每轮每房间 10 条，共 60 条全部完成 |
| 独立进程 | 按 ready 文件核对实际解释器 PID，三个 Agent 各不相同 |
| 去重、状态隔离、非法任务 | 三轮均通过；重复写任务仅有一次执行记录；非法批次不影响后续合法请求 |
| 超时及迟到结果 | 三轮均通过；迟到结果不会覆盖 timed_out |
| 离线及重新上线 | 三轮均通过；健康房间正常完成；新解释器可执行新任务 |
| 生命周期 | 每轮所有受管进程正常退出，退出码均为 0；最终核查三轮共 15 个实际进程 PID，无残留 |
| 核心契约 unittest | 4/4 通过 |
| Ruff lint / 格式 | 通过 |
| 锁定环境核查 | `uv sync --locked --offline` 通过 |
| 根目录启动入口 | `python experiments/spade_poc/run.py demo` 实跑通过 |

可复现命令（项目根目录）：

```powershell
python experiments/spade_poc/run.py demo
python experiments/spade_poc/run.py acceptance --repeat 3
```

开发检查（在 `experiments/spade_poc` 目录）：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\ruff.exe check office_spade tests run.py
.\.venv\Scripts\ruff.exe format --check --quiet office_spade tests run.py
```

[通信样例日志](examples/communication.sample.jsonl) 从最终批次第一轮 K1315 的真实 ping 任务中提取，含协调者发送、房间接收、ACK、执行和结果返回事件。原始完整日志保留在上述报告同级的 run-1/2/3 目录。

上述第一阶段使用测试状态，未接 Building 工具或模型。本验收不代表任务持久化恢复、跨机器部署或新旧架构效率对照已经完成。

## 第二阶段：共享 Building 世界＋模拟模型接口

用户要求 vLLM 暂未就绪时先使用假模型。本阶段调用 SPADE-LLM 0.3.0 的模型适配、上下文及工具组件，模型端由本机脚本模拟 OpenAI-compatible API；报告标记为 `scripted_mock`。设备工具和传感器反馈来自原项目的 Building 仿真。

最终报告：[Building report.json](artifacts/building-acceptance-20261124T115359Z-d5b64f/report.json)。两轮各8项，共 **16/16 通过**。

| 检查 | 实际结果 |
|---|---|
| 单一共享世界 | 三个既有房间装配到一个 Runtime，由独立环境进程持有 |
| 房间权限与全局时钟 | 外房间设备写入、伪造房间身份、房间推进全局时钟均被拒绝 |
| 防止假完成 | 模拟模型只返回完成文字而没有执行时，设备核验失败，任务返回 failed |
| SPADE-LLM 工具闭环 | K1315 调用原 HVAC 工具设制冷24°C/3档，再调用设备查询工具并通过目标状态核验 |
| 物理反馈 | 两轮推进600秒后，观测温度均从28.00°C降到27.1531°C；这不表示已经达到24°C室温 |
| 两房间独立上下文 | K1316 另设制冷25°C/3档，K1315 保持24°C目标值 |
| 模型接口失败 | 注入 HTTP 503 后返回 failed，即使此前设备状态已经满足目标也不把失败写成成功 |
| API 协议和去重 | 每个正常任务仅有3次模型请求，依次收到0/1/2条工具结果；重发同一任务未重复调用模型或设备 |

配套检查：

- 10/10 单元测试通过，覆盖 Building 权限、工具 RPC 去重、共享时钟、消息协议和完成条件。
- 第一阶段通信回归8/8通过：[回归报告](artifacts/acceptance-20261124T115445Z-b9931f/report.json)。
- Ruff lint/格式和 `uv sync --locked --offline` 通过。
- 两轮所有受管进程退出码为0，10个实际解释器 PID 均无残留。
- [Building 样例日志](examples/building.sample.jsonl) 来自第一轮环境服务的真实工具执行、拒绝及仿真推进记录。

复现：

```powershell
python experiments/spade_poc/run.py building-demo
python experiments/spade_poc/run.py building-acceptance --repeat 2
```

后续填写真实模型服务配置后，可运行 `llm-demo`。当前配置缺失时该入口返回退出码2，不会自动以假模型代替。真实 vLLM 推理、完整 L1/L3 基准及真实硬件尚未验收。
