# Building 房间模型

本目录保存房间的静态事实、功能分区、受控设备、物理初值和模拟传感器。Scenario
只引用这些配置，不应再次定义房间容量或是否可预约。

| 配置 | 房间类型 | 容量 | `bookable` | 当前定位 |
|---|---|---:|---|---|
| `k1324.yaml` | `graduate_office` | 17 | `false` | 固定研究生办公室 |
| `k1316.yaml` | `seminar_room` | 8 | `true` | 可预约研讨室 |
| `k1315.yaml` | `conference_room` | 20 | `true` | 可预约会议室 |

## K1324

K1324 的 17 个固定位置由以下互斥工作区域构成：

- `k1324_professor_office_01`：单人教师办公室；
- `k1324_professor_office_02`：单人教师办公室；
- `k1324_shared_cubicle`：三个位置的共享隔间；
- `k1324_graduate_workstations`：12 个研究生工位。

K1324 不是共享会议资源，因此预约发现和会议创建都会排除或拒绝该房间。当前四个
功能区映射到一个 `k1324_office_zone`；这表示仿真暂不声称每个隔间具有独立空气
状态。以后取得隔墙、门、送回风和分区传感器数据后，再决定是否拆成多个物理 zone。

## K1316

K1316 作为小型研讨室，初始设为 8 人容量，支持投影、白板和视频研讨。配置包含
HVAC、独立通风、投影和可调光照明。面积与设备功率目前是仿真初值，现场测量后应
直接修改 `k1316.yaml`。

## K1315

K1315 作为正式会议室，承接此前会议 Scenario 使用的完整设备集合：HVAC、加湿器、
净化器、独立通风、演示区与观众区灯光、投影、音响/麦克风和打印机。当前容量设为
20 人，足以运行现有 12 人会议流程。容量、面积和设备额定参数仍需现场确认。

## 配置边界

- `room_type` 描述空间用途；
- `bookable` 是由 `RoomApp` 和 `ScheduleApp` 强制执行的业务规则；
- `functional_areas` 描述人和设备的业务位置；
- `zones` 描述物理空气体积；
- 多个房间配置由 `BuildingWorldRuntime.from_room_configurations()` 合并到同一时钟、
  物理引擎和 SensorHub 中。
