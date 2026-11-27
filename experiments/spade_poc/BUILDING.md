# Building 工具与模型接口接入

这一阶段把 SPADE 房间 Agent 接到原项目的 Building 环境。默认使用本地模拟模型接口，便于在 vLLM 尚未部署时先验收工具闭环。

## 运行

在项目根目录：

```powershell
# 小演示：K1315 空调控制、设备核验、推进仿真、传感器复查
python experiments/spade_poc/run.py building-demo

# 完整验收：增加权限、假完成、两房间上下文和模型服务报错检查
python experiments/spade_poc/run.py building-acceptance

# 当前真实模型配置是否齐全；不会联网请求模型
python experiments/spade_poc/run.py model-readiness
```

每次启动一个独立实验，自动管理 XMPP、Building 环境、协调者、两个房间 Agent 及模拟模型 HTTP 服务；结束后关闭本轮进程与服务。报告和日志保存在 artifacts 下，终端输出确切路径。

本机已完成两轮16/16整体验收、10/10单元测试和第一阶段通信回归，实际记录见 [VALIDATION.md](VALIDATION.md)。

## 哪些部分已复用

| 部分 | 实现 |
|---|---|
| 房间配置 | 原 k1315/k1316/k1324 YAML，三个房间装配到同一个世界 |
| 环境与物理 | 原 BuildingWorldApp、BuildingWorldRuntime、温湿度和空气质量模型 |
| 业务工具 | 原 DeviceRegistryApp、HvacApp、LightingApp、BuildingSensorApp |
| 房间权限 | 原 RoomScopedToolset；服务端校验设备和分区归属 |
| Agent 通信 | 第一阶段已验证的 SPADE/XMPP 任务与结果消息 |
| 模型适配 | SPADE-LLM 的 LLMProvider、ContextManager、LLMTool |
| 当前模型 | 本机脚本实现的 OpenAI-compatible `/v1/chat/completions` 接口 |

原世界对象只存在于 Building 服务进程中。房间进程使用各自的访问令牌调用工具，不各自复制一个世界。模拟模型位于启动器的本机 HTTP 服务中，只按固定步骤返回调用：开空调、读设备状态、返回摘要。它不是训练出来的模型，也不理解任意自然语言任务。

Building 环境是原项目的仿真，设备动作与传感器读数由该仿真产生，尚未连接真实硬件。

## 一次任务怎样完成

1. Coordinator 通过 XMPP 给 K1315 发任务，要求空调制冷24度、风量3档，并给出可验收的设备目标。
2. K1315 从共享环境读取最新本房间状态，把任务、允许的工具和本房间信息送给模拟模型接口。
3. SPADE-LLM 收到工具调用，由房间 Agent 转交共享环境执行原 `HvacApp.set_hvac`。
4. 模型再请求读取设备状态，最后返回摘要。
5. 房间 Agent 重新读取共享环境，以真实设备状态逐项核验目标；仅有“已完成”文字不能通过。
6. 演示驱动器作为协调侧推进全楼仿真600秒，房间再读取传感器，检查温度变化。

24度是空调设定值，传感器温度会随物理仿真逐步变化。设备合同验收和实际室温达标是两个不同条件，报告分别记录。

## 状态、权限和执行限制

- 每个房间有独立的模型任务上下文；历史世界状态保存在同一个环境服务中。
- 房间访问令牌绑定房间，不能通过更改 room_id 读取别的房间。现有工具权限规则继续检查 device_id/zone_id。
- 房间工具未暴露全局时间推进；演示驱动器持有协调侧权限，统一推进共享时钟。后续复杂调度应继续通过这一权威入口。
- 同一任务重发返回缓存；服务端工具 RPC 的 call_id 也防止同一次调用被重复执行。去重仍限于本次进程生命周期。
- 每次模型任务最多8轮模型调用、12次工具调用、100秒处理时间；单次模型请求20秒，关闭自动重试。Coordinator 可为该任务设置120秒等待时间。
- 模型服务报错、预算耗尽或设备目标不满足均返回 failed。任务超时不会撤销已经执行的设备动作，需要查询当前世界状态。
- 当前记录模型调用次数、工具历史和耗时；`token_usage` 为 null，尚未完整采集 Token 与缓存统计，不据此声称成本或性能改善。

`llm_task` 的 payload 示例（设备 ID 应从当前房间 snapshot 获取）：

```json
{
  "operation": "llm_task",
  "task": "将本房间空调开启为制冷24度、风量3档，然后读取设备状态核验。",
  "expectations": [
    {"device_id": "从本房间状态中读取的空调ID", "power_on": true, "mode": "cooling", "target_temperature_c": 24.0, "level": 3}
  ]
}
```

当前目标核验支持设备开关、模式、目标温度、档位和灯光亮度。宽泛业务目标、会议重规划和完整 L3 验证器接入属于后续工作。

## 将来切换到你的 vLLM

在运行命令的同一终端设置真实地址、模型名称和必要的密钥：

```powershell
$env:OFFICE_LLM_BASE_URL = 'http://你的模型服务器:8000/v1'
$env:OFFICE_LLM_MODEL = '你的实际模型名称'
# 如果服务需要认证，再设置 OFFICE_LLM_API_KEY；无需写入源码。
python experiments/spade_poc/run.py model-readiness
python experiments/spade_poc/run.py llm-demo
```

`llm-demo` 使用外部配置，不会自动换成模拟模型。默认的 `building-demo/building-acceptance` 始终使用本地模拟模型，避免无意调用真实服务。

同样支持 `VLLM_API_BASE/VLLM_MODEL/VLLM_API_KEY` 作为回退配置；OFFICE_LLM 配置优先。真实模型必须支持 OpenAI-compatible 工具调用，vLLM 端的工具解析与模型模板需要按你的模型配置。当前尚未对真实 vLLM 做联网验收。
