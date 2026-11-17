# Office 场景实验室

基于现有 FAIRY Building World 的本地浏览器工作台：Three.js 显示和编辑空间，Python 管理设备、Agent 和房间级物理状态。

## 启动

需要 Python 3.10+、Node.js 20.19+ 或 22.12+，以及支持 WebGL 的浏览器。建议使用独立 Python 环境。

在项目根目录安装 Python 依赖并构建前端：

```bash
python -m pip install -r requirements-visualization.txt
npm --prefix web install
npm --prefix web run build
python -m fairy.visualization
```

打开 **http://127.0.0.1:8000**。后端同时提供构建后的网页、模型资产和 API；API 文档位于 `/docs`。服务默认仅监听本机，是单进程、单用户、单活动实验的工作台。

开发时可以分别启动两个终端：

```bash
# 终端一，在项目根目录
python -m fairy.visualization

# 终端二，在项目根目录
npm --prefix web run dev
```

打开 **http://localhost:5173**。Vite 将 `/api` 的 HTTP 和 WebSocket 请求代理到 Python 服务。不要对 Python 服务启用多个 Uvicorn worker，否则各进程会拥有不同实验。

## 使用流程

1. 选择 K1315、K1316 或 K1324。初始房型与家具位置均为示意布局，尺寸来自现有房间面积的矩形近似，不是实测平面图。
2. 从物体库添加家具或设备外观，通过三维控件或属性面板设置位置、旋转、缩放；支持俯视和三维视角。
3. 将设备外观绑定到已有逻辑设备。灯光分区可以对应多个视觉灯具，它们共用状态与功率统计。复制视觉灯具不会创建新的业务设备。
4. 设置灯或空调的初始状态，保存场景。进入运行模式时会先保存，然后创建独立实验。
5. 在运行模式中应用设备设置，点击时间推进按钮，观察环境读数、能耗和事件。运行时物体布局保持固定。
6. 在 Agent 面板运行“内置演示流程”，或配置模型后运行真正的 FAIRY Agent。重置会根据**最近保存的配置**创建新实验，并清空本次运行轨迹。

首版可写设备为灯和空调；其他已配置设备支持布置和状态查看。K1324 原始房间配置没有灯光逻辑设备，添加灯具外观不会自动补造设备，需在后续扩展房间业务配置。

内置演示流程是明确标注的确定性演示，不调用模型：打开灯与空调，经多步时间推进观察温度变化。手动控制和演示流程都不需要模型密钥。

## 真实 Agent

在启动 Python 服务前设置服务端环境变量；不要把密钥写入前端或提交到 Git：

```bash
export OFFICE_LLM_MODEL="your-model-name"
export OFFICE_LLM_BASE_URL="https://your-provider.example/v1"
# 按你的本地密钥管理方式设置 OFFICE_LLM_API_KEY，或使用已有 OPENAI_API_KEY。
python -m fairy.visualization
```

`OFFICE_LLM_BASE_URL` 可省略以使用 OpenAI 默认端点。这里复用仓库的 `OpenAICompatibleClient`，模型需支持工具调用。安装了 OpenAI SDK 且模型名、密钥已配置时，网页启用“运行 Agent”。

例如接入 DeepSeek 时，设置 `OFFICE_LLM_MODEL=deepseek-chat`、`OFFICE_LLM_BASE_URL=https://api.deepseek.com`，并通过本地密钥管理方式设置 `OFFICE_LLM_API_KEY`。就绪检查只确认服务端配置与 SDK 是否齐全，不代表已经通过联网认证。网页会显示缺少模型名、密钥或 SDK 的具体原因。

Agent 通过现有的设备查询、灯光、空调、传感器和时间工具操作**当前房间实验**；事件面板显示其真实工具执行过程。此入口不是现有所有 L1/L3 benchmark 的批量运行器。每轮最多 30 次工具调用、180 秒墙钟时间；单次网络请求超时 20 秒，不进行 SDK 自动重试。停止在工具边界生效，等待模型响应时不能立即抢占。

完成后 Agent 面板显示模型返回的最终摘要；认证失败、限流、连接失败、超时及执行失败会分别报告，不把失败显示成已完成。模型摘要是对观测的解释，仍应以环境面板的传感器读数核对是否达到目标。服务不会把模型服务的原始错误响应发给浏览器。

当前聊天中的模型也可以通过外部工具脚本操作这个边界：无浏览器时调用 `Runner.snapshot()`、`command()`、`advance()`；与浏览器联动时使用下表的 HTTP API。辅助脚本、会话日志和验证报告放在仓库外部。该方式由聊天会话逐轮选择操作，脚本本身不包含模型，也不自动调用当前聊天账户。

## 数据与状态边界

| 数据 | 保存位置 / 所有者 |
|---|---|
| 已有设备、额定功率、房间物理参数 | 原 `fairy/configs/rooms/` 配置 |
| 视觉布局、GLB 引用、设备绑定、初始命令 | `.office/scenes/<room_id>.json` |
| 当前设备状态 | 本次 Runner 内的 `BuildingWorldApp` |
| 环境演化与观测 | 现有 Building Runtime、Physics、SensorHub |
| 运行事件与浏览器曲线 | 有界内存；重置后重新开始 |

可以用 `OFFICE_SCENE_DIR` 改变布局存储目录。保存使用临时文件和原子替换；无效绑定、重复视觉 ID、非有限坐标及非法初始参数会被拒绝。没有保存文件时，由现有房间配置生成默认布局。

布局版本为 `schema_version: 1`。物体使用独立 `id`，通过可选 `device_id` 关联逻辑设备；位置单位为米、Y 轴向上，旋转以 XYZ 欧拉角**弧度**保存，属性面板以度显示。`initial_states` 按逻辑设备 ID 保存命令参数，与运行状态分开。

浏览器命令进入一个拥有全部可变状态的 Python 执行线程，复用现有 App 参数校验和事件。Agent 工具与人工命令在工具边界串行执行，浏览器等待后端确认再改变设备表现。

该工作台使用保持原 TimeManager 接口的显式仿真时钟：仅时间工具推进时钟与物理，浏览器空闲和模型思考不增加模拟时间。运行实例使用固定开始时间、观测随机种子，便于重复实验，不改变原有 CLI 的时钟行为。

显示层只输出设备状态、SensorHub 观测、累计能耗及公开事件，不直接序列化内部物理 checkpoint。设定温度与测得室温分开显示；空调气流动画和灯具亮度是状态反馈，位置变化不影响现有房间级温湿度模型。

重置会隔离旧运行实例。正在等待网络的旧 Agent 可在请求结束后退出，但不能访问新实例；布局保存也不会把当前设备状态写回初始配置。

## Blender / GLB 模型

默认家具和设备使用程序生成的几何体，无外部图片或模型下载依赖。需要更准确外观时：

1. 在 Blender 中制作模型，以米为单位，应用物体缩放。
2. 家具模型原点放在地面底部；灯、空调等墙面/吊装设备原点放在本体中心。
3. 导出 glTF 2.0 的 `.glb` 文件，使用标准 Y-up 导出转换，避免再手动重复旋转轴向。
4. 放到 `web/public/models/`，例如 `web/public/models/air-conditioner.glb`。
5. 在物体的 GLB 路径中填写 `/models/air-conditioner.glb`，保存。正式运行需重新构建前端并重启 Python 服务。

模型资产不保存设备业务状态。替换外观时保留视觉 `id` 和 `device_id`；资源载入失败时使用内置几何体作为回退。

## API

| 接口 | 行为 |
|---|---|
| `GET /api/catalog` | 已有房间、设备、物体库、`agent_configured` 与详细的 `agent_readiness` |
| `GET/PUT /api/scenes/{room_id}` | 读取、校验并保存布局与初始状态 |
| `POST /api/runs` | 按保存的 `scene_id` 创建实验 |
| `GET /api/runs/current` | 获取当前实验，没有时返回 `null` |
| `GET /api/runs/{run_id}` | 公开展示状态快照 |
| `POST /api/runs/{run_id}/commands` | `{device_id, parameters}`，调用现有设备 App |
| `POST /api/runs/{run_id}/advance` | `{seconds}`，单次 1–3600 整秒 |
| `POST /api/runs/{run_id}/agent` | `{mode: "demo" 或 "llm", task?}` |
| `POST /api/runs/{run_id}/stop` | 请求在安全边界停止流程 |
| `POST /api/runs/{run_id}/reset` | 使用保存配置重建，返回新的 run ID |
| `WS /api/runs/{run_id}/events` | 连接时全量同步，此后发送变化后的展示快照 |

WebSocket 消息为 `{type: "snapshot", data: ...}`，包含递增 `revision`，不包含内部真值。旧实验 ID 在重置后失效。

页面切换到新实验时会立即更新期望的 run ID，忽略随后到达的旧实验响应，避免重置后被旧状态覆盖。

`agent_readiness` 包含 `configured`、`model`、`reason`、`reason_code`；配置完整时后两项为 `null`。运行快照中的 `agent.summary` 为可选的最终结果，`agent.error_code` 为可选错误类别。布局版本仍为 1。

## 验证

```bash
# 新增测试也可独立用标准库 unittest 执行
python -m unittest discover -s tests -p 'test_visualization_*.py' -v

# 已安装完整开发依赖时
python -m pytest tests/test_visualization_*.py tests/test_building_runtime.py tests/test_building_indoor_environment_engine.py tests/test_building_sensor_integration.py tests/test_building_conference_tools.py -q

npm --prefix web run typecheck
npm --prefix web run build
```

新增测试覆盖配置往返、共享灯组、非法绑定、命令拒绝、物理与命令状态分离、环境推进、重置、队列并发、三次独立演示的一致性、Agent 工具循环、最终摘要与错误类别、HTTP 和 WebSocket 全量重连。Agent 离线测试只替换外部模型响应，实际使用 FAIRY Agent 和真实设备/物理工具。

浏览器验收工具独立分发，放在仓库同级的 `office-tools/`，不写入项目代码目录：

```bash
# 只检查环境；不会安装依赖或创建实验
python -B ../office-tools/browser_acceptance.py --repo . --preflight-only

# 首次生成真实 npm 锁文件，再干净安装、类型检查、构建和运行浏览器验收
python -B ../office-tools/browser_acceptance.py --repo . --prepare
```

完整验收需要本机端口访问、npm 依赖下载、Python Playwright 和可用的 Chromium。工具使用独立场景存储与端口，截图、会话记录和日志保存在项目外；`blocked`、`failed`、`passed` 分别记录，不将语法转换或 ASGI 测试算作浏览器通过。首次成功生成锁文件后，应把真实 `web/package-lock.json` 纳入版本控制，后续安装使用 `npm ci`。

第一版不包含真实硬件控制、历史时间轴回放、空间气流/照度计算、多人协作或新增业务房间/设备的编辑器。
