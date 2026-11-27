"""Real-process acceptance; no transport or Agent response mocks."""

import json
import platform
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from .cluster import LocalCluster, request
from .core import ROOMS, write_json


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def events(directory: Path, role: str):
    path = directory / f"{role}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("expected event was not observed before deadline")
        time.sleep(0.05)


def check_completed(results):
    require(all(row.get("status") == "completed" for row in results), f"task failures: {results}")
    require(
        all(row["room_id"] == row["state"]["room_id"] for row in results),
        "room result was routed incorrectly",
    )


def exercise(cluster: LocalCluster, *, full: bool):
    checks = []

    def passed(name, **evidence):
        checks.append({"name": name, "passed": True, **evidence})
        print(f"PASS {name}", flush=True)

    pings = cluster.tasks(*(request(room, "ping") for room in ROOMS))
    check_completed(pings)
    require(all(row["answer"] == "pong" for row in pings), "missing pong")
    pids = [row["worker_pid"] for row in pings]
    coordinator_pid = cluster.identities["coordinator"]["pid"]
    require(
        pids == [cluster.identities[room.lower()]["pid"] for room in ROOMS],
        "result PID does not match the launched room interpreter",
    )
    require(len(set(pids + [coordinator_pid])) == 3, "Agents must be separate processes")
    passed(
        "xmpp_ping_pong_and_process_isolation", agent_pids=pids + [coordinator_pid], results=pings
    )
    if not full:
        return checks

    tasks = [request(room) for room in ROOMS for _ in range(10)]
    results = cluster.tasks(*tasks)
    check_completed(results)
    require(len({row["task_id"] for row in results}) == 20, "task IDs collided")
    passed("twenty_routed_tasks", completed=len(results), results=results)

    duplicate_id = uuid4().hex
    duplicate = cluster.tasks(
        request(
            "K1315",
            "set_test_temperature",
            repeats=2,
            task_id=duplicate_id,
            payload={"temperature_c": 29.5},
        )
    )
    check_completed(duplicate)
    wait_until(
        lambda: any(
            row["event"] == "duplicate_replayed" and row.get("task_id") == duplicate_id
            for row in events(cluster.directory, "k1315")
        )
    )
    executions = [
        row
        for row in events(cluster.directory, "k1315")
        if row["event"] == "task_executed" and row.get("task_id") == duplicate_id
    ]
    require(len(executions) == 1, "duplicate message executed more than once")
    passed("duplicate_executes_once", task_id=duplicate_id, executions=len(executions))

    states = cluster.tasks(*(request(room) for room in ROOMS))
    check_completed(states)
    require(
        [row["state"]["temperature_c"] for row in states] == [29.5, 24.0],
        "room state leaked between workers",
    )
    passed("room_state_isolation", results=states)

    invalid = cluster.tasks(request("K1315", "unsupported_operation"))
    require(invalid[0]["status"] == "failed", "unsupported operation was accepted")
    try:
        cluster.tasks(request("K1315"), {"room_id": "K1316", "unexpected": True})
    except RuntimeError:
        pass
    else:
        raise AssertionError("malformed batch was accepted")
    require(
        cluster.tasks(request("K1315"))[0]["status"] == "completed",
        "room did not survive invalid operation",
    )
    passed("invalid_task_isolated", result=invalid[0])

    late_id = uuid4().hex
    delayed = cluster.tasks(
        request("K1316", timeout=0.2, task_id=late_id, payload={"test_delay_seconds": 0.7}),
        request("K1315"),
    )
    require(delayed[0]["status"] == "timed_out", "delayed task did not time out")
    require(delayed[1]["status"] == "completed", "independent room was blocked")
    wait_until(
        lambda: any(
            row["event"] == "terminal_message_ignored"
            and row.get("task_id") == late_id
            and row.get("message_type") == "result"
            for row in events(cluster.directory, "coordinator")
        )
    )
    ledger = cluster.call("ledger")[late_id]
    require(
        ledger["status"] == "timed_out" and ledger["late_messages"] > 0,
        "late result overwrote the terminal timeout",
    )
    passed("timeout_remains_terminal_after_late_result", results=delayed, ledger=ledger)

    old_pid = cluster.identities["k1316"]["pid"]
    cluster.stop_process("k1316")
    offline = cluster.tasks(request("K1316", timeout=1), request("K1315"))
    require(offline[0]["status"] == "timed_out", "offline task did not time out")
    require(900 <= offline[0]["elapsed_ms"] < 2500, "offline timeout was not bounded")
    require(offline[1]["status"] == "completed", "offline room blocked healthy room")
    passed("offline_room_does_not_block_healthy_room", results=offline)

    cluster.start_process("room", "K1316")
    restarted = cluster.tasks(request("K1316"))
    check_completed(restarted)
    require(restarted[0]["worker_pid"] != old_pid, "room process did not restart")
    require(
        restarted[0]["worker_pid"] == cluster.identities["k1316"]["pid"],
        "restart response did not come from the new interpreter",
    )
    passed("new_task_after_room_restart", previous_pid=old_pid, results=restarted)
    return checks


def run(args):
    repeats = args.repeat if args.role == "acceptance" else 1
    if not 1 <= repeats <= 10:
        raise ValueError("repeat must be between 1 and 10")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6]
    directory = args.output.resolve() / f"{args.role}-{stamp}"
    directory.mkdir(parents=True)
    report = {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "spade": version("spade"),
        "pyjabber": version("pyjabber"),
        "room_data_source": "synthetic_fixture",
        "llm_enabled": False,
        "passed": False,
        "runs": [],
    }
    failed = False
    for index in range(repeats):
        entry = {"index": index + 1, "passed": False}
        cluster = None
        try:
            cluster = LocalCluster(directory / f"run-{index + 1}")
            with cluster:
                entry["checks"] = exercise(cluster, full=args.role == "acceptance")
            require(
                all(process.returncode == 0 for process in cluster.processes.values()),
                "a worker failed or required forced cleanup; inspect console logs",
            )
            entry["passed"] = True
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            failed = True
            print(entry["error"], flush=True)
        finally:
            if cluster is not None:
                entry["processes_started"] = cluster.starts
                entry["ports"] = [cluster.port, cluster.s2s_port]
                entry["service"] = cluster.identities.get("xmpp", {})
                entry["cleanup"] = {
                    key: process.poll() for key, process in cluster.processes.items()
                }
            report["runs"].append(entry)
            write_json(directory / "report.json", report)
        if failed:
            break
    report["passed"] = not failed and len(report["runs"]) == repeats
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(directory / "report.json", report)
    print(f"Report: {directory / 'report.json'}", flush=True)
    return 0 if report["passed"] else 1
