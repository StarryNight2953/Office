# Building World 代码架构与工作流

本文是当前智慧建筑代码的总入口。目标是回答四个问题：

1. 一次请求在代码中如何流动；
2. 哪个对象拥有哪类状态；
3. 新增场景、设备、传感器或物理量时应修改哪里；
4. 当前哪些链路已经运行，哪些仍未连接。

K1324 传感器筛选、公共天气替代项及分阶段采购建议见
`fairy/configs/rooms/k1324_sensor_selection.md`。

### 开发环境

已有 Conda 环境 `are` 时，可在项目根目录执行：

```bash
conda activate are
python -m pip install -r requirements-building.txt
PYTHONPATH=. python -m pytest tests/test_building_*.py -q
```

三个房间配置均使用 JSON-compatible YAML，因此运行时不依赖 PyYAML。真实 MQTT
部署使用 `paho-mqtt` 建立连接、认证、订阅和重连，再把消息交给现有 Adapter。

## 1. 当前总体结构

```text
Scenario / Engine / Agent
          |
          | Tool call
          v
Building Apps ---------------------------------------------------+
  Room/Schedule/Occupancy/Device/HVAC/AirDevice/Sensor Apps      |
          |                                            |
          v                                            |
BuildingWorldApp                            SensorHub  |
  rooms / people / meetings                 ^    ^     |
  reservations / BuildingEvent              |    |     |
                                             |    |     |
                            simulated reading|    |real reading
                                             |    |
BuildingWorldRuntime                         |  Sensor Adapter
  device state -> EquipmentModel -> HvacCommand
  BuildingPhysicsOrchestrator                |  MQTT/BACnet/...
  IndoorEnvironmentEngine ------------------+
  BuildingObservationModel
  energy / comfort / PhysicsEvent
```

当前三个房间与环境感知均已装配到共享 Building Runtime。设备命令可通过
`BuildingWorldRuntime` 在 `SystemApp.advance_time()` 后形成物理效果和模拟观测；
Event Queue、第一版 Trigger Policy、Building 专用 system prompt 和
`building_baseline_react` 已接入。`SystemApp.advance_time()` 现在会在需要重规划的
Building 事件边界提前返回，并携带事件摘要；这实现的是 Tool 调用边界上的协作式
唤醒，不是异步抢占正在生成文本的模型。尚未完成的是 Controller 的显式有限状态、
迟滞终止策略和 Conference 长时自适应控制循环。

## 2. 分层职责

### 2.1 Scenario 层

目录：`fairy/scenarios/building_kechuang/`

Scenario 只负责：

- 组装本场景使用的 App；
- 设置初始房间、人员、日程和外部事件；
- 描述 Agent 任务；
- 定义 Oracle workflow；
- 验证最终结果。

Scenario 不应实现房间冲突、权限、容量判断或物理公式。这些规则必须进入可复用
App 或 Physics，否则换一个场景就会复制业务逻辑。

当前场景：

- `base.py`：K1324、K1316、K1315 房间、人员及 App 组装；
- `scenario_meeting_booking.py`：首选 K1315 冲突后预约 K1316；
- `meeting_lifecycle.py`：正常会议阶段、占用变化和会后资源释放；
- `scenario_conference_standard.py`：标准会议完整生命周期；
- `scenario_climate_coordination.py`：空调与加湿器的观测反馈协同；
- `scenario_occupancy_ramp.py`：计划人数变化下的 CO₂ 响应。

### 2.2 App 层

目录：`fairy/apps/building_world/`

| 组件 | 职责 | 是否可写状态 |
|---|---|---|
| `BuildingWorldApp` | 建筑领域唯一业务状态源、ID、事件和快照 | 是 |
| `RoomApp` | 查询房间和确定性冲突检查 | 否 |
| `ScheduleApp` | 会议查询、创建、取消及业务规则 | 通过 World 写 |
| `ResourceAllocationApp` | 原子预留和释放共享资源 | 通过 World 写 |
| `OccupancyApp` | 人员在校和位置查询/更新 | 通过 World 写 |
| `BuildingSensorApp` | 向 Agent 暴露规范化传感器读数 | 否 |
| `SensorHub` | 合并模拟与真实 Provider，处理来源优先级 | 只缓存读数 |
| `DeviceRegistryApp` | 查询设备能力、位置、健康和可用性 | 否 |
| `HvacApp` | 校验并修改 HVAC 命令状态 | 通过 World 写 |
| `AirDeviceApp` | 校验并修改加湿器/净化器状态 | 通过 World 写 |
| `VentilationApp` | 控制独立新风档位，映射为室外风量 | 通过 World 写 |
| `LightingApp` | 控制分区亮度、色温与场景 | 通过 World 写 |
| `MeetingEquipmentApp` | 控制投影、音响和麦克风并查询 readiness | 通过 World 写 |
| `PrintingApp` | 控制打印机并管理随仿真时间完成的打印任务 | 设备通过 World；任务由 App 写 |
| `BuildingOperationsApp` | 汇总运行边界、公开承诺、会议 readiness、环境闭环和功率预算，并提供终止/时间推进守卫 | 否；只记录协调检查位置 |
| `BuildingWorldRuntime` | 统一推进设备、负荷、物理、观测和事件 | 是 |

App Tool 返回结构化 `dict`。确定性违规通常返回：

```python
{
    "status": "rejected",
    "reason": "room_conflict",
    "conflicting_id": "existing-001",
}
```

LLM 不能通过换一种 Tool 参数绕过这些规则。

### 2.3 Physics 层

目录：`fairy/physics/building/`

| 组件 | 职责 |
|---|---|
| `models.py` | 区域参数、隐藏真值、HVAC 物理输入、能耗和舒适度结果 |
| `indoor_environment_engine.py` | 温度、湿度、CO₂、PM2.5 守恒计算 |
| `observation_model.py` | 从隐藏真值生成带噪声、延迟和缺失的模拟读数 |
| `sensor_api.py` | `SensorReading`、`SensorProvider`、`SensorPublisher` 稳定边界 |
| `orchestrator.py` | 推进物理、累计能耗、发布观测、生成阈值 transition |

关键边界：

```text
Tool / Device state -> HvacCommand -> Physics truth -> ObservationModel
                                                  -> SensorReading
```

Agent 不能读取 `ZoneState`，只能通过 `BuildingSensorApp` 读取
`SensorReading`。

### 2.4 Adapter 层

目录：`fairy/adapters/building/`

Adapter 将厂商/协议数据转换成统一 `SensorReading`：

```text
topic / point address / raw payload
        -> SensorPointMapping
        -> unit conversion
        -> timestamp + quality mapping
        -> SensorReading
```

`MqttSensorAdapter` 不负责建立网络连接。仓库中的 `MqttSensorSubscriber` 基于
paho-mqtt 负责认证、订阅和重连，并在回调中调用 `adapter.on_message(...)`；
`fairy.simulators.building` 可用当前物理 Runtime 生成数据并通过 Broker 做闭环联调。

## 3. 状态所有权

后续修改前应先确定目标状态属于哪一层。

| 状态 | 唯一所有者 | 其他组件如何访问 |
|---|---|---|
| 房间静态信息 | `BuildingWorldApp.rooms` | `RoomApp` 只读 |
| 人员及位置 | `BuildingWorldApp.people` | `OccupancyApp` 查询/更新 |
| 会议日程 | `BuildingWorldApp.schedule` | `ScheduleApp` 查询/更新 |
| 资源预留 | `BuildingWorldApp.reservations` | `ResourceAllocationApp` 更新 |
| 业务事件 | `BuildingWorldApp.events` | Scenario 验证/未来 Event Bus 消费 |
| 功能分区 | `BuildingWorldApp.functional_areas` | 房间配置与设备位置查询 |
| 通用设备设置 | `BuildingWorldApp.device_states.settings` | 设备 App 更新；Runtime 读取物理相关设置 |
| 打印任务 | `PrintingApp.jobs` | PrintingApp 查询并按仿真时钟完成 |
| 环境隐藏真值 | `IndoorEnvironmentEngine.states` | Orchestrator 推进 |
| 累计能耗 | `BuildingPhysicsOrchestrator` | step result/snapshot |
| 模拟传感器采样状态 | `BuildingObservationModel` | snapshot/restore |
| 最新读数 | Adapter / `SensorHub` | `BuildingSensorApp` 只读 |

禁止在 Scenario、SensorApp 或 ScheduleApp 中复制另一层的可变状态。

### 3.1 统一实验指标

`Engine.outcome_summary()` 会在 Building 场景中调用 `build_building_metrics()`，
并将结果写入 `run_report.json` 的 `outcome.building`。统一字段包括：

- 分区与总累计能耗；
- 舒适度和空气质量越界时长；
- CO₂/PM2.5 峰值和最低舒适度；
- Building 事件、Agent 唤醒和设备动作数量；
- 运行结束时仍开启的设备和未释放的预约；
- 房间、zone、设备和传感器规模；
- 启用 Shadow Mode 时各 zone/quantity 的 bias、MAE、RMSE、最大绝对误差和时间偏移。

`scripts/summarize_runs.py` 可将这些指标与 LLM 调用、token、任务成功率一起汇总为
TSV 或 CSV，供跨场景和跨 Controller 比较。

当前空间模型如下：

| 房间 | 类型 | 容量 | 可预约 | 功能结构 |
|---|---|---:|---|---|
| K1324 | 研究生办公室 | 17 | 否 | 两间单人教师办公室、一间三人工位隔间、12 个研究生工位 |
| K1316 | 研讨室 | 8 | 是 | 研讨桌与演示/白板区 |
| K1315 | 会议室 | 20 | 是 | 演示区、前后参会区、会议服务区及完整会议设备 |

三个房间分别映射到 `k1324_office_zone`、`k1316_seminar_zone` 和
`k1315_conference_zone`。功能分区用于设备布置和业务推理；每个房间当前仍只使用
一个物理空气区，在有分区传感器或可靠流体参数之前不人为制造多个独立空气真值。
K1315 的 20 人容量、K1316 的 8 人容量和面积参数是当前仿真初值，取得现场尺寸后
应直接在相应房间配置中校正。

## 4. 会议预约完整工作流

入口类：`ScenarioBuildingKechuangRoomBooking`

```text
Engine.run_scenario_agent / replay_workflow
        |
        v
OccupancyApp.get_person_presence(professor-01)
        |
        v
ScheduleApp.get_person_schedule(...)
        |
        v
RoomApp.find_available_rooms(... capacity=6, projector)
        |
        +-- K1324: existing-001 时间冲突 -> 排除
        +-- K1315: 容量/投影不满足      -> 排除
        +-- K1316: 满足条件             -> 返回
        |
        v
ScheduleApp.create_meeting(room_id=k1316)
        |
        +-- 校验时间、人员、权限、容量、能力
        +-- 再次检查房间和人员冲突
        +-- ResourceAllocationApp.reserve_room()
        |       `-> RESOURCE_RESERVED (event-0001)
        +-- BuildingWorldApp.add_schedule_entry()
        |       `-> SCHEDULE_CREATED (event-0002, parent=event-0001)
        v
Scenario.validate()
        `-> 必须只有一个新会议，且房间为 k1316
```

会议时间使用带 UTC offset 的 ISO-8601 字符串。`datetime_utils.py` 会拒绝无时区
时间。时间段采用半开区间 `[start, end)`，因此 14:00–15:00 与 15:00–16:00
不冲突。

### Oracle / Replay 调用

```python
from fairy.controllers.engine import Engine
from fairy.scenarios.building_kechuang.scenario_room_booking import (
    ScenarioBuildingKechuangRoomBooking,
)

oracle_engine = Engine(None, ScenarioBuildingKechuangRoomBooking())
oracle = oracle_engine.build_oracle_workflow(run_oracle=False)

replay_engine = Engine(None, ScenarioBuildingKechuangRoomBooking())
replayed = replay_engine.replay_workflow(oracle)
report = replay_engine.evaluation_report(replayed)
```

场景 ID：`scenario_building_kechuang_room_booking`。Registry 会扫描
`fairy.scenarios.building_kechuang` 及其房间子包。

## 5. 模拟环境与传感器工作流

```text
Device/occupancy/outdoor inputs
        |
        v
BuildingPhysicsOrchestrator.step(at_time, dt, ...)
        |
        +-- IndoorEnvironmentEngine.step()
        |      -> temperature / RH / CO2 / PM2.5 truth
        |      -> step energy / comfort
        |
        +-- BuildingObservationModel.sample(truth)
        |      -> bias + noise + missing + latency
        |      -> ObservationSample(reading, truth_value)
        |
        +-- observation_sink.publish_many(readings)
        |      -> SensorHub
        |
        +-- threshold transition
               -> PhysicsEvent

BuildingSensorApp -> SensorHub.read() -> Agent-visible SensorReading
```

`ObservationSample.truth_value` 只用于内部评估，`BuildingSensorApp` 序列化结果中
没有该字段。

## 6. 真实传感器与 Shadow Mode

```text
MQTT client callback
        -> MqttSensorAdapter.on_message()
        -> PushSensorAdapter.ingest()
        -> canonical SensorReading
        -> SensorHub.register_provider(priority=100)
        -> BuildingSensorApp
```

模拟读数一般使用 priority 0。真实 Provider 使用更高 priority 后，Hub 合并视图
选择真实读数。Shadow Mode 使用：

```python
hub.read_by_source(request)
```

同时取得 `published` 模拟读数与真实 Provider 读数，用于误差和标定分析。

时间字段：

- `observed_at`：设备采样时间；
- `available_at`：网关收到、允许对外提供的时间；
- `read()` 只返回 `available_at <= request.at_time` 的读数。

点位示例在 `fairy/configs/rooms/k1324_sensors.yaml`。

## 7. Snapshot 与 Replay

Runtime 组合快照包含：

```text
BuildingWorldRuntime.snapshot()
  + BuildingWorldApp: rooms / zones / devices / people / schedule / events
  + BuildingPhysicsOrchestrator: truth / energy / threshold memory / RNG
  + SensorHub: internally published simulated readings
  + runtime current time and trace
```

真实 Adapter/Provider 的缓存仍由外部网关负责持久化，不包含在纯模拟 checkpoint
中。

## 8. 常见扩展应修改哪里

### 8.1 新增会议预约规则

例如“访客必须由 Staff 发起”或“领导会议优先级更高”：

1. 在 `types.py` 增加必要的角色/字段；
2. 在 `ScheduleApp.create_meeting()` 增加确定性校验；
3. 若影响预留竞争，在 `ResourceAllocationApp` 增加优先级规则；
4. 增加 App 单元测试；
5. Scenario 只负责提供触发规则的初始条件。

### 8.2 新增房间

在 `configs/rooms/` 中增加符合 `room_schema.json` 的 YAML，并通过
`room_loader.load_room_configuration()` 创建业务、物理、设备和传感器对象。不要继续
在 Scenario `base.py` 中手工复制完整房间定义。

### 8.3 新增真实传感器

1. 在点位配置增加 address、zone、quantity 和单位；
2. 使用现有 `SensorPointMapping`，或新增 BACnet/Modbus Adapter；
3. 注册到 `SensorHub`；
4. 不修改 `IndoorEnvironmentEngine`；
5. 增加单位、时间戳、延迟、坏质量和掉线测试。

### 8.4 新增物理量

例如 VOC：

1. `physics/building/models.py`：给 `ZoneState` 和结果增加字段；
2. `indoor_environment_engine.py`：增加守恒方程；
3. `sensor_api.py`：增加 `SensorQuantity` 和标准单位；
4. `observation_model.py`：增加 truth 映射；
5. `orchestrator.py`：增加阈值 transition；
6. 更新 Physics 和 Sensor Integration 测试。

### 8.5 新增设备 Tool

例如空调 App：

1. 在 Building World 定义 `DeviceSpec/DeviceState`；
2. `HvacApp.set_hvac()` 只修改设备状态并产生 Device 事件；
3. Equipment Model 将设定温度、档位和故障转换成 `HvacCommand`；
4. Orchestrator 用 `HvacCommand` 推进物理；
5. SensorApp 返回环境变化；
6. Agent 根据 Effect/Observation 验证目标，而不是把 Tool 成功视为环境达标。

### 8.6 新增 Scenario

1. 继承 `KechuangBuildingScenario`；
2. `initiate_scenario()` 只设置差异化初始状态；
3. 定义 `scenario_input`；
4. 用 `build_events_flow()` 和 `EventRegisterer.capture_mode()` 定义 Oracle 事件图；
5. 定义最终状态 `validate()`；
6. 使用 `@register_scenario("...")`；
7. 添加 Oracle → Replay → Evaluate 测试。

## 9. 当前尚未接通的关键边界

以下能力不要误认为已经完成：

1. **预约成功不会凭空推断未来控制计划**：只有被 Trigger Policy 标记为 wake-worthy 的已发生事件，才会在总管的下一个模型轮次前自动分派给所属房间 Agent；需要 T-45 准备等行为时，场景仍应显式安排未来事件；
2. **多智能体仍是显式选择，不是默认行为**：CLI 和 L3 live runner 默认保持旧的单 Agent，Oracle 与 Replay 路径也不变；
3. **真实 MQTT 连接、认证和重连客户端未实现，仅实现消息 Adapter**；
4. **checkpoint 已覆盖纯模拟 Runtime 和多智能体控制状态**，但真实 Provider 的外部缓存仍由网关负责；
5. **CLI 会输出完整多智能体 checkpoint，但尚无 `--resume` 参数**；恢复目前通过 `BuildingMultiAgentSystem.restore()` API 完成；
6. **取消异步调用不能硬中断已进入线程的同步模型请求**，实际停止依赖单 Agent 的 timeout 和工具预算；
7. **multi 模式暂不与旧 `--a2a` 叠加**，避免形成两套嵌套委派拓扑。

## 10. 推荐的下一步整合目标

统一 Runtime 已实现按事件边界推进。`BuildingEventQueue` 保存尚未发生的事件，
`BuildingTriggerPolicy` 对已经发生的领域事件生成可审计的 `TriggerDecision`：

```python
class BuildingWorldRuntime:
    world: BuildingWorldApp
    physics: BuildingPhysicsOrchestrator
    sensors: SensorHub
    event_queue: BuildingEventQueue
    trigger_policy: BuildingTriggerPolicy

    def advance_to(self, target_time):
        # 1. 取下一外部事件
        # 2. 推进 physics 到事件时间
        # 3. 发布 observation 和 physics transition
        # 4. 应用日程/人员/设备事件
        # 5. Trigger Policy 决定是否唤醒 Agent
        ...
```

当前实现保证：

- 未来事件在执行时刻之前不会出现在 `BuildingWorldApp.events`；
- 事件落在物理步中间时，Runtime 会在事件时间切开该物理步；
- 相同时间的事件按稳定 sequence 执行；
- 普通 `SENSOR_UPDATED` 事件被记录但不会唤醒 Agent；
- 日程取消、设备故障、人工覆盖和环境阈值越界会请求唤醒；
- Queue、事件分类游标和决策 trace 均包含在 Runtime checkpoint 中。

当前 opt-in 控制层由 `RoomEventRouter` 消费分类后的 event/decision 对，
只把 `should_wake_agent=True` 的事件投入对应房间 inbox。Trigger Policy 和
Runtime 不直接依赖或调用 LLM；`BuildingSupervisorAgent` 会在每个总管 LLM
轮次之前检查新增 inbox，并以有界并发自动运行涉及的房间 Agent。这样 Runtime
时间推进仍保持确定性，模型失败也不会发生在物理推进调用栈中。

届时 Conference 场景的数据流应是：

```text
SCHEDULE_CREATED
  -> Event Queue 安排 T-45 preparation
  -> Trigger Policy 唤醒 Agent
  -> HVAC/lighting/printing Tools
  -> Physics step
  -> Sensor observation / threshold event
  -> Agent verification or replan
  -> MEETING_ENDED
  -> 关闭设备并释放资源
```

### 10.1 多智能体 MVP

多智能体实现位于：

- `fairy/controllers/building/room_agents.py`
  - `RoomContextApp`：只暴露一个房间的状态和已发生事件；
  - `RoomScopedToolset`：代理既有 App Tool，并在调用前校验 room/zone/device/meeting 归属；
  - `RoomAgentRegistry`：按房间注册、延迟创建 Agent，维护有界 inbox、预算和 trace。
- `fairy/controllers/building/multi_agent.py`
  - `RoomAgentManagerApp`：供总管执行 list/status/inbox/delegate/broadcast/enable/reset；
  - `RoomEventRouter`：把 wake-worthy 事件定向投递到房间 inbox；
  - `BuildingMultiAgentSystem`：装配总管、房间 Registry、Manager 和 Router。

在现有 Building Scenario 中启用：

```python
from fairy.controllers.building import build_building_multi_agent_system
from fairy.controllers.engine import Engine

scenario.setup()

multi_agent = build_building_multi_agent_system(
    world=scenario.building_runtime.world,
    runtime=scenario.building_runtime,
    supervisor_llm=create_supervisor_llm(),
    # 每个房间应获得独立 client/配置；client 可以指向同一个模型服务。
    room_llm_factory=lambda room_id: create_room_llm(room_id),
    building_apps=scenario.apps or [],
    # 最多同时运行 3 个不同房间 Agent；其余任务在调度器内排队。
    max_parallel_room_agents=3,
)
engine = Engine(agent=multi_agent.supervisor, scenario=scenario)
multi_agent.attach_engine(engine)

workflow = engine.run_scenario_agent(
    max_tool_calls=180,
    timeout_seconds=1800,
    setup_scenario=False,
)
```

CLI 可按次选择运行方式；不传该参数时仍为 `single`：

```bash
# 原单 Agent 路径（默认）
python -m fairy.cli --agent --scenario <scenario_id> \
  --controller building_baseline_react --building-agent-mode single

# 总管 + 每房间一个 Agent
python -m fairy.cli --agent --scenario <scenario_id> \
  --controller building_baseline_react --building-agent-mode multi \
  --max-parallel-room-agents 4 \
  --room-max-tool-calls 40 \
  --room-timeout-seconds 300
```

multi 模式额外输出 `<scenario>.multi_agent_traces.json` 和
`<scenario>.multi_agent_state.json`。前者用于审计路由、自动分派和房间运行，后者是
JSON 可序列化 checkpoint，包含 Runtime、房间 inbox/启停状态、累计指标、有限
trace 和路由状态：

```python
checkpoint = multi_agent.snapshot()
# 在按相同配置重新构建 system 后：
restored_multi_agent.restore(checkpoint)
```

模型连接、线程锁和完整聊天上下文不会写入 checkpoint；恢复后的房间 Agent 会在
第一次新委派时惰性创建，并使用当前公开房间快照与待处理事件继续工作。
新任务不再把上一次自由文本回答当作已验证状态。

v3 公平成对实验已接入结构化房间目标校验、自动有界等待、营业结束时间限制和
收尾无进展检测。完整修复说明、证据范围及手动运行命令见
[v3 修复与复测说明](../../../reports/building_l3_fair_ab_v3_01/implementation.zh.md)。

v4 进一步保留 reset 前的累计用量和运行账本，对明确设备合同自动结束，并返回
`blocked/deferred` 策略结果；还包含保守空房事件过滤和两种公平模式共用的平台期
监控退避。当前运行命令与合同范围见
[v4 修复与复测说明](../../../reports/building_l3_fair_ab_v4_01/implementation.zh.md)。

L3 批量实测建议从仓库根目录用模块方式启动，避免脚本路径启动时找不到 `fairy`：

```powershell
python -m scripts.building_l3_live_runner --stage repair `
  --building-agent-mode multi --max-tool-calls 500 `
  --room-max-tool-calls 80 --room-timeout-seconds 300 `
  --max-prompt-chars 100000 --max-repeated-identical-calls 6 `
  --timeout-seconds 3600 --output-root runs/building_l3_multi_repair_next
```

`--max-tool-calls` 限制单 Agent 或 Supervisor；房间 Agent 独立受
`--room-max-tool-calls` 限制。长运行只裁剪发送给模型的上下文，完整消息与工具记录
仍保留在产物中。相同仿真时刻的重复无进展调用达到阈值时会以 `no_progress`
停止。`get_operational_status.can_finish` 只表示运营收尾完成，
`quality_alerts` 会提示已无法挽回的取消或打印逾期；最终仍以场景 validation 为准。
排查时可用 `--scenario-numbers 3,14,16` 只跑指定场景；它会覆盖 `--stage`。

`broadcast_to_rooms` 供 Supervisor 的同步 function-calling 使用，但内部已经通过
有界线程池并行运行不同房间。异步 Python 服务可以直接使用非阻塞 API：

```python
async def inspect_rooms(multi_agent):
    return await multi_agent.manager.broadcast_to_rooms_async(
        ["k1324", "k1316", "k1315"],
        "检查本房间状态并处理紧急事件。",
    )

# 如果整个 Supervisor 调用也不能阻塞当前 event loop：
result = await multi_agent.run_supervisor_async(
    "并行检查三个房间，并汇总每个房间的结果。"
)
```

并发语义：

- 不同房间 Agent 的模型调用会并行执行，并受 `max_parallel_room_agents` 限制；
- 同一房间的多个委派自动排队，避免同时修改同一个 Agent 的 messages/workflow；
- 广播结果始终按输入房间顺序返回，重复 room ID 只执行一次；
- 单房间失败只形成该房间的失败结果，不会取消其他房间；
- 共享 `BuildingWorldApp` 的实际工具操作使用短时互斥锁，保证事件 ID 和状态写入一致；模型推理与等待仍可并行；
- 取消异步等待不会强制终止已经在线程中运行的同步模型请求，实际停止仍由 Agent 的 `timeout_seconds` 和工具预算控制。

默认安全边界：

- 房间 Agent 只看到自己的过滤快照和 wake-worthy inbox；
- 房间 Tool 使用 allowlist，且无可解析 room scope 的调用默认拒绝；
- 跨房间选择、资源冲突和优先级决策留在总管；
- Runtime listener 失败只记入 trace，不中断事件队列或物理推进；
- 每次委派重置模型 messages，但保留有限 inbox、最近结果和有界 run trace；
- 事件投递本身不会调用模型；总管在下一个 LLM 轮次前进行有界自动分派；
- 场景完成守卫会同时检查原有业务阻塞项与未确认的房间事件，避免提前结束。

当前 MVP 限制：

- 未知归属的全局事件只记录为 unrouted，不会默认广播；
- checkpoint 恢复已有 API，但 CLI 尚未提供从 checkpoint 继续运行的参数；
- 已有 20 场景真实模型运行记录；本轮保护改动尚需重新执行配对实测才能评价效果；
- 取消异步等待不会硬终止已在线程中运行的同步模型请求；
- `--building-agent-mode multi` 与旧 `--a2a` 当前互斥。

## 11. 测试对应关系

| 测试文件 | 覆盖范围 |
|---|---|
| `test_building_indoor_environment_engine.py` | 热湿、CO₂、PM2.5、能耗、阈值和快照 |
| `test_building_sensor_integration.py` | MQTT 映射、真实/模拟优先级、Agent 真值隔离 |
| `test_building_meeting_booking.py` | 预约、冲突、容量、能力、事件、快照和 Replay |
| `test_building_multi_agent.py` | 房间隔离、跨房拒绝、懒加载、预算、事件路由、故障隔离、总管委派、自动消费、组合完成守卫、异步并行、同房串行、累计指标和 checkpoint 恢复 |

修改某一层时，应优先运行对应测试，再运行 FAIRY migration smoke test，确认 Farm
场景没有受到 Building 扩展影响。

## 12. 需要长期保持的架构约束

- Scenario 描述问题，不实现可复用规则；
- App 执行业务规则，不实现物理公式；
- Physics 维护隐藏真值，不读取或信任 Agent 输出；
- SensorReading 不携带 truth；
- Tool 成功不等于物理目标达成；
- 所有时间必须有明确时区；
- 所有 replay ID 必须确定性生成；
- 资源冲突和安全规则必须由确定性代码执行；
- 模拟与真实设备通过 Adapter 边界替换，上层场景不依赖厂商 SDK。

## 13. v5 公平实跑：明确计划与有限继续等待

新增共享的设备目标可实现性校验和受保护批量执行：
`BuildingOperationsApp.apply_device_plan` / `apply_room_plans` 向单、多模式对称开放。
明确的房间合同可不调用房间模型，由控制器通过原有 scoped tools 执行，保留工具预算、逐动作日志和状态验证。
同房间兼容目标可合并；错误模式或冲突在执行前拒绝；正在进行的会议不能误关必需设备。

`apply_room_plans` 可包含一次 0..60 分钟的批准等候，仅在全部目标先验证成功后开始。
等候中只有可证明已处理的常规事件能继续；重大事件、环境告警、就绪/功率等守卫和时间边界仍中断。
这不是自由后台执行，也不代表设备状态已经产生所需物理效果。

新增 `*.device_plan_actions.json` 保留批内实际动作。报告分别列出外层/房间工具数与批内动作数，不能把它们简单相加。
完整边界、验证记录及手动配对命令见
[v5 实现说明](../../../reports/building_l3_fair_ab_v5_01/implementation.zh.md)。
