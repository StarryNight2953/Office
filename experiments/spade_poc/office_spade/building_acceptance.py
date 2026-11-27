"""Shared-world acceptance, with an explicit mock model or configured live endpoint."""

import json
import platform
from contextlib import nullcontext
from datetime import datetime, timezone
from importlib.metadata import version
from uuid import uuid4

from .acceptance import check_completed, require
from .building import BuildingClient
from .cluster import LocalCluster, request
from .core import ROOMS, write_json
from .mock_model import MockModelServer
from .model import public_readiness


def hvac(state):
    return next(item for item in state["context"]["devices"] if item["device_type"] == "hvac")


def measured_temperature(state):
    readings = [reading for zone in state["observations"] for reading in zone["readings"]]
    return next(
        reading["value"]
        for reading in readings
        if reading["quantity"] in {"temperature", "air_temperature_c", "temperature_c"}
    )


def exercise(cluster, *, mock, full):
    checks = []

    def passed(name, **evidence):
        checks.append({"name": name, "passed": True, **evidence})
        print(f"PASS {name}", flush=True)

    clients = {
        room: BuildingClient(
            f"http://127.0.0.1:{cluster.world_port}", cluster.world_tokens[room], room
        )
        for room in ROOMS
    }
    states = cluster.tasks(*(request(room) for room in ROOMS))
    check_completed(states)
    require(
        all(item["state"]["data_source"] == "building_simulation" for item in states),
        "fixture room state leaked into Building mode",
    )
    require(
        len(cluster.identities["building"]["world_rooms"]) == 3, "canonical world missing rooms"
    )
    first, second = states[0]["state"], states[1]["state"]
    first_device, second_device = hvac(first), hvac(second)
    passed(
        "shared_canonical_world_and_sensor_observations",
        states=states,
        service=cluster.identities["building"],
    )

    if full:
        foreign = cluster.tasks(
            request(
                "K1315",
                "building_tool",
                payload={
                    "name": "HvacApp__set_hvac",
                    "arguments": {
                        "device_id": second_device["device_id"],
                        "power_on": True,
                        "mode": "cooling",
                        "target_temperature_c": 24.0,
                        "fan_level": 3,
                    },
                },
            )
        )
        require(foreign[0]["status"] == "failed", "foreign-room tool write was accepted")
        require(hvac(clients["K1316"].snapshot()) == second_device, "foreign room state changed")
        wrong_identity = BuildingClient(
            f"http://127.0.0.1:{cluster.world_port}", cluster.world_tokens["K1315"], "K1316"
        )
        try:
            wrong_identity.snapshot()
        except ValueError:
            pass
        else:
            raise AssertionError("room capability accepted a different room ID")
        try:
            clients["K1315"].request("advance", seconds=60)
        except ValueError:
            pass
        else:
            raise AssertionError("room advanced the shared global clock")
        passed("room_permissions_and_coordinator_clock_authority", rejected=foreign)

    expectations = [
        {
            "device_id": first_device["device_id"],
            "power_on": True,
            "mode": "cooling",
            "target_temperature_c": 24.0,
            "level": 3,
        }
    ]

    def model_request(room, task, expected):
        return request(
            room, "llm_task", timeout=120, payload={"task": task, "expectations": expected}
        )

    if full and mock:
        claim = cluster.tasks(
            model_request("K1315", "MOCK_CLAIM_ONLY: prepare this room", expectations)
        )
        require(claim[0]["status"] == "failed", "unsupported model completion claim was accepted")
        require(not claim[0]["verification"]["passed"], "false claim passed canonical verification")
        passed("model_text_cannot_fake_completion", result=claim[0])

    task = model_request(
        "K1315", "请将本房间空调开启为制冷，设定24摄氏度、风量3档，读取设备状态核验。", expectations
    )
    task["repeats"] = 2
    outcome = cluster.tasks(task)
    check_completed(outcome)
    require(outcome[0]["verification"]["passed"], "device postconditions unmet")
    require(
        outcome[0]["tool_calls"] >= 2 and outcome[0]["llm_calls"] >= 2,
        "model path did not perform the tool execution/readback cycle",
    )
    require(hvac(clients["K1316"].snapshot()) == second_device, "unassigned room was changed")
    passed("spade_llm_tool_execution_and_readback", result=outcome[0])

    before = clients["K1315"].snapshot()
    administrator = BuildingClient(f"http://127.0.0.1:{cluster.world_port}", cluster.world_admin)
    administrator.request("advance", seconds=600)
    after = cluster.tasks(request("K1315"))[0]["state"]
    initial_temperature, final_temperature = (
        measured_temperature(before),
        measured_temperature(after),
    )
    require(final_temperature < initial_temperature, "cooling did not reduce observed temperature")
    passed(
        "physical_feedback_after_coordinator_time_advance",
        before=initial_temperature,
        after=final_temperature,
        observations=after["observations"],
    )

    if full:
        second_expectations = [
            {
                "device_id": second_device["device_id"],
                "power_on": True,
                "mode": "cooling",
                "target_temperature_c": 25.0,
                "level": 3,
            }
        ]
        other = cluster.tasks(
            model_request(
                "K1316", "开启本房间空调为制冷25度、风量3档并复查设备状态。", second_expectations
            )
        )
        check_completed(other)
        require(
            hvac(clients["K1315"].snapshot())["target_temperature_c"] == 24,
            "second agent changed the first room",
        )
        passed("second_room_independent_model_context_same_world", result=other[0])
        if mock:
            error = cluster.tasks(
                model_request("K1315", "MOCK_HTTP_ERROR: prepare room", expectations)
            )
            require(
                error[0]["status"] == "failed" and error[0].get("error_code"),
                "provider failure was reported as successful",
            )
            passed("provider_failure_is_reported", result=error[0])
    return checks


def run(args):
    mock = args.role != "llm-demo"
    if not mock and not public_readiness()["configured"]:
        print(json.dumps(public_readiness(), ensure_ascii=False))
        return 2
    if not 1 <= args.repeat <= 5:
        raise ValueError("repeat must be between 1 and 5")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6]
    directory = args.output.resolve() / f"{args.role}-{stamp}"
    directory.mkdir(parents=True)
    report = {
        "passed": False,
        "inference_kind": "scripted_mock" if mock else "live_model",
        "building_data_source": "building_simulation",
        "python": platform.python_version(),
        "spade_llm": version("spade-llm"),
        "runs": [],
    }
    for index in range(args.repeat):
        entry, cluster = {"index": index + 1, "passed": False}, None
        try:
            with MockModelServer() if mock else nullcontext() as fixture:
                environment = fixture.environment if mock else None
                cluster = LocalCluster(
                    directory / f"run-{index + 1}", building=True, model_environment=environment
                )
                with cluster:
                    entry["checks"] = exercise(
                        cluster, mock=mock, full=args.role == "building-acceptance"
                    )
                require(
                    all(process.returncode == 0 for process in cluster.processes.values()),
                    "worker required forced cleanup or failed",
                )
                if fixture:
                    entry["mock_http_requests"] = fixture.requests
                    normal = [row for row in fixture.requests if row["case"] == "normal"]
                    expected_rooms = ROOMS if args.role == "building-acceptance" else ("K1315",)
                    for room in expected_rooms:
                        require(
                            [row["tool_results_seen"] for row in normal if row["room_id"] == room]
                            == [0, 1, 2],
                            "mock wire protocol or task deduplication failed",
                        )
                    entry["checks"].append(
                        {
                            "name": "mock_wire_protocol_and_task_deduplication",
                            "passed": True,
                            "normal_model_requests": len(normal),
                        }
                    )
                    print("PASS mock_wire_protocol_and_task_deduplication", flush=True)
                entry["passed"] = True
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            print(entry["error"], flush=True)
        finally:
            if cluster:
                entry["processes_started"] = cluster.starts
                entry["cleanup"] = {
                    key: process.poll() for key, process in cluster.processes.items()
                }
            report["runs"].append(entry)
            write_json(directory / "report.json", report)
        if not entry["passed"]:
            break
    report["passed"] = len(report["runs"]) == args.repeat and all(
        run["passed"] for run in report["runs"]
    )
    write_json(directory / "report.json", report)
    print(f"Report: {directory / 'report.json'}", flush=True)
    return 0 if report["passed"] else 1
