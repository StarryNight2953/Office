# Building MQTT Sensor Simulator

该目录提供独立于物理内核的 MQTT 传感器模拟进程。数据来自现有
`BuildingWorldRuntime -> BuildingObservationModel`，不是另一套随机环境模型。

## 数据路径

```text
Building Runtime -> SensorReading -> MqttSensorPublisher -> MQTT Broker
MQTT Broker -> MqttSensorSubscriber -> MqttSensorAdapter -> SensorHub
```

默认加载 K1324、K1316 和 K1315，发布温度、相对湿度、CO₂，以及配置中已有的
PM2.5。Topic 形式为：

```text
kechuang/<room_id>/environment/<temperature|humidity|co2|pm25>
```

Payload 示例：

```json
{"sensor_id":"k1324_temp_01","zone_id":"k1324_office_zone","quantity":"air_temperature_c","value":24.6,"unit":"degC","observed_at":"2026-08-17T10:30:00+00:00","quality":"good"}
```

## 启动

先启动本地 Broker，例如 Mosquitto，再在 `are` 环境运行：

```bash
conda run -n are env PYTHONPATH=. python -m smartoffice.simulators.building \
  --broker localhost \
  --port 1883 \
  --step-seconds 60
```

默认是实时模式：每 60 秒推进 60 秒物理时间。快速联调时可以取消等待并限制步数：

```bash
conda run -n are env PYTHONPATH=. python -m smartoffice.simulators.building \
  --broker localhost \
  --accelerated \
  --steps 10
```

用 `--room-config` 可重复指定房间；未指定时使用三个科创空间房间。生产 Broker
还可配置 `--username`、`--password` 和 `--tls`。密码只通过命令行参数传入，不应
提交进仓库。

## 接入 Building Runtime

订阅端使用与模拟器相同的 topic 构造函数，避免发布/订阅配置漂移：

```python
from smartoffice.adapters.building import (
    MqttConnectionSettings,
    MqttSensorAdapter,
    MqttSensorSubscriber,
)
from smartoffice.simulators.building import build_sensor_point_mappings

mappings = build_sensor_point_mappings(configurations)
adapter = MqttSensorAdapter(mappings)
subscriber = MqttSensorSubscriber(
    MqttConnectionSettings(host="localhost", port=1883),
    adapter,
)
runtime.sensors.register_provider("kechuang_mqtt", adapter, priority=100)
runtime.configure_sensor_shadow(reference_source="kechuang_mqtt")
subscriber.start()
```

进程退出时必须调用 `subscriber.stop()`。真实 MQTT Provider 使用较高优先级后，
Agent 读取真实/回环 MQTT 数据；Runtime 内部观测仍保留，可通过
`SensorHub.read_by_source()` 做 Shadow Mode 对比。
Runtime 每个物理时间步会把新匹配样本写入 `sensor_shadow` trace，并通过统一 Building
指标按 zone/quantity 分别输出 bias、MAE、RMSE、最大绝对误差和时间偏移，不会
混合温度、湿度与 ppm 等不同单位。默认只比较采样时间相差不超过 300 秒的读数。
Shadow Mode 只比较观测，不会用实测值覆盖物理真值。

## 时间模式

- 默认实时模式让墙钟等待时间和物理时间步一致，适合 Broker/网络联调。
- `--accelerated` 只加速物理时钟，不等待墙钟，适合协议吞吐和场景回归测试。
- MQTT Payload 中的 `observed_at` 始终取 Runtime 的带时区模拟时间；订阅端的
  `available_at` 取实际接收时间。将订阅端注册进另一个模拟 Runtime 时，需要确保
  读取时钟不早于接收时间。
