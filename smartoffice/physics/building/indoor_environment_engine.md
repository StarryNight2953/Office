# Building Physics Core

完整 Building World 工作流、状态所有权及扩展路径见
`smartoffice/apps/building_world/README.md`。本文只说明物理与观测模型。

`smartoffice.physics.building` 是 Building World 的确定性降阶物理内核。它只维护模拟
真值，不读取传感器，也不执行面向用户的设备命令。

## 边界

```text
Device state / occupancy / outdoor conditions
                    |
                    v
        IndoorEnvironmentEngine
                    |
                    v
 temperature / humidity / CO2 / PM2.5 truth
                    |
                    v
        BuildingObservationModel
                    |
                    v
 noisy, delayed, missing SensorReading
```

- `models.py`：区域参数、真值状态、设备物理输入和逐步结果。
- `indoor_environment_engine.py`：耦合温度、水分、CO₂ 和 PM2.5 守恒模型。
- `observation_model.py`：采样周期、延迟、偏置、噪声、缺失和质量码。
- `orchestrator.py`：统一推进、累计能耗、阈值 transition 事件和 snapshot/restore。
- `sensor_api.py`：模拟与真实传感器共享的只读 Provider 边界。

## 物理输入，而不是设备 API

`HvacCommand` 表示设备模型已经计算出的物理作用，例如供冷量、供热量、新风量、
加湿量、净化器洁净空气量和电功率。后续 `HvacApp` 负责将开关、模式、设定温度、
风速和故障状态转换成这些作用。Tool 调用不能直接修改室温或传感器值。

制冷量会通过 `cooling_moisture_removal_g_per_kwh` 产生粗略冷凝除湿作用，因此可以
模拟“空调制冷—湿度下降—加湿器响应”的耦合链。该参数需要用 K1324 实测数据标定。

## 真值与观测

`ZoneState` 是隐藏真值。`BuildingObservationModel.sample()` 返回内部
`ObservationSample`，其中 `truth_value` 只用于评估和调试；Agent-facing SensorApp
只能暴露其中的 `SensorReading`。读取时还必须满足 `available_at <= current_time`，
避免传感器延迟信息提前泄漏。

传感器同化不属于模拟路径。未来接入真实硬件时，应另建 estimated state 或状态
估计器，不能让实测读数静默覆盖可回放的模拟真值。

## 确定性与事件

相同初始状态、输入序列、时间步和随机种子会生成相同结果。Orchestrator 只在阈值
状态发生 transition 时产生事件：第一次越界产生 violation，持续越界不重复告警，
恢复后产生 recovered。这些事件随后由 Building Trigger Policy 决定是否唤醒 Agent。

## 当前模型范围

- 温度：单节点 RC、围护结构、渗透/新风、送风、太阳、人员和设备显热。
- 湿度：水蒸气质量守恒、人员产湿、加湿、显式除湿和制冷冷凝除湿。
- CO₂：人员释放和室外空气交换。
- PM2.5：室外渗入、室内源、沉降和净化器 CADR。
- 能耗：HVAC COP、辅助设备和普通设备的逐步 kWh。
- 舒适度：温度、湿度、CO₂ 和 PM2.5 的可审计加权代价。

这套模型用于 ARE 闭环实验，不替代 EnergyPlus、Modelica 或 CFD。
