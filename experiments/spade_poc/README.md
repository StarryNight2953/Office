# Office-kun SPADE 通信原型

对应 `results.md` 第 5.1 节：一个 Building Coordinator 和两个 Room Agent（K1315、K1316）各自独立进程，通过真实本机 XMPP 服务交换任务和结果。

现在提供两种模式：第一阶段的 `demo/acceptance` 使用 `synthetic_fixture` 验证通信；新增的 `building-demo/building-acceptance` 接入原项目的共享 Building 世界、设备工具和物理反馈，并通过 SPADE-LLM 调用本机模拟模型接口。模拟模型按脚本返回工具调用，不进行真实推理。详细说明见 [BUILDING.md](BUILDING.md)。

## 直接运行（当前机器已安装）

在项目根目录执行，启动器会自动选择原型的独立 Python：

```powershell
python experiments/spade_poc/run.py demo
python experiments/spade_poc/run.py acceptance --repeat 3
python experiments/spade_poc/run.py building-demo
python experiments/spade_poc/run.py building-acceptance
```

`demo` 完成两个房间的 ping/pong 后退出。`acceptance` 默认完整复现三轮，每轮有 8 类检查。运行时自动选择空闲端口、生成会话口令和本地证书、启动服务及 Agent，并在结束后关闭本轮进程。

结果写入 `experiments/spade_poc/artifacts/<运行标识>/`，终端打印 `report.json` 的确切路径。检查失败会返回非零退出码并保留日志。

已完成的实跑记录见 [VALIDATION.md](VALIDATION.md)，通信样例见 [communication.sample.jsonl](examples/communication.sample.jsonl)。

## 新机器安装与复现

需要 uv 和可用的 Python 3.11/3.12。项目固定 SPADE 4.1.4，所有传递依赖由 `uv.lock` 锁定。此次实际验证使用 CPython 3.11.14 / Windows。

```powershell
Set-Location experiments/spade_poc
uv sync --locked --python 3.11
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m office_spade acceptance --repeat 3
```

Linux/macOS 使用 `.venv/bin/python`，或者从项目根目录使用 `python experiments/spade_poc/run.py ...`。本轮尚未在 Linux/macOS 实跑。

Python 3.11 用于满足 SPADE-LLM 的版本要求；依赖已纳入隔离环境的 pyproject.toml 和 uv.lock。原项目的 Python 环境与依赖文件仍可独立使用。

## 进程与职责

| 进程 | 职责 |
|---|---|
| XMPP / PyJabber | 注册、认证、在线状态与真实消息路由；仅监听 127.0.0.1 |
| Coordinator | 同时分派房间任务、跟踪截止时间、按任务编号汇总结果 |
| K1315 Room Agent | 串行处理本房间任务，维护本地测试状态和去重缓存 |
| K1316 Room Agent | 独立处理本房间任务，维护独立状态与缓存 |

Coordinator 可以并发等待不同房间。每个房间串行处理任务；Building 模式中，世界状态统一由独立环境服务管理，原有房间权限工具在服务端校验资源归属。模型服务可由各房间共享，各任务的对话上下文独立。

`run.py` 是便捷启动器，`cluster.py` 管理子进程生命周期，`worker.py` 封装服务器启动，`agents.py` 实现 SPADE behaviours，`core.py` 定义协议与测试状态适配器，`acceptance.py` 执行真实进程验收。

## 消息和任务约定

```json
{"schema_version":1,"message_id":"unique-message-id","task_id":"unique-task-id","room_id":"K1315","type":"task","payload":{"operation":"get_state"}}
```

消息类型为 `task/ack/result/error`。XMPP metadata 的 ontology 为 `office-spade/1`，thread 与 task_id 一致；接收方同时校验发送者、房间和任务关系。

第一阶段可用操作：`ping`、`get_state`、`set_test_temperature`（`temperature_c` 为 10–40 的有限数值）。`test_delay_seconds`（0–10）仅用于注入处理延迟。修改温度只修改本地测试数值。Building 模式支持 `ping/get_state/building_tool/llm_task`，见 [BUILDING.md](BUILDING.md)。

任务状态为 `pending → acknowledged → completed/failed`，也可在截止后进入 `timed_out`。ACK 不延长截止时间。超时后的结果写入日志，保留超时终态。超时表示协调者停止等待，不会强行撤回已在房间执行的任务。

相同 task_id、相同 payload 的重发返回缓存结果；相同编号携带不同 payload 会被拒绝。去重仅覆盖当前房间进程生命周期，最多保留 1000 条任务；容量满时拒绝新任务，避免驱逐缓存后重复执行。重启会清空测试状态与缓存，新任务可以继续提交。

## 手动启动与发任务

从 `experiments/spade_poc` 目录使用四个终端。每个终端设置同一个临时 `OFFICE_SPADE_PASSWORD`（使用自己的测试口令），选择同一个全新的 `--run-dir`。示例端口须保持空闲。

```powershell
# 每个终端先设置相同的本地测试口令
$env:OFFICE_SPADE_PASSWORD = '<your-local-session-password>'

# 终端 1
.\.venv\Scripts\python.exe -m office_spade xmpp --run-dir artifacts/manual-01 --port 15222 --s2s-port 15269
# 终端 2
.\.venv\Scripts\python.exe -m office_spade room --room K1315 --run-dir artifacts/manual-01 --port 15222
# 终端 3
.\.venv\Scripts\python.exe -m office_spade room --room K1316 --run-dir artifacts/manual-01 --port 15222
# 终端 4
.\.venv\Scripts\python.exe -m office_spade coordinator --run-dir artifacts/manual-01 --port 15222
```

在终端 4 输入一行 JSON，回车后查看结构化结果：

```json
{"request_id":"example-1","command":"tasks","tasks":[{"room_id":"K1315","payload":{"operation":"get_state"}},{"room_id":"K1316","payload":{"operation":"get_state"}}]}
```

该 stdin 控制入口只属于本地 Coordinator；Coordinator 与房间之间的任务和结果均经 XMPP。`command: "ledger"` 可查询任务状态。单次 tasks 请求最多 50 条任务，单任务等待上限可设置为 0.05–180 秒；传入 `repeats: 2` 可重发同一任务。

可在另一个终端创建对应停止文件：

```powershell
New-Item artifacts/manual-01/k1316.stop -ItemType File
```

重新启动 K1316 前，仅删除本次目录里的 `k1316.stop` 与 `k1316.ready.json`。完整退出时先停止 Coordinator（在空白输入行按 Ctrl+Z、回车发送 EOF），再创建两个房间及 XMPP 的 `.stop` 文件。自动 demo/acceptance 会负责这些操作，推荐优先使用自动入口。

## 验收与日志

| 检查 | 验收依据 |
|---|---|
| 真实通信和独立进程 | 两个 pong；返回 PID 对应实际启动的房间解释器，三个 Agent PID 各不相同 |
| 20 条任务 | 每房间 10 条均完成，task_id 唯一、结果房间匹配 |
| 去重 | 同一个写任务发送两次，执行日志仅一条，存在缓存重放记录 |
| 状态隔离 | K1315 修改为 29.5°C，K1316 仍为 24°C |
| 非法任务 | 返回 failed，房间随后仍能完成合法任务 |
| 超时和迟到结果 | 延迟任务超时，迟到 result 被记录且不覆盖终态 |
| 房间离线 | K1316 退出后，K1315 正常完成，K1316 在 1 秒配置超时后结束 |
| 重新上线 | K1316 新进程完成新任务，验证解释器 PID 已变化 |

每个 Agent 的 JSONL 记录 UTC 时间、实际进程 PID、task_id、room_id、消息类型和任务状态；`task_executed` 与 `duplicate_replayed` 区分执行和重发。`xmpp.jsonl` 记录真实服务端在线状态变化。`*.console.log` 用于诊断底层错误，`report.json` 保存验收结果、端口、进程与清理退出码。

## 已处理的依赖边界

为 PyJabber 显式增加 `sqlalchemy[asyncio]`，确保安装 greenlet。启动适配器针对锁定的 PyJabber 0.4.5 处理 loopback 额外监听、可选组件单例初始化，并生成会话证书。没有修改安装包源码，也没有启动可选 HTTP 管理服务。

本地证书使用会话自签名模式，Agent 关闭证书校验；该配置仅面向本机原型。运行日志和证书所在 artifacts 目录已被 Git 忽略。

官方参考：[SPADE Quick Start](https://spade-mas.readthedocs.io/en/latest/usage.html)、[SPADE 项目](https://github.com/javipalanca/spade)、[SPADE-LLM 安装要求](https://spadeagents.eu/docs/spade_llm/getting-started/installation/)。
