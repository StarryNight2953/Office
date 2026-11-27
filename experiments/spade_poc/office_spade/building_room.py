"""Room task adapter: canonical Building state plus optional SPADE-LLM decision loop."""

import asyncio
import copy
import json
import os

from .building import BuildingClient
from .model import ModelTaskRunner


class BuildingRoomState:
    def __init__(self, room_id, log):
        self.room_id, self.log = room_id, log
        self.client = BuildingClient(
            os.environ["OFFICE_BUILDING_URL"], os.environ["OFFICE_BUILDING_TOKEN"], room_id
        )
        self.cache, self.total_executions = {}, 0

    async def execute(self, request):
        if request.room_id != self.room_id or request.type != "task":
            raise ValueError("room scope violation")
        fingerprint = json.dumps(request.payload, sort_keys=True, allow_nan=False)
        if request.task_id in self.cache:
            previous, result = self.cache[request.task_id]
            if previous != fingerprint:
                raise ValueError("task_id reused with a different payload")
            return copy.deepcopy(result), True
        if len(self.cache) >= 1000:
            raise ValueError("prototype task cache full")
        operation = request.payload.get("operation")
        self.total_executions += 1
        try:
            initial = await asyncio.to_thread(self.client.snapshot)
            if operation in {"ping", "get_state"}:
                result = {
                    "status": "completed",
                    "answer": "pong" if operation == "ping" else "ok",
                    "state": initial,
                }
            elif operation == "building_tool":
                outcome = await asyncio.to_thread(
                    self.client.invoke,
                    request.payload.get("name"),
                    request.payload.get("arguments"),
                    request.task_id,
                )
                accepted = not (
                    isinstance(outcome, dict)
                    and (outcome.get("status") == "rejected" or "error" in outcome)
                )
                result = {
                    "status": "completed" if accepted else "failed",
                    "tool_result": outcome,
                    "state": await asyncio.to_thread(self.client.snapshot),
                }
            elif operation == "llm_task":
                result = await ModelTaskRunner(self.client, self.log).run(
                    request.task_id,
                    request.payload.get("task"),
                    request.payload.get("expectations"),
                    initial,
                )
            else:
                raise ValueError("unsupported Building operation")
        except (ValueError, TypeError, ConnectionError) as exc:
            result = {
                "status": "failed",
                "error_code": type(exc).__name__,
                "reason": str(exc),
                "state": {"room_id": self.room_id, "data_source": "building_simulation"},
            }
        result.update(
            execution_count=1, total_executions=self.total_executions, worker_pid=os.getpid()
        )
        self.cache[request.task_id] = (fingerprint, copy.deepcopy(result))
        return result, False
