# Smart-Building ARE / K13 World Model TODOs

> 基于 FAIRY 现有 `Agent → Controller → App → Physics → Scenario → Evaluation` 框架，构建面向 K13 层智慧办公场景的事件驱动 Smart-Building ARE。

## 当前实施状态（2026-08-17）

> 下方原始清单保留了早期设计过程，部分 `[ ]` 已经被后续重构实现，不能再单独
> 作为代码现状判断依据。本节是当前阶段的实施基线。

已经完成：

- K1324、K1316、K1315 的声明式房间配置、统一加载和共享 Runtime；
- 房间、功能区、人员、日程、资源、设备和统一 `BuildingEvent` 状态模型；
- HVAC、加湿器、净化器、通风、灯光、投影、音响、麦克风和打印工具；
- 温度、湿度、CO₂、PM2.5、设备/人员热负荷、能耗和舒适度降阶物理模型；
- 带噪声、偏差、延迟、缺失和质量码的 Observation Model；
- 分钟级物理推进、Event Queue、Trigger Policy、snapshot/restore 和 trace；
- Building 专用 system prompt 与 `building_baseline_react`；
- `smartoffice/scenarios/building_kechuang/` 下四个可运行、可回放、可验证场景；
- MQTT Publisher、Subscriber、Adapter、SensorHub 和物理传感器模拟器；
- Building 与 FAIRY 迁移回归测试。

本轮已完成：

- [x] 使用真实 Mosquitto 验收发布、订阅和 SensorHub 优先级；
- [x] 实现 MQTT/内部模拟观测的 Shadow Mode 误差记录；
- [x] 实现 K1316 小型研讨完整生命周期场景；
- [x] 建立跨场景 Building 实验指标并完成真实模型首轮批量实验。

新的优先事项：

1. 为温湿度反馈控制增加迟滞、最大模拟时长、无改善计数和达标终止策略；
2. 使用多随机种子重复真实模型实验，而不是依赖单次结果；
3. 实测三个房间的面积、设备参数和传感器位置后进行物理标定；
4. 在真实传感器接入后运行 Shadow Mode 并分析 bias/MAE/RMSE。

当前明确延后：设备/传感器异常事件、复杂门禁、楼层可视化、多模态新房间生成和
BACnet/Modbus 接入。

## 0. 项目目标与边界

- 复用 FAIRY 的 CLI、LLM 接口、ReAct/function-calling、Tool Builder、工作流记录、Oracle、Replay 和评估框架。
- 采用“零基础系统”假设：不预设 Kaihong 或其他外部系统已经提供硬件、资源分配、状态监控、设备接口或异常处理能力。
- Demo 中所需的房间、人员、传感器、设备、资源、日程、监控和故障均由本项目完整模拟；未来接入真实硬件时仅替换设备 Adapter，不改变上层 World Model、ARE Runtime 和场景代码。
- 新增与 `farm_world` 平行的 `building_world`，不把农业代码直接改写成办公代码。
- L1 首先跑通 K1324 的“会议准备—会中保障—突发变化—会后关闭”闭环。
- 当前阶段不优先实现模型选择 Router；先解决“哪些事件需要唤醒 Agent、何时重新规划、哪些动作可以规则或工具直达”。
- 所有设备动作必须通过 World Model 产生可观察的物理效果，不能直接修改传感器读数。

## 1. 目标代码结构

```text
smartoffice/
├── apps/
│   └── building_world/
│       ├── __init__.py
│       ├── building_world_app.py
│       ├── types.py
│       ├── room_loader.py
│       ├── device_registry.py
│       ├── resource_allocation_app.py
│       ├── monitoring_app.py
│       ├── hvac_app.py
│       ├── air_device_app.py
│       ├── lighting_app.py
│       ├── access_app.py
│       ├── occupancy_app.py
│       ├── schedule_app.py
│       ├── office_device_app.py
│       └── sensor_app.py
├── adapters/
│   └── building/
│       ├── base_adapter.py
│       └── simulated_adapter.py
├── physics/
│   └── building/
│       ├── __init__.py
│       ├── orchestrator.py
│       ├── thermal_engine.py
│       ├── humidity_engine.py
│       ├── air_quality_engine.py
│       ├── energy_engine.py
│       ├── comfort_engine.py
│       └── observation_model.py
├── controllers/
│   ├── event_bus.py
│   ├── event_queue.py
│   ├── trigger_policy.py
│   ├── building_are_controller.py
│   └── building_agent_builder.py
├── scenarios/
│   ├── building_k1324/
│   │   ├── base.py
│   │   ├── conference_l1.py
│   │   ├── ceo_mode.py
│   │   └── office_hours.py
│   └── bos/
│       ├── evaluator.py
│       ├── metrics.py
│       └── decision_gates.py
├── visualization/
│   └── building_world/
│       ├── floorplan_loader.py
│       ├── world_model_renderer.py
│       ├── trace_overlay.py
│       └── export_figure.py
└── configs/
    ├── rooms/
    │   ├── room_schema.json
    │   └── k1324.yaml
    └── floors/
        └── k13.yaml
```

---

# P0 — K1324 Working L1

## 2. 建筑领域类型与状态模型

- [ ] 在 `smartoffice/apps/building_world/types.py` 定义 `RoomStaticSpec`。
- [x] 定义 `RoomDynamicState`；环境隐藏真值继续由 Physics `ZoneState` 持有。
- [x] 定义 `ZoneSpec`，支持会议区、办公区、出入口等空间分区。
- [x] 定义 `PersonState`、`PersonRole` 和人员当前位置。
- [x] 定义 `DeviceSpec`、`DeviceState` 和设备故障状态。
- [ ] 定义 `SensorReading`，区分真实状态、采样时间和观测值。
- [ ] 定义 `ScheduleEntry` 和 `MeetingState`。
- [ ] 定义 `BuildingAction`、`ActionResult` 和 `ActionEffect`。（前两项已完成）
- [x] 定义统一 `BuildingEvent`：事件 ID、时间、类型、来源、payload、父事件和因果链。
- [ ] 在 `smartoffice/types.py` 中只增加必要的跨领域事件接口，避免建筑类型污染全局类型系统。

### 必需人员角色

- [x] `LEADER`
- [x] `PROFESSOR`
- [x] `STAFF`
- [x] `STUDENT`
- [x] `VISITOR`
- [x] `UNKNOWN`

### 必需事件类型

- [ ] `SCHEDULE_CREATED / CHANGED / CANCELLED`
- [ ] `PERSON_ARRIVED / LEFT`
- [ ] `OCCUPANCY_CHANGED`
- [ ] `SENSOR_UPDATED`
- [ ] `DEVICE_STATE_CHANGED / FAILED`
- [ ] `MEETING_DELAYED / ENDED_EARLY`
- [ ] `MANUAL_OVERRIDE`
- [ ] `COMFORT_THRESHOLD_VIOLATED`
- [ ] `AIR_QUALITY_THRESHOLD_VIOLATED`

## 3. K1324 房间配置

- [x] 建立通用 `configs/rooms/room_schema.json`。
- [x] 编写 `configs/rooms/k1324.yaml`。
- [ ] 录入房间面积、层高、容量、门窗和区域信息。（门窗待实测补充）
- [ ] 录入空调、加湿器、净化器、灯光、门禁、打印机、投影和音响。（核心设备、投影音响和打印机已录入）
- [ ] 录入温湿度、CO₂、PM2.5、人员存在等传感器。（环境四项已录入）
- [x] 录入设备额定功率、档位、位置和影响区域。
- [ ] 录入座位、空调出风方向及冷风直吹关系。
- [ ] 录入温湿度、空气质量和安全阈值。
- [x] 实现 `room_loader.py`，完成配置加载、字段校验、范围校验和初始状态创建。
- [x] 为配置加载增加单元测试和非法配置测试。

## 4. BuildingWorldApp

- [x] 新增 `BuildingWorldApp`，作为建筑领域总入口，对齐 `FarmWorldApp`。
- [x] 保存房间静态描述和动态状态。
- [ ] 注册所有建筑子 App。
- [x] 通过 `BuildingWorldRuntime` 持有并协调 `BuildingPhysicsOrchestrator`。
- [ ] 实现 `get_world_state()`。
- [ ] 实现 `get_observation()`，只返回 Agent 可观测信息。
- [ ] 实现 `apply_event(event)`。
- [ ] 实现 `advance_time(minutes)`。
- [ ] 实现 `get_pending_events()`。
- [x] 实现 World/Physics/模拟 SensorHub 的组合 `snapshot()` 和 `restore()`。
- [ ] 将动作、物理效果和传感器反馈写入统一 trace。

## 5. 设备与办公 Tool/API

### 5.1 HVAC

- [x] 新增 `hvac_app.py`。
- [x] 实现空调开关、制冷/制热模式、目标温度和风速控制。
- [x] 查询空调运行状态和故障状态。
- [x] 校验目标温度、档位和设备能力。
- [x] 控制命令只修改设备运行状态，由物理引擎更新环境。
- [ ] 每次控制产生 `ActionEvent`，实际物理作用产生 `EffectEvent`。

### 5.2 空气设备

- [x] 新增 `air_device_app.py`。
- [x] 实现加湿器开关和档位控制。
- [x] 实现空气净化器开关和档位控制。
- [ ] 模拟加湿器缺水、净化器滤芯状态和设备故障。

### 5.3 灯光

- [ ] 新增 `lighting_app.py`。
- [ ] 支持分区灯光、亮度和会议预设。
- [ ] 将灯光功率和散热接入物理模型。

### 5.4 门禁与人员

- [ ] 新增 `access_app.py`。
- [ ] 实现人员身份、角色、临时授权和访问判断。
- [ ] 将最终门禁安全约束实现为确定性规则，禁止 LLM 绕过。
- [ ] 新增 `occupancy_app.py`。
- [ ] 模拟人员到达、离开、位置和房间人数。
- [ ] 支持查询教授是否在校、关键人员是否到达。
- [ ] 将人数和位置接入热负荷、CO₂ 与舒适度模型。

### 5.5 日程

- [ ] 新增 `schedule_app.py`。
- [ ] 支持创建、修改、取消和查询会议。
- [ ] 支持 Office Hours、人员冲突和房间冲突。
- [ ] 日程修改必须产生可触发重新规划的事件。

### 5.6 办公设备

- [ ] 新增 `office_device_app.py`。
- [ ] 实现普通文件和演讲者名牌打印。
- [ ] 模拟缺纸、卡纸、墨量不足和任务队列。
- [ ] 实现投影、显示屏、音响和麦克风控制。
- [ ] 实现会议设备就绪检查。

### 5.7 传感器

- [ ] 新增 `sensor_app.py`。
- [ ] 提供温度、湿度、CO₂、PM2.5 和占用观测。
- [ ] 区分 World Model 内部真实状态与传感器读数。
- [ ] 支持噪声、采样间隔、延迟、缺失和故障。

### 5.8 设备注册、资源分配与状态监控

- [x] 新增 `device_registry.py`，统一登记设备 ID、类型、能力、位置和状态。
- [ ] 实现设备发现、注册、注销、能力查询和可用性查询。（查询已完成）
- [ ] 新增 `resource_allocation_app.py`，模拟房间、会议设备、打印机和其他共享资源的申请、占用、释放和冲突处理。
- [ ] 支持资源预留、优先级、超时释放、取消和重新分配。
- [ ] 将人员角色、会议等级、时间窗口和资源约束纳入分配决策。
- [ ] 新增 `monitoring_app.py`，持续维护设备在线状态、运行模式、功率、传感器新鲜度和任务执行状态。
- [ ] 实现健康检查、超时检测、异常告警和状态变化事件。
- [ ] 监控系统只维护结构化状态和产生事件，不直接替 Agent 做高层适应性决策。
- [ ] 实现 `base_adapter.py`，定义设备读状态、执行命令和订阅反馈的统一接口。
- [ ] 实现 `simulated_adapter.py`，为所有 Demo 设备提供完整的软件模拟。
- [ ] 确保场景和 Agent 只依赖统一 Adapter 接口，不直接依赖任何真实硬件 SDK。

## 6. 建筑物理 World Model

### 6.1 统一物理接口

- [ ] 定义 `step(state, static_spec, dt_seconds)` 接口。
- [ ] 支持 1–10 分钟的可配置时间步。
- [ ] 所有引擎共享同一个 `RoomDynamicState`。
- [ ] 支持固定随机种子和确定性 replay。

### 6.2 温度模型

- [ ] 实现一阶 RC 热模型。
- [ ] 考虑室外温度、门窗换热、空调制冷/制热、人数和设备散热。
- [ ] 支持预测达到目标温度所需提前量。
- [ ] 添加空调型号和额定制冷/制热能力配置。

### 6.3 湿度模型

- [ ] 模拟空调除湿作用。
- [ ] 模拟加湿器、人员和室外空气影响。
- [ ] 支持“空调运行 → 湿度下降 → 加湿器触发”的耦合动态。

### 6.4 空气质量模型

- [ ] 根据人数和活动水平模拟 CO₂ 累积。
- [ ] 模拟自然通风或新风换气。
- [ ] 模拟净化器对 PM2.5 的衰减。
- [ ] 空气质量越界时产生阈值事件。

### 6.5 能耗模型

- [ ] 查明 K1324 空调型号、额定功率和制冷量。
- [ ] 建立空调功率与模式、风速、设定温度、室内外温差的粗略关系。
- [ ] 计算所有设备瞬时功率和累计能耗。
- [ ] 输出分设备能耗、总能耗和电费估计。

### 6.6 舒适度模型

- [ ] 计算温度、湿度、CO₂ 和 PM2.5 舒适度代价。
- [ ] 根据人员位置和出风口方向计算冷风直吹惩罚。
- [ ] 计算设备噪声或局部环境差异。
- [ ] 支持按人员、区域和全房间输出舒适度。

### 6.7 Observation Model

- [ ] 从真实环境状态生成传感器观测。
- [ ] 模拟噪声、延迟、缺失和故障。
- [ ] 禁止 Agent 直接访问不可观测的内部真实状态。

### 6.8 Physics Orchestrator

- [x] 按“外部事件 → 设备状态 → 温度 → 湿度 → 空气质量 → 能耗 → 舒适度 → 观测”推进。
- [x] 汇总每个时间步产生的事件。
- [ ] 检查温度、湿度、功率等物理范围。
- [x] 记录可回放的逐步状态 trace。

## 7. 分钟级仿真时间

- [ ] 扩展 `TimeManager` 支持分钟级推进。
- [ ] 保留原有农业逐日接口和行为，避免破坏现有场景。
- [x] 增加 `advance_to(timestamp)`。
- [x] 增加下一事件时间查询。（由 BuildingEventQueue 提供）
- [ ] 支持暂停、恢复、快进、snapshot 和 replay。（Runtime snapshot/replay 已完成）
- [x] 处理物理时间步与事件时间不对齐的问题。

## 8. Everything-is-Event Runtime

### 8.1 Event Bus

- [ ] 新增 `event_bus.py`。
- [ ] 支持事件发布、订阅和按类型注册处理器。
- [ ] 统一接收日程、人员、传感器、设备、故障和人工覆盖事件。
- [ ] 保存事件因果父子关系。
- [ ] 将所有事件写入运行日志。

### 8.2 Event Queue

- [x] 新增 `event_queue.py`。
- [x] 按仿真时间调度未来事件。
- [ ] 支持运行时插入、修改和取消事件。
- [x] 支持相同事件序列的确定性重放。

### 8.3 Trigger Policy

- [x] 新增 `trigger_policy.py`。
- [x] 定义必须唤醒 Agent 的重要事件集合。
- [x] 定义温湿度、空气质量和设备故障阈值触发。
- [ ] 判断当前计划前提是否被事件破坏。
- [x] 普通周期传感器更新不得默认调用 LLM。
- [x] 记录每次“唤醒/不唤醒”决策，供后续 Router 和论文分析使用。

### 8.4 Building ARE Controller

- [ ] 新增 `building_are_controller.py`。
- [ ] 从 Event Queue 取出下一事件并推进时间和物理状态。
- [ ] 将事件应用于 World State。
- [ ] 由 Trigger Policy 判断是否唤醒 Agent。
- [ ] 根据当前事件、状态、目标和相关历史构建上下文。
- [ ] 调用现有 ReAct Agent 和 function-calling 循环。
- [ ] 执行 Tool Calls，并等待物理效果和观测反馈。
- [ ] 在新事件使旧计划失效时重新规划。
- [ ] 支持手工覆盖和安全约束。

## 9. Agent 层复用与扩展

- [ ] L1 复用现有 `Agent`、`BaseAgent`、`BaseLLM` 和 `toolset_builder.py`。
- [ ] 复用现有重试、退避、工具执行和 workflow recording。
- [ ] 新增 Smart Building system prompt。
- [ ] 要求 Agent 先检查证据和状态，再执行设备动作。
- [ ] 要求 Agent 根据 `EffectEvent` 验证动作是否生效。
- [ ] 禁止 Agent 假设工具调用成功等同于物理目标达成。
- [ ] 在 `AgentBuilder` 注册 `building_baseline_react`。
- [ ] 注册 `building_event_react`。
- [ ] 当前不迁移全部 10 种农业 Controller。

## 10. Conference L1 场景

- [ ] 新增 `scenarios/building_k1324/conference_l1.py`。
- [x] 新增无异常条件的 `scenario_conference_standard.py`，跑通正常会议生命周期。
- [x] 新增 `scenario_climate_coordination.py`，验证空调—湿度—加湿器正常联动。
- [x] 新增 `scenario_occupancy_ramp.py`，验证人数—CO₂—通风正常演化。
- [ ] 设置 K1324 初始环境、设备、人员和正式会议日程。
- [ ] T-60 min：会议进入准备阶段。
- [ ] T-45 min：根据预测到达时间开始环境预调。
- [ ] T-30 min：打印演讲者名牌和会议材料。
- [ ] T-20 min：空调除湿导致湿度下降。
- [ ] T-15 min：领导或关键人员到达。
- [ ] T-10 min：实际人数超出预期。
- [ ] T+00 min：会议开始。
- [ ] T+30 min：会议提前结束并触发设备关闭。
- [ ] 至少实现一次空调—湿度—加湿器联动。
- [ ] 至少实现一次会议变化后的计划撤销或重新规划。
- [ ] 增加打印机缺纸或关键设备失败作为可选异常。
- [ ] 输出事件时间线、设备动作、环境曲线、舒适度和能耗。

## 11. Conference Oracle / Replay / Evaluate

- [ ] 编写 Conference Oracle workflow。
- [ ] 定义必须完成的准备任务和时间窗口。
- [ ] 定义可接受的等价动作。
- [ ] 定义门禁、安全和舒适度 decision gates。
- [ ] 定义突发事件后的正确修正行为。
- [ ] 使用现有 Replay 机制确定性重放。
- [ ] 确保同一随机种子得到一致结果。

---

# P1 — 场景、评估与实验基础

## 12. CEO Mode

- [ ] 新增 `ceo_mode.py`。
- [ ] 实现领导日程、预计到达和真实到达事件。
- [ ] 根据身份和日程提前进行环境准备。
- [ ] 领导取消或延迟到达时撤销或延后动作。
- [ ] 学生、访客和无关人员不能触发相同资源安排。
- [ ] 测试临时授权和手工覆盖。

## 13. Office Hours Mode

- [ ] 新增 `office_hours.py`。
- [ ] 接入教授日程和在校/在办公室观测。
- [ ] 接收学生会面请求。
- [ ] 综合教授状态、房间可用性、学生身份和请求条件进行安排。
- [ ] 教授离开、学生迟到、房间占用时动态重安排。
- [ ] 未确认会面时不启动高能耗设备。

## 14. Conference Mode 完整版

- [ ] 将 Conference Mode 设计为包含 VIP 安排的复合场景。
- [ ] 增加主讲人、领导、教师、学生和访客角色。
- [ ] 增加打印、投影、音响、照明、门禁和环境准备任务。
- [ ] 建立会前、会中和会后状态机。
- [ ] 支持会议延期、延长、取消、人员变化和设备故障。
- [ ] 抽取通用 `MeetingLifecycle`、`PreparationTask` 和 `RolePolicy`，避免场景复制代码。

## 15. Building Operational Score（BOS）

- [ ] 复用 FOS 的 `Outcome + Decision + Efficiency` 评估骨架。
- [ ] 新增 Outcome：会议准备、设备就绪、门禁正确、打印完成。
- [ ] 新增 Adaptability：事件检测、旧计划撤销、重新规划、响应延迟。
- [ ] 新增 Comfort：温湿度、CO₂、PM2.5、冷风直吹和舒适度越界时间。
- [ ] 新增 Efficiency：能耗、空转能耗、无效动作、LLM/Tool 调用和 token。
- [ ] 增加房间和共享设备资源利用率。
- [ ] 增加资源冲突数、等待时间和无效预留时间。
- [ ] 增加状态一致性、故障恢复和手工覆盖指标。
- [ ] 支持权重敏感性分析和跨场景汇总。

## 16. Baseline 与消融

- [ ] Baseline 1：固定 Function-Calling Workflow。
- [ ] Baseline 2：普通任务级 ReAct。
- [ ] Baseline 3：Event-driven ReAct，但无物理 World Model。
- [ ] Proposed：Event-driven ARE + World Model。
- [ ] 比较任务成功率、适应性响应延迟、舒适度违反、能耗和无效调用。
- [ ] 分别验证 Event Runtime、World Model 和 Agent 重规划的价值。

## 17. Long-Horizon 与事件库

- [ ] 支持跨小时、跨天的日程和事件。
- [ ] 保存计划状态、观测状态、确认状态和历史因果关系。
- [ ] 支持检查点恢复和反事实事件重放。
- [ ] 构建会议提前、延期、延长、取消事件。
- [ ] 构建人员迟到、缺席、超额和未授权访问事件。
- [ ] 构建设备故障、传感器失效和人工覆盖事件。
- [ ] 构建温度达标但湿度过低等耦合物理事件。
- [ ] 构建房间已空但设备继续运行等能耗异常事件。

## 18. 测试与回归

### 物理模型

- [ ] 制冷模式下空调运行使室温下降。
- [ ] 空调持续运行使湿度下降。
- [ ] 加湿器运行使湿度上升。
- [ ] 人数增加使 CO₂ 上升。
- [ ] 净化器运行使 PM2.5 下降。
- [ ] 所有状态保持在合法物理范围。

### Tool/API

- [ ] 非法空调参数被拒绝。
- [ ] 未授权人员不能绕过门禁规则。
- [ ] 打印机异常能够产生失败事件。
- [ ] Tool 动作、结果和物理效果进入 trace。

### ARE Runtime

- [ ] 普通采样事件不会频繁唤醒 Agent。
- [ ] 关键日程变化、设备故障和人工覆盖可以唤醒 Agent。
- [ ] 新事件可以使旧计划失效并触发修正。
- [ ] 事件可以追溯到触发它的动作或环境变化。

### FAIRY 回归

- [ ] 原有 Farm CLI、逐日时间推进和 App 注册保持正常。
- [ ] 原有 Oracle、Replay 和 FOS 结果不受建筑模块影响。
- [ ] Building Physics 不会被 Farm Engine 错误加载。

---

# P2 — MobiCom Paper 与泛化探索

## 19. K13 层 Floorplan World Model 可视化

> 参考附图的三层表达：上层是 Agent/用户任务，中层是机器可读能力抽象，下层是真实空间平面图及设备、区域、状态和任务轨迹。该图拟作为 MobiCom 论文中的 K13 层 World Model 总览图。

### 19.1 K13 Floor Schema

- [ ] 建立 `configs/floors/k13.yaml`。
- [ ] 录入 K13 层各房间、走廊、出入口和公共区域。
- [ ] 标注 K1324、K1315、K1316 等重点房间。
- [ ] 定义房间邻接、人员可达路径和设备影响范围。
- [ ] 将 Floor、Room、Zone、Device、Sensor、Person 和 Event 建立 ID 映射。
- [ ] 确保 Floorplan 元素能够引用运行时 World State。

### 19.2 三层图结构

- [ ] 上层绘制 `Users / Tasks / LLM Agent + ARE Runtime`。
- [ ] 中层绘制机器可读 Building Skills/Capabilities。
- [ ] 能力分组至少包含环境控制、人员/门禁、日程调度、会议设备、办公设备和感知。
- [ ] 表达 Agent 的 capability retrieval、task dispatch 和 data feedback 闭环。
- [ ] 下层使用 K13 层真实平面图。
- [ ] 在平面图中标注房间功能区、传感器、空调、加湿器、净化器、打印机和会议设备。
- [ ] 叠加人员位置、设备状态、环境状态和事件位置。
- [ ] 叠加 CEO、Office Hours、Conference 的任务执行路径或事件序列。

### 19.3 可视化代码

- [ ] 实现 `floorplan_loader.py`，加载矢量平面图及坐标映射。
- [ ] 实现 `world_model_renderer.py`，根据 K13 World State 绘制房间、区域和设备。
- [ ] 实现 `trace_overlay.py`，将 Agent 动作、人员移动和事件因果路径叠加到平面图。
- [ ] 支持按时间点绘制静态状态快照。
- [ ] 支持按事件序列绘制多步轨迹。
- [ ] 支持选择性显示设备、传感器、人员、环境和任务图层。
- [ ] 从 `run_report.json` 和 `agent_workflow.json` 自动生成图中状态及轨迹。
- [ ] 实现 `export_figure.py`，导出论文级 SVG/PDF 和高分辨率 PNG。
- [ ] 保证 SVG 中主要元素可编辑，文字不栅格化。
- [ ] 建立统一颜色、图标、线型和图例规范。

### 19.4 论文图内容

- [ ] 在图中给出 K13 层 World Model 规模摘要：房间数、区域数、设备数、传感器数、能力数和事件类型数。
- [ ] 选择一个 Conference 场景展示跨空间、跨设备任务链。
- [ ] 用实线表示动作路径，用虚线表示数据/事件反馈，用颜色区分场景或能力类别。
- [ ] 体现“物理空间 → 能力抽象 → Agent 编排 → 物理反馈”的闭环。
- [ ] 避免将图做成单纯建筑平面图；必须体现其为 Agent-readable World Model。
- [ ] 输出初版后检查单栏、双栏和演示文稿三种缩放条件下的可读性。

### 19.5 验收标准

- [ ] 图中每个设备和区域均能映射回配置文件中的唯一 ID。
- [ ] 图中任务轨迹能够由真实 scenario trace 自动生成。
- [ ] 同一渲染器可用于 K1324 单房间图和 K13 全楼层图。
- [ ] 输出 SVG/PDF 满足 MobiCom 论文排版需求。
- [ ] 图的叙事清楚区分 Agent、能力抽象、物理空间和反馈闭环。

## 20. 未见房间 World Model 生成

- [ ] 定义通用 `RoomWorldModelSchema`。
- [ ] 输入已有房间配置或代码模板及新房间多角度照片。
- [ ] 使用多模态 LLM 识别用途、区域、桌椅、门窗和可见设备。
- [ ] 输出受 Schema 约束的 YAML/JSON，而不是自由生成 Python 代码。
- [ ] 为每个自动生成字段保存置信度和证据。
- [ ] 标记空调型号、额定功率等不可从图片确认的参数。
- [ ] 由人工补充缺失参数并执行物理范围校验。
- [ ] 通过 `room_loader.py` 自动实例化新的 Building World。
- [ ] 完成 K1315 → K1316 示例。

## 21. 未见房间与未见场景泛化实验

- [ ] 将房间结构、设备能力、人员角色、事件和任务目标解耦。
- [ ] 使用 declarative scenario 描述 CEO、Office Hours 和 Conference。
- [ ] 分别评估“新房间泛化”和“新事件/任务泛化”。
- [ ] 留出未见身份组合、事件顺序、设备组合和房间布局。
- [ ] 评估无需修改 Agent 代码时的任务完成率。
- [ ] 评估自动生成 World Model 的结构正确性和任务可执行性。
- [ ] 将照片生成 World Model 定位为论文 exploration/demo；若要作为核心贡献，需增加系统性数据集和量化评估。

---

# 22. 实施里程碑

## Milestone 1：K1324 L1

- [ ] Building 类型系统和 `k1324.yaml` 可加载。
- [ ] BuildingWorldApp 和核心设备 Tool 可运行。
- [ ] 模拟设备注册、资源分配和状态监控链路可运行。
- [ ] 温度、湿度、能耗和舒适度模型可推进。
- [ ] 分钟级时间、Event Queue 和 Trigger Policy 可运行。
- [ ] Conference L1 完整闭环可重复执行。
- [ ] 输出环境曲线、能耗、事件时间线和 Agent workflow。

## Milestone 2：三模式与评估

- [ ] CEO、Office Hours、Conference 三类场景可运行。
- [ ] Oracle、Replay、BOS 和事件库完成。
- [ ] Function-Calling、ReAct 和 Event-driven ARE 可以公平对比。

## Milestone 3：K13 World Model 与 MobiCom 图

- [ ] K13 Floor Schema 与主要房间配置完成。
- [ ] K13 平面图与运行时状态完成绑定。
- [ ] 能够从真实 scenario trace 自动生成任务轨迹。
- [ ] 完成论文级三层 World Model 总览图。

## Milestone 4：泛化探索

- [ ] 图片到 Room Schema 的多模态生成链路完成。
- [ ] K1315 → K1316 演示完成。
- [ ] 未见房间与未见场景评测完成。

---

# 23. L1 Definition of Done

满足以下条件才视为 K1324 L1 完成：

- [ ] 正式会议从日程事件开始运行。
- [ ] ARE 根据事件而不是固定脚本决定何时唤醒 Agent。
- [ ] ReAct Agent 能调用设备和办公 Tool。
- [ ] 所有房间与设备资源均由模拟资源系统注册、分配、监控和释放。
- [ ] 不连接任何真实硬件时，Demo 仍能完整运行并产生状态反馈和故障事件。
- [ ] 空调动作通过物理模型改变温度、湿度和能耗。
- [ ] 湿度下降能够进一步触发加湿器决策。
- [ ] 会议变化或设备故障能够使原计划失效并触发修正。
- [ ] 会后设备正确关闭。
- [ ] 全过程可以 snapshot、replay 和 evaluate。
- [ ] 输出完整的事件、动作、物理效果、观测、LLM 调用和评估 trace。

最终最小闭环：

```text
Conference Event
    → ARE Trigger
    → ReAct / Tool Calls
    → Device State Change
    → Building Physics
    → Sensor Observation / Effect Event
    → Plan Verification or Revision
```
