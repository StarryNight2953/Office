# K1324 传感器筛选与分阶段部署建议

本清单根据 `TODO/sersor_reference.png` 表格中的 ontology 进行合并和筛选，目标是
支持当前 K1324 的会议预约、会议准备、舒适度、空气质量和能耗实验。表中同义名称
不代表需要分别采购多个传感器。

## 1. 筛选原则

- 同一物理量的不同 ontology 合并为一个项目内规范名称；
- 室外天气优先使用网络公共服务，前期可省略本地气象设备；
- 可由温度、湿度或多个测点计算出的派生量不单独安装传感器；
- 中央空调水系统和风管测点仅在 K1324 确实能接入对应设备时部署；
- 当前 L1 优先观测 Agent 决策会直接影响的室内环境量；
- 设备控制状态不是传感器真值，不能用“空调已开启”代替温度或功率测量。

## 2. 前期建议实装的房间侧传感器

| 表中 ontology（合并） | 规范物理量 | 单位 | 建议 | 当前代码 | 用途 |
|---|---|---:|---|---|---|
| `Zone_Air_Temperature_Sensor`、`Air_Temperature_Sensor`、`Temperature_Sensor` | 室内区域空气温度 | °C | **P0 必装** | `AIR_TEMPERATURE_C` 已支持 | HVAC 控制、预冷/预热、舒适度、物理标定 |
| `Relative_Humidity_Sensor`、`Zone_Air_Humidity_Sensor`、`Humidity_Sensor` | 室内相对湿度 | %RH | **P0 必装** | `RELATIVE_HUMIDITY_PCT` 已支持 | 空调除湿—加湿器联动、舒适度 |
| `CO2_Sensor` | 室内 CO₂ | ppm | **P0 必装** | `CO2_PPM` 已支持 | 人数/通风代理、空气质量告警、会议超员检测 |
| `Electrical_Power_Sensor`、`Power_Sensor` | 分设备或房间瞬时有功功率 | W/kW | **P1 建议** | 尚未作为 `SensorQuantity` 接入 | 校验能耗模型、识别空转设备、BOS 能效指标 |
| `Building_Electrical_Meter_Sensor` | 建筑/配电回路累计或总功率 | kW、kWh | **P1 可选** | 尚未接入 | 总能耗基线；若只有全楼总表，对单房间动作归因较弱 |

### 建议的最小部署组合

前期每个主要环境 zone 使用一台组合式温湿度/CO₂ 设备即可：

```text
K1324 office zone: temperature + RH + CO2
```

如果 K1324 面积较大或座位靠近空调出风口，可增加第二个温湿度点，分别放在：

- 主要人员活动区；
- 空调直吹或远离出风口的代表性区域。

CO₂ 传感器应放在人员呼吸带附近，但避免贴近门、窗、送风口或某一个人的固定座位。

## 3. 可由公共网络服务获取、前期可省略设备的室外量

| 表中 ontology（合并） | 建议来源 | 前期是否安装本地设备 | 备注 |
|---|---|---|---|
| `Outdoor_Air_Temperature_Sensor`、`Outside_Air_Temperature_Sensor` | 公共天气 API/校园气象服务 | 可省略 | 用作建筑热模型边界；必须记录数据时间和来源 |
| `Outdoor_Air_Humidity_Sensor`、`Outside_Air_Humidity_Sensor` | 公共天气 API/校园气象服务 | 可省略 | 与室外温度共同用于湿度边界 |
| `Outside_Air_Dewpoint_Sensor` | 天气 API，或由室外温湿度计算 | 可省略 | 不建议为露点单独安装传感器 |
| `Outside_Air_Enthalpy_Sensor` | 由室外温度和湿度计算 | 可省略 | 属于派生量，不是前期硬件需求 |

建议为网络天气数据实现一个 `OutdoorConditionProvider`，而不是伪装成室内
`SensorProvider`。其返回值应包含：

```text
source / observed_at / fetched_at / temperature / RH / data_quality
```

网络服务失败时使用最近有效值并标记 stale。若论文后期需要更精确地研究窗边、屋顶
或楼宇局部微气候，再增加一套本地室外温湿度传感器作为对照。

## 4. 可计算的派生量，不建议单独部署

| 表中 ontology | 计算来源 | 处理建议 |
|---|---|---|
| `Average_Zone_Air_Temperature_Sensor` | 多个 zone 温度测点平均 | 在软件中聚合，不新增硬件 |
| `Warmest_Zone_Air_Temperature_Sensor` | 多个 zone 温度测点取最大值 | 在软件中聚合，不新增硬件 |
| `Air_Enthalpy_Sensor`、`Enthalpy_Sensor` | 空气温度 + 相对湿度 | 在 Observation/Metric 层计算 |
| `Dewpoint_Sensor` | 空气温度 + 相对湿度 | 在 Observation/Metric 层计算 |
| `Outside_Air_Enthalpy_Sensor` | 室外温度 + 室外湿度 | 由天气数据计算 |
| `Chilled_Water_Differential_Temperature_Sensor` | 冷冻水供水温度 − 回水温度 | 有两个水温点时计算 |
| `Demand_Sensor`、`Cooling_Demand_Sensor` | 控制器请求、功率和设定点 | 作为控制/BMS 状态，不作为独立环境传感器采购 |

## 5. HVAC 设备侧可选传感器

以下测点主要用于设备诊断、控制性能和更精细的能耗模型。对于普通分体空调或无法
接入风管/水系统的 K1324，前期可以省略。

### 5.1 风侧温湿度

| 表中 ontology（合并） | 部署条件 | 优先级 | 用途 |
|---|---|---|---|
| `Supply_Air_Temperature_Sensor`、`Discharge_Air_Temperature_Sensor` | 可接近送风口或设备提供点位 | P1 | 判断空调是否真正制冷/制热、估算送风冷量 |
| `Return_Air_Temperature_Sensor` | 有明确回风口或设备点位 | P1/P2 | 估计盘管负荷和区域回风状态 |
| `Mixed_Air_Temperature_Sensor` | 有新风/回风混合箱 | P2 | 分析新风比例和混合过程 |
| `Supply_Air_Humidity_Sensor` | 可接入送风段 | P2 | 标定空调除湿作用 |
| `Return_Air_Humidity_Sensor` | 可接入回风段 | P2 | 标定区域含湿量 |
| `Exhaust_Air_Temperature_Sensor`、`Exhaust_Air_Humidity_Sensor` | 有机械排风系统 | P2 | 排风热湿状态分析 |

`Air_Temperature_Sensor` 和 `Temperature_Sensor` 是泛化 ontology，必须结合设备位置
映射成 zone、supply、return、mixed 或 exhaust，不能直接作为稳定项目字段。

### 5.2 风量与压力

| 表中 ontology（合并） | 部署条件 | 优先级 | 用途 |
|---|---|---|---|
| `Air_Flow_Sensor`、`Discharge_Air_Flow_Sensor` | 有风管或 VAV/新风设备接口 | P2 | CO₂ 稀释和送风热量模型 |
| `Supply_Air_Static_Pressure_Sensor` | 中央风管系统 | P2 | 风机/VAV 控制和故障诊断 |
| `Air_Differential_Pressure_Sensor`、`Differential_Pressure_Sensor` | 有明确压差测点 | P2 | 房间/风管压差及通风诊断 |
| `Filter_Differential_Pressure_Sensor` | 净化器/AHU 滤网两侧可测 | P1/P2 | 滤网堵塞和维护提醒 |

对于独立空气净化器，若厂家 API 已提供滤芯寿命和风量状态，可先使用设备遥测；只有
需要验证厂家估计或研究滤网真实阻力时再增加压差传感器。

### 5.3 冷热水系统

| 表中 ontology（合并） | 部署条件 | 优先级 | 用途 |
|---|---|---|---|
| `Chilled_Water_Temperature_Sensor`、`Chilled_Water_Supply_Temperature_Sensor` | K1324 接入冷冻水系统 | P2 | 冷源供水状态 |
| `Chilled_Water_Return_Temperature_Sensor` | 有回水点位 | P2 | 计算水侧温差和负荷 |
| `Chilled_Water_Flow_Sensor` | 有水管和流量计/BMS 点位 | P2 | 水侧制冷量计算 |
| `Chilled_Water_Differential_Pressure_Sensor` | 有水系统压差点位 | P2 | 泵和阀门诊断 |
| `Hot_Water_Supply_Temperature_Sensor`、`Hot_Water_Temperature_Sensor` | 接入热水供暖 | P2 | 供热状态 |
| `Hot_Water_Return_Temperature_Sensor` | 有回水点位 | P2 | 供热负荷计算 |
| `Discharge_Water_Temperature_Sensor`、`Return_Water_Temperature_Sensor`、`Water_Temperature_Sensor` | 需要先明确具体水回路 | 待定 | 泛化名称，未映射设备前不采购 |

如果 K1324 使用分体式直膨空调而非可接入的冷热水末端，这一整组水侧传感器可以
省略。

## 6. 表格未包含但当前智慧办公场景建议考虑的传感器

这些不来自参考表，但与现有 TODO 和代码目标直接相关：

| 传感器 | 建议 | 当前代码 | 用途 |
|---|---|---|---|
| 人员存在/人数传感器 | P0/P1 建议 | 尚未接入 `SensorQuantity` | 门磁、PIR、毫米波、摄像头匿名计数或门禁事件；用于人数、CO₂ 和会议提前结束 |
| PM2.5 | P1 建议 | `PM25_UG_M3` 已支持 | 净化器闭环和空气质量评价；室外 PM 可先用公共服务 |
| 门窗开闭 | P1 可选 | 尚未接入 | 判断渗透风、门禁和能耗异常 |
| 照度 | P2 可选 | 尚未接入 | 分区照明和会议模式 |
| 噪声级 | P2 可选 | 尚未接入 | 会议舒适度，需处理隐私与录音边界 |

## 7. 推荐分阶段清单

### Phase 0：最小可运行 Demo

```text
实装：室内 temperature + RH + CO2 组合传感器
网络：室外 temperature + RH（并派生 dewpoint/enthalpy）
模拟：PM2.5、功率、人数变化可先由场景和物理模型生成
```

### Phase 1：闭环验证与论文数据

```text
增加：分设备或房间电功率
增加：人员存在/匿名人数
增加：PM2.5
可选：送风温度、第二个区域温湿度点
```

### Phase 2：HVAC 诊断和精细模型

```text
按真实系统类型选择：送/回/混合空气、风量、静压、滤网压差
仅在存在相应系统时选择：冷冻水/热水温度、流量、压差
```

## 8. 与当前代码的差距

当前 `SensorQuantity` 已支持：

```text
AIR_TEMPERATURE_C
RELATIVE_HUMIDITY_PCT
CO2_PPM
PM25_UG_M3
```

若进入 Phase 1/2，需要继续增加：

```text
ELECTRICAL_POWER_W
OCCUPANCY_COUNT
AIR_FLOW_M3_S
AIR_PRESSURE_PA / DIFFERENTIAL_PRESSURE_PA
WATER_TEMPERATURE_C
WATER_FLOW_M3_S
ILLUMINANCE_LUX
```

增加 quantity 时必须同步修改：

1. `smartoffice/physics/building/sensor_api.py` 的 enum 和标准单位；
2. 真实 Adapter 点位映射；
3. 若是模拟量，修改 `ZoneState`/设备状态及 `BuildingObservationModel`；
4. `BuildingSensorApp` 无需为每种 quantity 单独增加 Tool；
5. 增加单位转换、延迟、缺失和 Agent 真值隔离测试。
