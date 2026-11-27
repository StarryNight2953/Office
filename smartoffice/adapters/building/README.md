# Building Sensor Integration

完整 Building World 分层、会议流程和扩展说明见
`smartoffice/apps/building_world/README.md`。本文只说明传感器 Adapter 边界。

真实协议客户端不应进入物理引擎。客户端只负责连接和订阅，然后把收到的消息交给
对应 Adapter：

```python
from smartoffice.adapters.building import MqttSensorAdapter, SensorPointMapping
from smartoffice.apps.building_world import SensorHub
from smartoffice.physics.building import SensorQuantity

adapter = MqttSensorAdapter(
    [
        SensorPointMapping(
            point_address="k13/k1324/environment/temperature",
            sensor_id="k1324_temp_01",
            zone_id="k1324_office_zone",
            quantity=SensorQuantity.AIR_TEMPERATURE_C,
            source_unit="degC",
        )
    ]
)

# 网络客户端的消息回调只需转发：
adapter.on_message(topic, payload, received_at=received_at)

hub = SensorHub(["k1324_office_zone"], published_priority=0)
hub.register_provider("k1324_mqtt", adapter, priority=100)
```

模拟模式下，给 `BuildingPhysicsOrchestrator` 传入同一个 Hub：

```python
runtime = BuildingPhysicsOrchestrator(
    engine,
    observation_model=observation_model,
    observation_sink=hub,
)
```

Orchestrator 会将模拟读数发布到 Hub。若真实 Provider 使用更高 priority，合并视图
会选择真实读数；`hub.read_by_source(request)` 仍保留各来源，供 Shadow Mode 计算
仿真误差。

`BuildingSensorApp` 是 Agent-facing 边界，只序列化 `SensorReading`。内部
`ObservationSample.truth_value` 不会出现在 Tool 返回值中。

## 时间语义

- `observed_at`：设备实际采样时间，优先使用设备时间戳；
- `available_at`：网关收到并可对外提供数据的时间；
- `SensorProvider.read()` 仅返回 `available_at <= request.at_time` 的读数；
- 生产部署应统一使用带时区的 UTC 时间，并在网关监控设备时钟漂移。

## 网络客户端边界

`mqtt_transport.py` 提供基于 paho-mqtt 的通用发布/订阅客户端，并且保持延迟导入，
因此纯物理仿真不会加载网络 SDK。客户端负责连接、认证、重连和订阅；Adapter
负责点位映射、单位转换、质量码和缓存。完整模拟器和运行命令见
`smartoffice/simulators/building/README.md`。
