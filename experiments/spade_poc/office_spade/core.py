"""Versioned messages and process-local synthetic room state."""

import copy
import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOMS = ("K1315", "K1316")
DOMAIN = "127.0.0.1"
ONTOLOGY = "office-spade/1"


def jid(role: str) -> str:
    return f"{role.lower()}@{DOMAIN}"


@dataclass(frozen=True)
class Envelope:
    task_id: str
    room_id: str
    type: str
    payload: dict
    message_id: str = field(default_factory=lambda: uuid4().hex)
    schema_version: int = 1

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported schema_version")
        if self.room_id not in ROOMS or self.type not in {"task", "ack", "result", "error"}:
            raise ValueError("invalid room_id or message type")
        if not all(
            isinstance(v, str) and 0 < len(v) <= 128 for v in (self.task_id, self.message_id)
        ):
            raise ValueError("invalid task/message ID")
        if not isinstance(self.payload, dict):
            raise ValueError("payload must be an object")
        json.dumps(self.payload, allow_nan=False)

    def encode(self) -> str:
        return json.dumps(asdict(self), allow_nan=False)

    @classmethod
    def decode(cls, body: str):
        if not isinstance(body, str) or len(body) > 32768:
            raise ValueError("invalid message body")
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("message must be an object")
            return cls(**data)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid envelope") from exc


class EventLog:
    def __init__(self, directory: Path, role: str):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{role.lower()}.jsonl"
        self.role = role

    def write(self, event: str, envelope: Envelope | None = None, **details):
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": self.role,
            "pid": os.getpid(),
            "event": event,
            **details,
        }
        if envelope is not None:
            row.update(
                task_id=envelope.task_id,
                message_id=envelope.message_id,
                room_id=envelope.room_id,
                message_type=envelope.type,
            )
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, allow_nan=False) + "\n")


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


class RoomState:
    """Fixture adapter, serially accessed by one room behaviour. No Building writes."""

    def __init__(self, room_id: str):
        if room_id not in ROOMS:
            raise ValueError("unknown room")
        self.room_id = room_id
        self.temperature_c = 27.0 if room_id == "K1315" else 24.0
        self.cache: dict[str, tuple[str, dict]] = {}
        self.total_executions = 0

    def execute(self, request: Envelope) -> tuple[dict, bool]:
        if request.room_id != self.room_id or request.type != "task":
            raise ValueError("room scope violation")
        fingerprint = json.dumps(request.payload, sort_keys=True, allow_nan=False)
        if request.task_id in self.cache:
            previous, result = self.cache[request.task_id]
            if previous != fingerprint:
                raise ValueError("task_id reused with a different payload")
            return copy.deepcopy(result), True
        if len(self.cache) >= 1000:
            raise ValueError("prototype task cache is full; start a new session")
        operation = request.payload.get("operation")
        if operation not in {"ping", "get_state", "set_test_temperature"}:
            raise ValueError("unsupported operation")
        if operation == "set_test_temperature":
            temperature = request.payload.get("temperature_c")
            if (
                isinstance(temperature, bool)
                or not isinstance(temperature, (float, int))
                or not math.isfinite(temperature)
                or not 10 <= temperature <= 40
            ):
                raise ValueError("temperature_c must be a finite number between 10 and 40")
            self.temperature_c = float(temperature)
        self.total_executions += 1
        result = {
            "status": "completed",
            "answer": "pong" if operation == "ping" else "ok",
            "state": {
                "room_id": self.room_id,
                "temperature_c": self.temperature_c,
                "data_source": "synthetic_fixture",
            },
            "execution_count": 1,
            "total_executions": self.total_executions,
            "worker_pid": os.getpid(),
        }
        self.cache[request.task_id] = (fingerprint, copy.deepcopy(result))
        return result, False
