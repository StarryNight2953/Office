"""SPADE behaviours: scoped room execution and asynchronous coordination."""

import asyncio
import copy
import inspect
import math
from dataclasses import dataclass, field
from uuid import uuid4

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour
from spade.message import Message
from spade.template import Template

from .core import ONTOLOGY, ROOMS, Envelope, EventLog, RoomState, jid


def wire_message(recipient: str, envelope: Envelope) -> Message:
    message = Message(to=recipient, body=envelope.encode(), thread=envelope.task_id)
    message.set_metadata("ontology", ONTOLOGY)
    return message


def protocol_template():
    template = Template()
    template.set_metadata("ontology", ONTOLOGY)
    return template


class UnexpectedMessages(CyclicBehaviour):
    async def run(self):
        message = await self.receive(timeout=0.5)
        if message is not None:
            self.agent.log.write(
                "unmatched_message",
                sender=str(message.sender),
                ontology=message.get_metadata("ontology"),
                body=(message.body or "")[:1000],
            )


class RoomAgent(Agent):
    def __init__(self, room_id, password, port, log: EventLog, state=None):
        super().__init__(jid(room_id), password, port=port)
        self.state = state or RoomState(room_id)
        self.log = log

    async def setup(self):
        self.add_behaviour(self.ReceiveTasks(), protocol_template())
        self.add_behaviour(UnexpectedMessages(), ~protocol_template())

    class ReceiveTasks(CyclicBehaviour):
        async def run(self):
            message = await self.receive(timeout=0.5)
            if message is None:
                return
            agent = self.agent
            if str(message.sender).split("/")[0] != jid("coordinator"):
                agent.log.write("sender_rejected")
                return
            try:
                request = Envelope.decode(message.body)
                if message.thread != request.task_id or request.type != "task":
                    raise ValueError("invalid task thread/type")
            except ValueError as exc:
                agent.log.write("message_rejected", reason=str(exc))
                return
            agent.log.write("message_received", request)
            try:
                if request.room_id != agent.state.room_id:
                    raise ValueError("room scope violation")
                delay = request.payload.get("test_delay_seconds", 0)
                if (
                    isinstance(delay, bool)
                    or not isinstance(delay, (int, float))
                    or not math.isfinite(delay)
                    or not 0 <= delay <= 10
                ):
                    raise ValueError("test_delay_seconds must be between 0 and 10")
                ack = Envelope(request.task_id, request.room_id, "ack", {"status": "accepted"})
                await self.send(wire_message(jid("coordinator"), ack))
                agent.log.write("message_sent", ack)
                if delay and request.task_id not in agent.state.cache:
                    await asyncio.sleep(delay)
                execution = agent.state.execute(request)
                result, duplicate = await execution if inspect.isawaitable(execution) else execution
                agent.log.write(
                    "duplicate_replayed" if duplicate else "task_executed",
                    request,
                    execution_count=result["execution_count"],
                    total_executions=agent.state.total_executions,
                )
                reply = Envelope(
                    request.task_id,
                    request.room_id,
                    "result" if result["status"] == "completed" else "error",
                    result,
                )
            except ValueError as exc:
                reply = Envelope(
                    request.task_id,
                    request.room_id,
                    "error",
                    {"status": "failed", "reason": str(exc)},
                )
                agent.log.write("task_rejected", request, reason=str(exc))
            await self.send(wire_message(jid("coordinator"), reply))
            agent.log.write("message_sent", reply)


@dataclass
class PendingTask:
    room_id: str
    payload: dict
    future: asyncio.Future
    status: str = "pending"
    result: dict = field(default_factory=dict)
    late_messages: int = 0


class CoordinatorAgent(Agent):
    def __init__(self, password, port, log: EventLog):
        super().__init__(jid("coordinator"), password, port=port)
        self.log = log
        self.tasks: dict[str, PendingTask] = {}

    async def setup(self):
        self.inbox = self.ReceiveResults()
        self.add_behaviour(self.inbox, protocol_template())
        self.add_behaviour(UnexpectedMessages(), ~protocol_template())

    async def request(self, room_id, payload, *, timeout=5.0, repeats=1, task_id=None):
        if room_id not in ROOMS or not isinstance(payload, dict):
            raise ValueError("unknown room or invalid payload")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0.05 <= timeout <= 180
        ):
            raise ValueError("timeout must be between 0.05 and 180 seconds")
        if type(repeats) is not int or not 1 <= repeats <= 5:
            raise ValueError("repeats must be between 1 and 5")
        task_id = task_id or uuid4().hex
        # Validate before adding to ledger or sending anything.
        Envelope(task_id, room_id, "task", payload)
        if task_id in self.tasks:
            raise ValueError("task_id already submitted; use repeats to resend the same task")
        if len(self.tasks) >= 1000:
            raise ValueError("prototype task ledger is full; start a new session")
        task = PendingTask(
            room_id, copy.deepcopy(payload), asyncio.get_running_loop().create_future()
        )
        self.tasks[task_id] = task
        started = asyncio.get_running_loop().time()
        try:
            async with asyncio.timeout(timeout):
                for _ in range(repeats):
                    envelope = Envelope(task_id, room_id, "task", payload)
                    await self.inbox.send(wire_message(jid(room_id), envelope))
                    self.log.write("message_sent", envelope, status=task.status)
                await asyncio.shield(task.future)
        except TimeoutError:
            if not task.future.done():
                task.status = "timed_out"
                task.result = {"status": "timed_out", "reason": "room response deadline exceeded"}
                task.future.set_result(task.result)
                self.log.write(
                    "task_timed_out", task_id=task_id, room_id=room_id, status=task.status
                )
        return {
            "task_id": task_id,
            "room_id": room_id,
            **task.result,
            "elapsed_ms": round((asyncio.get_running_loop().time() - started) * 1000, 2),
        }

    def ledger(self):
        return {
            key: {
                "room_id": task.room_id,
                "status": task.status,
                "late_messages": task.late_messages,
            }
            for key, task in self.tasks.items()
        }

    class ReceiveResults(CyclicBehaviour):
        async def run(self):
            message = await self.receive(timeout=0.5)
            if message is None:
                return
            agent = self.agent
            try:
                envelope = Envelope.decode(message.body)
                task = agent.tasks.get(envelope.task_id)
                if (
                    task is None
                    or task.room_id != envelope.room_id
                    or str(message.sender).split("/")[0] != jid(task.room_id)
                    or message.thread != envelope.task_id
                    or envelope.type not in {"ack", "result", "error"}
                ):
                    raise ValueError("unexpected sender, task, room or message type")
                if envelope.type == "result" and (
                    envelope.payload.get("status") != "completed"
                    or envelope.payload.get("state", {}).get("room_id") != task.room_id
                ):
                    raise ValueError("invalid result state")
            except (ValueError, AttributeError) as exc:
                agent.log.write("message_rejected", reason=str(exc))
                return
            agent.log.write("message_received", envelope, status=task.status)
            if task.status in {"completed", "failed", "timed_out"}:
                if task.status == "timed_out":
                    task.late_messages += 1
                agent.log.write("terminal_message_ignored", envelope, status=task.status)
                return
            if envelope.type == "ack":
                task.status = "acknowledged"
                return
            task.status = "completed" if envelope.type == "result" else "failed"
            task.result = {**envelope.payload, "status": task.status}
            task.future.set_result(task.result)
            agent.log.write("task_finished", envelope, status=task.status)
