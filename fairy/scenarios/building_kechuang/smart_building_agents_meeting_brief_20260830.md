# Smart-building Agents 交流展示 Brief

日期：2026-08-30  
对象：黄琪琪、张兴宇、吴豪  
建议形式：30-60 分钟线上会议，屏幕共享为主，不准备正式 PPT

## 1. 会议目标

这次交流的目标不是做一次完整汇报，而是让三位同学快速进入项目语境，并引导他们从研究和工程两个角度提出新想法。

会后希望他们能理解：

1. 智慧楼宇 Agent 项目正在解决什么问题；
2. 它如何从 FAIRY 农业场景扩展而来；
3. 当前系统已经实现了哪些模块；
4. 多房间、多楼宇、近实时事件处理为什么会变难；
5. 团队如何使用 vLLM、GPU 服务器、Orin/Thor 等设备运行和评测大模型；
6. 接下来可以在哪些方向参与。

## 2. 推荐会议节奏

30 分钟版本：

| 时间 | 内容 | 展示材料 |
|---:|---|---|
| 0-5 min | 项目背景：从农业 FAIRY 到智慧楼宇 Agent | 本文件 + `PROJECT_MEMORY.md` |
| 5-12 min | 当前代码架构：Scenario、App、Runtime、Physics、Sensor | `fairy/scenarios/building_kechuang/README.md` |
| 12-20 min | 演示一个完整场景 | 建议 L3-18 或 K1315 conference |
| 20-25 min | vLLM/模型部署与 Profiling 条件 | runner、日志、vLLM 命令 |
| 25-30 min | 抛出开放问题，收集想法 | 本文件第 9 节 |

60 分钟版本：

| 时间 | 内容 | 展示材料 |
|---:|---|---|
| 0-8 min | 研究背景与总体目标 | 项目记忆文档 |
| 8-18 min | FAIRY 继承关系：农业 L1/L2/L3 到 Building L3 | L3 spec/catalog |
| 18-30 min | 代码工作流细讲：工具、Runtime、Event Queue、物理仿真 | `runtime.py`、`system.py`、`l3/base.py` |
| 30-40 min | 场景演示：会议请求、房间选择、设备准备、环境调节、事件打断 | L3-18 或 L3-14 |
| 40-50 min | 实验环境：vLLM、Qwen、GPU 服务器、Orin/Thor | live runner 与服务日志 |
| 50-60 min | 讨论下一步：多房间、多楼宇、近实时、通用框架 | 问题清单 |

## 3. 一句话开场

可以这样开场：

> 我们原来用 FAIRY 做农业全季节 Agentic Scenario，现在想把同一套“环境仿真 + 工具调用 + 事件队列 + 长周期验证”的思想迁移到智慧楼宇。智慧楼宇的核心差异是：用户互动更频繁，设备更多，事件响应更接近实时，而且房间之间会争夺资源，比如会议室、打印机、功率和空气质量。

## 4. 当前项目架构怎么讲

建议按五层讲，不需要展开所有代码：

```text
Scenario layer
  描述任务、时间线、隐藏用户事件、Oracle 工具流、最终验证

BuildingWorldApp layer
  暴露 Agent 可调用工具：房间、预约、人员、传感器、HVAC、灯光、投影、打印等

Runtime layer
  负责时间推进、事件队列、事件边界唤醒、打印完成等

Physics layer
  模拟温度、湿度、CO2、PM2.5、设备影响和传感器观测

Adapter / Simulator layer
  连接真实传感器或 MQTT sensor simulator
```

关键代码入口：

| 文件 | 展示重点 |
|---|---|
| `fairy/scenarios/building_kechuang/PROJECT_MEMORY.md` | 当前状态总览 |
| `fairy/scenarios/building_kechuang/README.md` | 场景目录和 L3 说明 |
| `fairy/scenarios/building_kechuang/l3/catalog.py` | 20 个 L3 的声明式定义 |
| `fairy/scenarios/building_kechuang/l3/base.py` | 共享 L3 工作流、Oracle、验证逻辑 |
| `fairy/apps/building_world/building_world_app.py` | BuildingWorld 工具集合入口 |
| `fairy/apps/building_world/runtime.py` | Runtime 和 Event Queue 接入 |
| `fairy/apps/system.py` | `advance_time()` 如何触发 domain runtime |
| `scripts/building_l3_live_runner.py` | 调用 vLLM 真实模型跑批量场景 |

## 5. FAIRY 从农业到智慧楼宇的继承关系

农业场景已有的思想：

- L1：原子技能或短动作；
- L2：局部反馈闭环；
- L3：跨长时间、多阶段、多决策的完整场景；
- Scenario 不只是 prompt，还包括可回放工具流和最终验证；
- Agent 需要在环境变化中持续观测、决策、执行和修正。

智慧楼宇继承这些思想，但换成新的 domain：

| 农业 FAIRY | 智慧楼宇 FAIRY |
|---|---|
| 天气、土壤、作物、病虫害 | 室外天气、房间热湿环境、CO2、PM2.5 |
| 灌溉、施肥、农药、收获 | HVAC、新风、灯光、投影、打印、预约 |
| 农事阶段 | 工作日、会议阶段、人员进出、用户计划变化 |
| 全季节 L3 | 完整工作日或多活动 L3 |
| 农业物理/生长过程 | 室内环境物理仿真和设备响应 |

智慧楼宇更强调：

- 人和设备的高频互动；
- 多房间共享资源；
- 事件打断和重规划；
- 舒适度、空气质量、能耗和会议承诺之间的权衡。

## 6. 当前房间设定

| Room | 当前设定 | 展示重点 |
|---|---|---|
| K1324 | 研究生办公室，17 人容量，不可预约；包括两个老师独立办公室、一个三人工位隔间、12 个研究生工位 | 长时间有人办公、CO2 上升、舒适度与能耗 |
| K1316 | 研讨室，8 人容量，可预约 | 小型研讨、投影/白板/视频会议、连续预约 |
| K1315 | 会议室，20 人容量，可预约 | 正式会议、答辩、投影、音响、麦克风、打印、分区灯光 |

目前物理上每个房间仍是简化热区。后续如果要更真实，K1324 可以进一步拆为老师办公室、三人工位隔间、研究生开放工位等多个 zone。

## 7. 建议演示场景

首选演示：L3-18 `printing_material_coordination`

推荐原因：

- 复杂度适中，容易讲清楚；
- 覆盖会议预约、打印材料、deadline、设备准备、环境控制；
- 比纯环境控制更能体现“楼宇 Agent 不只是调空调”；
- 目前真实模型已通过该场景，演示风险较低。

可以这样讲它的故事：

```text
用户计划在会议室开会
  -> Agent 检查房间和预约
  -> 会议前读取人员和环境状态
  -> 准备投影/灯光/会议设备
  -> 提交打印材料任务
  -> Runtime 推进时间，打印机按优先级完成任务
  -> 人员进入房间，CO2 和温湿度随时间变化
  -> Agent 继续读取传感器并调节 HVAC/新风/净化
  -> 会议结束后释放预约、关闭设备、恢复空房状态
  -> Validator 检查环境、设备、打印 deadline、能耗等硬条件
```

备选演示：

| 场景 | 适合讲什么 |
|---|---|
| L3-14 equipment requirement added | 用户临时追加设备需求，测试事件打断和重规划 |
| L3-11 meeting downsize | 会议人数变少后换小房，测试预约修改 |
| L3-10 three-room high occupancy | 三房间同时高占用，展示 CO2 控制难点 |
| K1315 conference standard | 单会议室标准工作流，最容易给新同学入门 |

## 8. 可直接展示的命令

基础测试：

```bash
conda activate are
PYTHONPATH=. pytest -q tests/test_building_*.py
```

导出 L3 工作流给人工审查：

```bash
conda run -n are env PYTHONPATH=. python -m fairy.scenarios.building_kechuang.export_l3_workflows
```

查看真实模型服务是否可用：

```bash
curl http://100.115.106.71:8000/v1/models
```

当前 Tailscale/vLLM endpoint 曾经变化，展示前用实际 IP 替换。

FAIRY live runner 预检：

```bash
conda run -n are env PYTHONPATH=. python scripts/building_l3_live_runner.py \
  --preflight-only \
  --endpoint http://100.115.106.71:8000/v1 \
  --model Qwen3.6-35B-A3B-FP8
```

运行一个 smoke 场景：

```bash
conda run -n are env PYTHONPATH=. python scripts/building_l3_live_runner.py \
  --stage smoke \
  --endpoint http://100.115.106.71:8000/v1 \
  --model Qwen3.6-35B-A3B-FP8 \
  --output-root runs/building_qwen_l3_smoke_demo
```

运行全量 20 个 L3：

```bash
conda run -n are env PYTHONPATH=. python scripts/building_l3_live_runner.py \
  --stage all \
  --endpoint http://100.115.106.71:8000/v1 \
  --model Qwen3.6-35B-A3B-FP8 \
  --output-root runs/building_qwen_l3_all_demo
```

查看最近一次完整结果：

```bash
sed -n '1,80p' runs/building_qwen36_35b_l3_all_final_20260828/summary.md
```

## 9. 当前实验结论怎么讲

最新记录的完整 live run：

```text
Run: runs/building_qwen36_35b_l3_all_final_20260828
Model: Qwen3.6-35B-A3B-FP8
Endpoint: http://100.115.106.71:8000/v1
Result: 17/20 passed
Tool calls: 2579
LLM calls: 2597
Tokens: 110,388,889
Wall time: about 2 h 29 min
```

失败解释要讲准确：

| Scenario | 暴露问题 | 分类 |
|---|---|---|
| L3-10 | 高占用下 CO2 控制太晚，最终 K1315 CO2 超标 | 模型控制/自检能力问题 |
| L3-16 | 长流程中上下文超过 128k，vLLM 返回错误 | Controller/runtime 上下文管理问题 |
| L3-20 | 多承诺长流程中上下文超过 128k | Controller/runtime 上下文管理问题 |

不要简单说“模型只能完成 17 个”。更准确的说法是：

> 当前系统在全量 L3 live run 中完成了 17 个；1 个暴露模型对高 CO2 风险的前瞻控制不足；2 个主要被 controller context overflow 截断，说明下一步要做上下文压缩和阶段摘要。

## 10. Event Queue 现在怎么讲

当前 ARE/Event Queue 的状态可以这样解释：

```text
SystemApp.advance_time()
  -> 推进统一模拟时钟
  -> 调用 BuildingWorldRuntime 的 time hook
  -> Runtime 按固定步长推进室内物理仿真
  -> pop_due() 取出已经到达的 BuildingEvent
  -> event handler 把事件应用到世界状态
  -> trigger policy 判断是否需要唤醒 Agent
  -> 若 stop_on_event=True，在事件边界返回给 Agent
```

这套设计已经能支持分钟级仿真和“推进到事件边界再响应”。但它不是严格近实时系统。

后续要讨论的 ARE Event Queue 升级方向：

- 从 `advance_time()` 驱动变成独立 domain event loop；
- 支持秒级或亚分钟级 tick；
- 支持外部 MQTT/HTTP/WebSocket 事件直接入队；
- 区分 hard interrupt、soft notification、background telemetry；
- 增加事件优先级、去重、合并、过期和取消；
- 让 Agent 可以维护长期计划，但在新事件到来时局部重规划；
- 保留农业和楼宇都能使用的通用接口。

## 11. 讨论问题清单

建议不要一次问完，而是在演示过程中穿插。

研究问题：

- 智慧楼宇 Agent 的核心 benchmark 应该评价什么：舒适度、能耗、任务完成率、响应时间，还是综合指标？
- L3 场景是否应该更像真实一天，还是更像压力测试？
- 用户计划变化应该被看作异常，还是正常工作流的一部分？
- 如何防止 Agent 通过硬编码场景规则通过测试？
- 哪些 L2/L1 应该从 L3 trace 自动切分出来？

工程问题：

- 多房间扩展到多楼宇时，哪些配置必须 declarative/spec-driven？
- 房间设备能力、预约规则、权限规则如何避免写死在 Python 代码里？
- 真实传感器接入后，simulation truth 和 real observation 应该如何并存？
- MQTT sensor simulator 应该模拟到什么粒度才有价值？
- Controller 应该如何做阶段摘要和上下文压缩？

ARE 框架问题：

- 农业和楼宇是否能共享同一个 Event Queue 抽象？
- `advance_time()` 是否应该继续作为统一入口，还是拆成 domain runtime scheduler？
- 近实时场景下，Agent 应该每个事件都醒来，还是由 trigger policy 过滤？
- 如何记录可复现 trace，同时支持真实外部事件？

## 12. vLLM 与硬件环境展示

团队设备可按“能力层级”讲：

| 设备 | 适合承担的任务 |
|---|---|
| Raspberry Pi 5 | 轻量传感器采集、MQTT broker/client、小型边缘服务 |
| NVIDIA AGX Orin | 边缘推理、小模型部署、传感/控制闭环原型 |
| NVIDIA AGX Thor | 更强边缘推理、实时控制实验、多模态或更大模型测试 |
| GPU server | 较大模型推理、批量 Agent profiling、长上下文实验 |

vLLM 介绍重点：

- vLLM 可以部署 OpenAI-compatible API server；
- FAIRY 通过 OpenAI 风格接口调用本地/远端模型；
- live runner 会记录 tool calls、LLM calls、tokens、wall time 和 validation；
- Agent profiling 既看最终成功率，也看推理成本和长上下文稳定性；
- 当前暴露的问题之一正是长流程场景中的 context overflow。

基础 vLLM 启动形态：

```bash
vllm serve Qwen/Qwen3-8B --host 0.0.0.0 --port 8000
```

多 GPU tensor parallel 示例：

```bash
vllm serve Qwen/Qwen3-30B-A3B-Instruct-2507 \
  --host 0.0.0.0 \
  --port 8000 \
  --tensor-parallel-size 4
```

长上下文示例，需要结合显存谨慎设置：

```bash
vllm serve Qwen/Qwen3-30B-A3B-Instruct-2507 \
  --host 0.0.0.0 \
  --port 8000 \
  --tensor-parallel-size 4 \
  --max-model-len 131072
```

函数调用能力预检很重要。能普通聊天不代表能跑 Agent scenario。

## 13. Qwen/Qwen3.8 资料入口

会前需要确认团队实际使用的模型 ID。当前代码默认模型仍是：

```text
Qwen3.6-35B-A3B-FP8
```

如果会议中要讲 Qwen3.8，建议使用“Qwen3.8 相关权重/资料正在作为下一步模型候选”这个口径，并现场打开实际模型页确认模型名、上下文长度、量化格式和 vLLM 支持情况。

可发给同学的入口：

- Qwen 官方文档：https://qwen.readthedocs.io/
- Qwen vLLM 部署文档：https://qwen.readthedocs.io/en/latest/deployment/vllm.html
- Qwen GitHub：https://github.com/QwenLM/Qwen3
- Qwen Hugging Face 组织：https://huggingface.co/Qwen
- vLLM OpenAI-compatible server：https://docs.vllm.ai/en/latest/serving/openai_compatible_server/
- vLLM GitHub：https://github.com/vllm-project/vllm

## 14. 会中建议展示路线

屏幕共享可以按这个顺序走：

1. 打开 `PROJECT_MEMORY.md`，用 2 分钟讲当前项目状态；
2. 打开 `README.md`，展示 Kechuang 目录结构和 20 个 L3 场景族；
3. 打开 `l3/catalog.py`，说明 L3 是声明式 spec，不是复制 20 份硬编码脚本；
4. 打开 `l3/base.py`，说明共享工作流和 validator；
5. 打开 `runtime.py` 和 `system.py`，说明 Event Queue 与 `advance_time()`；
6. 打开 `scripts/building_l3_live_runner.py`，说明如何跑真实模型；
7. 打开 `runs/building_qwen36_35b_l3_all_final_20260828/summary.md`，展示 17/20 结果和问题分类；
8. 如果模型服务在线，跑 `--preflight-only` 或 `--stage smoke`；
9. 最后打开本文件第 11 节，进入讨论。

## 15. 会后可收集的反馈

建议会后让每位同学给出至少一个想法，按下面格式记录：

```text
姓名：
方向：研究 / 工程 / 硬件 / 实验 / 其他
建议：
为什么重要：
需要的数据或代码：
可以由谁继续推进：
```

特别希望收集的问题：

- 哪些真实楼宇交互最值得做成 L3；
- 哪些设备应优先接入；
- 如何设计跨楼宇 room/device schema；
- Event Queue 是否应该脱离 `advance_time()`；
- controller context overflow 应该如何压缩；
- vLLM profiling 应该记录哪些指标才足够支撑论文/报告。

## 16. 会前 Checklist

会议前 10 分钟检查：

- 确认三位同学的会议时间和会议链接；
- `conda activate are` 可以正常进入；
- `pytest -q tests/test_building_*.py` 至少最近一次通过；
- vLLM endpoint `/v1/models` 可访问；
- `scripts/building_l3_live_runner.py --preflight-only` 可运行；
- 准备好 `summary.md`，即使现场模型服务波动也能展示结果；
- 打开 Thor/Orin 或 GPU 服务器终端，准备展示 vLLM 命令和日志；
- 确认 Qwen3.8 或当前实际模型页面链接可打开；
- 准备一个空白文档记录三位同学的建议。

## 17. 会后可发消息模板

```text
今天交流的核心资料如下：

1. Kechuang Building 项目记忆：
   fairy/scenarios/building_kechuang/PROJECT_MEMORY.md

2. 智慧楼宇场景入口：
   fairy/scenarios/building_kechuang/README.md

3. 20 个 L3 场景定义：
   fairy/scenarios/building_kechuang/l3/catalog.py

4. 真实模型运行指南：
   fairy/scenarios/building_kechuang/live_model_experiment_guide.md

5. vLLM / Qwen 资料：
   https://qwen.readthedocs.io/en/latest/deployment/vllm.html
   https://github.com/QwenLM/Qwen3
   https://huggingface.co/Qwen
   https://docs.vllm.ai/en/latest/serving/openai_compatible_server/

欢迎继续补充你们觉得值得做成 L3 场景的真实楼宇工作流。
```
