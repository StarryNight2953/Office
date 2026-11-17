"""Single-owner adapter between the office UI and FAIRY's existing Apps.

The worker owns all mutable simulation state. Readers get detached, public
observations, never the internal physics checkpoint. Network calls run on that
same worker; pending UI commands are drained at Agent tool boundaries.
"""

from __future__ import annotations

import copy
import importlib.util
import math
import os
import queue
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fairy.apps.building_world.device_registry import DeviceRegistryApp
from fairy.apps.building_world.hvac_app import HvacApp
from fairy.apps.building_world.lighting_app import LightingApp
from fairy.apps.building_world.room_loader import load_room_configuration
from fairy.apps.building_world.runtime import BuildingWorldRuntime, constant_outdoor_provider
from fairy.apps.building_world.sensor_app import BuildingSensorApp
from fairy.apps.building_world.types import BuildingEvent, DeviceType
from fairy.apps.system import SystemApp
from fairy.physics.building.models import OutdoorConditions
from fairy.time_manager import TimeManager


ROOM_IDS = frozenset({"k1315", "k1316", "k1324"})
START_AT = datetime(2026, 9, 9, 1, tzinfo=timezone.utc)


class _SimulationClock(TimeManager):
    """Keep the existing clock API, but advance only with simulation actions."""

    def real_time_passed(self) -> float:
        return 0.0


class _Stopped(Exception):
    pass


@dataclass
class _Request:
    operation: str
    arguments: tuple[Any, ...]
    result: Future


def agent_configuration() -> dict[str, Any]:
    """Public readiness information; never return credentials or endpoint URLs."""

    model = os.environ.get("OFFICE_LLM_MODEL", "").strip()
    key = os.environ.get("OFFICE_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    sdk = importlib.util.find_spec("openai") is not None
    reason = None
    code = None
    if not model:
        code = "missing_model"
        reason = "请在服务端设置 OFFICE_LLM_MODEL。"
    elif not key or not key.strip():
        code = "missing_api_key"
        reason = "请在服务端设置 OFFICE_LLM_API_KEY 或 OPENAI_API_KEY。"
    elif not sdk:
        code = "missing_sdk"
        reason = "请安装 requirements-visualization.txt 中的 OpenAI SDK。"
    return {"configured": code is None, "model": model or None, "reason": reason, "reason_code": code}


def public_agent_error(exc: Exception) -> tuple[str, str]:
    """Classify failures without forwarding provider bodies or credentials."""
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if name == "AgentRunLimitExceeded":
        return "run_limit", "Agent 已达到本次工具调用或运行时限；请检查当前观测后继续。"
    if isinstance(exc, TimeoutError) or name == "APITimeoutError":
        return "timeout", "模型请求超时，请检查服务连接后重试。"
    if name in {"AuthenticationError", "PermissionDeniedError"} or status in {401, 403}:
        return "authentication", "模型认证失败，请检查服务端密钥和模型权限。"
    if name == "RateLimitError" or status == 429:
        return "rate_limit", "模型服务限流或额度不足，请检查额度并稍后重试。"
    if isinstance(exc, ConnectionError) or name == "APIConnectionError":
        return "connection", "无法连接模型服务，请检查服务端地址与网络。"
    if name in {"BadRequestError", "NotFoundError", "UnprocessableEntityError"} or status in {400, 404, 422}:
        return "model_request", "模型服务拒绝请求，请检查模型名称及工具调用支持。"
    if isinstance(exc, ImportError):
        return "dependency", "Agent 依赖不完整，请安装 requirements-visualization.txt。"
    return "execution", "Agent 执行失败，请检查服务端配置和当前实验状态。"


class Runner:
    """One isolated, single-room simulation; create a new instance to reset."""

    def __init__(self, scene: dict[str, Any]):
        if scene.get("room_id") not in ROOM_IDS:
            raise ValueError("unknown_room")
        self.run_id = uuid.uuid4().hex
        self._scene = copy.deepcopy(scene)
        self._requests: queue.Queue[_Request] = queue.Queue()
        self._submit_lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._closing = threading.Event()
        self._agent_stop = threading.Event()
        self._ready: Future = Future()
        self._cache: dict[str, Any] = {}
        self._revision = 0
        self._event_counter = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=200)
        self._status = "ready"
        self._agent_state: dict[str, Any] = {"status": "idle", "mode": None}
        self._worker = threading.Thread(target=self._serve, name=f"office-{self.run_id[:8]}", daemon=True)
        self._worker.start()
        self._ready.result()

    def snapshot(self) -> dict[str, Any]:
        with self._snapshot_lock:
            return copy.deepcopy(self._cache)

    def command(self, device_id: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return self._submit("command", device_id, copy.deepcopy(parameters))

    def advance(self, seconds: float) -> dict[str, Any]:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValueError("seconds must be an integer between 1 and 3600")
        if not math.isfinite(seconds) or not 1 <= seconds <= 3600 or seconds != int(seconds):
            raise ValueError("seconds must be an integer between 1 and 3600")
        return self._submit("advance", int(seconds))

    def start_agent(self, mode: str = "demo", task: str | None = None) -> dict[str, Any]:
        if mode not in {"demo", "llm"}:
            raise ValueError("unknown_agent_mode")
        if mode == "llm":
            config = agent_configuration()
            if not config["configured"]:
                raise ValueError(config["reason"])
        if task is not None and (not isinstance(task, str) or len(task) > 8000):
            raise ValueError("task must be text with at most 8000 characters")
        return self._submit("start", mode, task)

    def stop_agent(self) -> dict[str, Any]:
        # Request cancellation immediately, even while a provider is answering.
        # The worker acknowledges it at the next safe boundary.
        self._agent_stop.set()
        return self._submit("stop")

    def close(self) -> None:
        with self._submit_lock:
            if self._closing.is_set():
                return
            self._closing.set()
            self._agent_stop.set()
            self._fail_pending()
            self._requests.put(_Request("close", (), Future()))
        if threading.current_thread() is not self._worker:
            # A network request is bounded separately. A reset need not wait
            # for it: the old worker owns an entirely separate world.
            self._worker.join(timeout=1.0)

    def _submit(self, operation: str, *arguments: Any) -> Any:
        with self._submit_lock:
            if self._closing.is_set():
                raise RuntimeError("run_closed")
            request = _Request(operation, arguments, Future())
            self._requests.put(request)
        return request.result.result()

    def _serve(self) -> None:
        try:
            self._initialize()
            self._refresh()
            self._ready.set_result(None)
            while not self._closing.is_set():
                request = self._requests.get()
                self._dispatch(request)
        except Exception as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            else:
                self._status = "error"
                self._record("runtime_error", "Runtime stopped after an unexpected error.")
                self._refresh()
        finally:
            with self._submit_lock:
                self._closing.set()
                self._fail_pending()

    def _fail_pending(self) -> None:
        while True:
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                break
            if not request.result.done():
                request.result.set_exception(RuntimeError("run_closed"))

    def _initialize(self) -> None:
        configuration = load_room_configuration(
            Path(__file__).resolve().parents[1] / "configs" / "rooms" / f"{self._scene['room_id']}.yaml"
        )
        self._runtime = BuildingWorldRuntime.from_room_configuration(
            configuration,
            start_at=START_AT,
            outdoor_provider=constant_outdoor_provider(OutdoorConditions(
                air_temperature_c=30.0, relative_humidity_pct=65.0,
                co2_ppm=420.0, pm25_ug_m3=25.0, solar_irradiance_w_m2=300.0,
            )),
            observation_seed=0,
            max_timestep_seconds=60.0,
        )
        self._world = self._runtime.world
        self._clock = _SimulationClock()
        self._clock.reset(START_AT.timestamp())
        self._lighting = LightingApp(self._world)
        self._hvac = HvacApp(self._world)
        self._sensors = BuildingSensorApp(self._runtime.sensors)
        self._registry = DeviceRegistryApp(self._world)
        self._system = SystemApp()
        for app in (self._world, self._lighting, self._hvac, self._sensors, self._registry, self._system):
            app.register_time_manager(self._clock)
        self._system.register_time_advance_hook(self._advance_runtime)
        self._world.register_to_env("visualization", self._world_event)
        initial_states = self._scene.get("initial_states", {})
        if not isinstance(initial_states, dict):
            raise ValueError("initial_states must be an object")
        for device_id, parameters in initial_states.items():
            result = self._apply_command(device_id, parameters)
            if result["status"] != "accepted":
                raise ValueError(f"Invalid initial state for {device_id}: {result['reason']}")
        self._runtime.initialize_observations()
        self._record("run_created", "Experiment initialized from the saved scene.")

    def _dispatch(self, request: _Request) -> None:
        try:
            if request.operation == "close":
                request.result.set_result(None)
                return
            if self._closing.is_set():
                raise RuntimeError("run_closed")
            if request.operation == "start":
                if self._agent_state["status"] == "running":
                    raise ValueError("agent_already_running")
                mode, task = request.arguments
                self._agent_stop.clear()
                self._status = "running"
                self._agent_state = {"status": "running", "mode": mode}
                self._record("agent_started", "Built-in deterministic workflow started." if mode == "demo" else "FAIRY Agent started.", source="agent")
                self._refresh()
                request.result.set_result(self.snapshot())
                self._execute_agent(mode, task)
                return
            if request.operation == "command":
                result = self._apply_command(*request.arguments)
            elif request.operation == "advance":
                self._advance(*request.arguments)
                result = None
            elif request.operation == "stop":
                self._agent_stop.set()
                self._record("stop_requested", "Agent will stop at the next tool boundary.", source="user")
                result = None
            else:
                raise ValueError("unknown_operation")
            self._refresh()
            request.result.set_result(self.snapshot() if result is None else result)
        except Exception as exc:
            if not request.result.done():
                request.result.set_exception(exc)

    def _apply_command(self, device_id: str, parameters: dict[str, Any]) -> dict[str, Any]:
        spec = self._world.devices.get(device_id)
        state = self._world.device_states.get(device_id)
        if spec is None or state is None:
            return self._rejected(device_id, "unknown_device")
        if not isinstance(parameters, dict):
            return self._rejected(device_id, "parameters_must_be_object")
        if spec.device_type == DeviceType.LIGHTING:
            defaults = {
                "power_on": state.power_on,
                "brightness_pct": state.settings.get("brightness_pct") or 80,
                "color_temperature_k": state.settings.get("color_temperature_k") or 4000,
                "scene": state.settings.get("scene") if state.power_on else "office",
            }
            integer_fields = {"brightness_pct", "color_temperature_k"}
            string_fields = {"scene"}
            callback = self._lighting.set_lighting
        elif spec.device_type == DeviceType.HVAC:
            defaults = {
                "power_on": state.power_on, "mode": state.mode if state.power_on else "cooling",
                "target_temperature_c": state.target_temperature_c or 24.0,
                "fan_level": state.level or 2,
            }
            integer_fields = {"fan_level"}
            string_fields = {"mode"}
            callback = self._hvac.set_hvac
        else:
            return self._rejected(device_id, "device_read_only_in_visualization")
        if set(parameters) - set(defaults):
            return self._rejected(device_id, "unknown_parameter")
        for key, value in parameters.items():
            if key == "power_on" and not isinstance(value, bool):
                return self._rejected(device_id, "power_on_must_be_boolean")
            if key in integer_fields and (not isinstance(value, int) or isinstance(value, bool)):
                return self._rejected(device_id, f"{key}_must_be_integer")
            if key in string_fields and (not isinstance(value, str) or len(value) > 100):
                return self._rejected(device_id, f"{key}_must_be_short_text")
            if key == "target_temperature_c" and (isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)):
                return self._rejected(device_id, "target_temperature_must_be_finite")
        defaults.update(parameters)
        result = callback(device_id=device_id, **defaults)
        if result.get("status") == "rejected":
            self._record("command_rejected", f"{device_id}: {result['reason']}", source="user", device_id=device_id)
        return result

    def _rejected(self, device_id: str, reason: str) -> dict[str, Any]:
        self._record("command_rejected", f"{device_id}: {reason}", source="user", device_id=device_id)
        return {"status": "rejected", "device_id": device_id, "reason": reason}

    def _advance(self, seconds: int) -> None:
        self._system.advance_time(seconds=seconds, stop_on_event=False)
        self._record("time_advanced", f"Advanced simulation by {seconds} seconds.")

    def _advance_runtime(self, _previous: float, current: float, stop_on_event: bool) -> dict[str, Any]:
        result = self._runtime.advance_to(datetime.fromtimestamp(current, tz=timezone.utc), stop_on_wakeup=stop_on_event)
        wake_ids = {decision.event_id for decision in result.trigger_decisions if decision.should_wake_agent}
        response: dict[str, Any] = {"actual_timestamp": result.ended_at.timestamp()}
        if wake_ids:
            response["interruption"] = {
                "reason": "building_event_requires_reconciliation",
                "events": [{"event_id": event.event_id, "event_type": event.event_type.value, "subject_id": event.subject_id} for event in result.processed_events if event.event_id in wake_ids],
            }
        return response

    def _world_event(self, event: BuildingEvent) -> None:
        self._record(event.event_type.value, f"{event.subject_id}: {event.event_type.value}",
                     source=event.source, device_id=event.subject_id if event.subject_id in self._world.devices else None,
                     at_time=event.occurred_at.isoformat())

    def _record(self, kind: str, message: str, *, source: str = "runtime", device_id: str | None = None,
                at_time: str | None = None, details: dict[str, Any] | None = None) -> None:
        self._event_counter += 1
        row = {"id": f"ui-{self._event_counter}", "kind": kind,
               "time": at_time or self._runtime.current_time.isoformat(), "message": message, "source": source}
        if device_id is not None:
            row["device_id"] = device_id
        if details is not None:
            row["details"] = details
        self._events.append(row)

    def _refresh(self) -> None:
        self._revision += 1
        devices = {}
        for device_id, spec in self._world.devices.items():
            state = self._world.device_states[device_id]
            devices[device_id] = {
                "device_id": device_id, "device_type": spec.device_type.value,
                "room_id": spec.room_id, "zone_id": spec.zone_id,
                "power_on": state.power_on, "mode": state.mode, "level": state.level,
                "target_temperature_c": state.target_temperature_c,
                "health": state.health.value, "settings": copy.deepcopy(state.settings),
                "rated_power_w": spec.rated_power_w,
            }
        sensors = [{"sensor_id": item.sensor_id, "zone_id": item.zone_id,
                    "quantity": item.quantity.value, "value": item.value,
                    "unit": item.unit, "quality": item.quality.value,
                    "observed_at": item.observed_at.isoformat()}
                   for item in self._runtime.sensors.read_all(self._runtime.current_time)]
        snapshot = {
            "run_id": self.run_id, "scene_id": self._scene["id"], "room_id": self._scene["room_id"],
            "status": self._status, "sim_time": self._runtime.current_time.isoformat(),
            "revision": self._revision, "devices": devices, "sensors": sensors,
            "events": list(self._events), "agent": copy.deepcopy(self._agent_state),
            "metrics": {"energy_kwh": sum(self._runtime.physics.cumulative_energy_kwh_by_zone.values())},
        }
        with self._snapshot_lock:
            self._cache = snapshot

    def _boundary(self, wait: float = 0.0) -> None:
        """Apply queued UI operations on the owner thread between tool calls."""
        deadline = time.monotonic() + wait
        count = 0
        while count < 64:
            if self._closing.is_set():
                raise _Stopped()
            try:
                if wait and time.monotonic() < deadline:
                    request = self._requests.get(timeout=max(0.0, deadline - time.monotonic()))
                else:
                    request = self._requests.get_nowait()
            except queue.Empty:
                break
            self._dispatch(request)
            count += 1
        if self._agent_stop.is_set() or self._closing.is_set():
            raise _Stopped()

    def _execute_agent(self, mode: str, task: str | None) -> None:
        try:
            if mode == "demo":
                self._run_demo()
            else:
                self._run_llm(task)
            self._agent_state["status"] = "completed"
            self._status = "ready"
            self._record("agent_completed", "Workflow completed.", source="agent")
        except _Stopped:
            self._agent_state["status"] = "stopped"
            self._status = "stopped"
            self._record("agent_stopped", "Agent stopped at a safe boundary.", source="agent")
        except Exception as exc:
            self._agent_state["status"] = "error"
            # Provider exceptions may include HTTP bodies. Keep credentials,
            # URLs and raw conversation content out of the browser event feed.
            code, message = public_agent_error(exc)
            self._agent_state["error_code"] = code
            self._agent_state["error"] = message
            self._status = "error"
            self._record("agent_error", message, source="agent", details={"error_code": code})
        finally:
            self._refresh()

    def _run_demo(self) -> None:
        self._boundary(wait=0.25)
        for device_id, spec in self._world.devices.items():
            if spec.device_type == DeviceType.LIGHTING:
                self._apply_command(device_id, {"power_on": True, "brightness_pct": 70, "color_temperature_k": 4000, "scene": "meeting"})
            elif spec.device_type == DeviceType.HVAC:
                self._apply_command(device_id, {"power_on": True, "mode": "cooling", "target_temperature_c": 24.0, "fan_level": 3})
            else:
                continue
            self._refresh()
            self._boundary(wait=0.3)
        for _ in range(12):
            self._boundary(wait=0.25)
            self._advance(120)
            readings = self._sensors.read_all_sensors()["readings"]
            temperatures = [round(row["value"], 2) for row in readings if row["quantity"] == "air_temperature_c" and row["value"] is not None]
            self._record("demo_observation", f"Observed room temperature: {temperatures} °C", source="demo")
            self._refresh()

    def _run_llm(self, task: str | None) -> None:
        # Optional dependency: manual control and the demo never import it.
        from fairy.agents.agent.agent import Agent
        from fairy.agents.llm.openai_compatible_client import OpenAICompatibleClient

        owner = self

        class InteractiveAgent(Agent):
            def before_llm_turn(self, task: str) -> str:
                owner._boundary()
                return ""

            def after_llm_message(self, message: Any) -> None:
                owner._boundary()

            def call_function(self, tool_call: Any) -> Any:
                owner._boundary()
                response = super().call_function(tool_call)
                owner._refresh()
                owner._boundary()
                return response

            def log(self, message: Any) -> None:
                # UI receives the allowlisted runtime feed below.
                return None

        llm = OpenAICompatibleClient(
            model=os.environ["OFFICE_LLM_MODEL"],
            api_key=os.environ.get("OFFICE_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OFFICE_LLM_BASE_URL"),
            timeout=20.0, parallel_tool_calls=False,
        )
        llm.api_client = llm.api_client.with_options(max_retries=0)
        agent = InteractiveAgent(
            "OfficeAgent", llm,
            system_message=(
                "You control a simulated office, using only the provided FAIRY tools. "
                "Discover device IDs, control lighting and HVAC, and verify environmental effects using sensors. "
                "A successful device command does not mean room temperature reached its target. "
                "Advance time in steps of at most 300 seconds and check observations. "
                "Report results concisely in Chinese; do not invent readings."
            ),
            toolsets=[self._registry, self._lighting, self._hvac, self._sensors, self._system],
            hidden_tool_names={"SystemApp__wait_for_notification"},
        )
        agent.set_time_manager(self._clock)
        agent.max_llm_call_retries = 0
        # Retain FAIRY's schemas and event machinery while enforcing the same
        # input boundary for model-generated and manually entered commands.
        agent.tools_map["LightingApp__set_lighting"] = lambda device_id, **parameters: self._apply_command(device_id, parameters)
        agent.tools_map["HvacApp__set_hvac"] = lambda device_id, **parameters: self._apply_command(device_id, parameters)
        agent.tools_map["SystemApp__advance_time"] = self._agent_advance
        agent.runtime_event_sink = self._agent_event
        try:
            summary = agent.run(input=task or "Prepare this room for a meeting: turn lights to 70% at 4000K, set cooling to 24°C with fan level 3, advance at least 10 minutes and verify sensor observations.", max_tool_calls=30, timeout_seconds=180)
            self._boundary()
            if isinstance(summary, str) and summary.strip():
                # This is the model's final answer, distinct from the tool/event
                # feed. Redact configured credentials even if a provider echoes
                # them unexpectedly; retain no raw provider exception content.
                for variable in ("OFFICE_LLM_API_KEY", "OPENAI_API_KEY", "OFFICE_LLM_BASE_URL"):
                    private = os.environ.get(variable)
                    if private:
                        summary = summary.replace(private, "[redacted]")
                self._agent_state["summary"] = summary.strip()[:8000]
        finally:
            llm.api_client.close()

    def _agent_advance(self, seconds: int = 0, minutes: int = 0, hours: int = 0,
                       days: int = 0, stop_on_event: bool = True) -> dict[str, Any]:
        units = (seconds, minutes, hours, days)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in units):
            return {"status": "rejected", "reason": "time_units_must_be_nonnegative_integers"}
        total = seconds + minutes * 60 + hours * 3600 + days * 86400
        if not 1 <= total <= 3600 or not isinstance(stop_on_event, bool):
            return {"status": "rejected", "reason": "advance_must_be_between_1_and_3600_seconds"}
        return self._system.advance_time(seconds=total, stop_on_event=stop_on_event)

    def _agent_event(self, event: dict[str, Any]) -> None:
        kind = str(event.get("event_type", "agent_event"))
        # Explicit allowlist avoids exporting prompts, API errors, model
        # responses, tool payloads or any simulator internals by accident.
        details = {key: event[key] for key in ("tool_name", "tool_call_count", "llm_turn_count", "status") if key in event}
        message = kind.replace("_", " ")
        if event.get("tool_name"):
            message += f": {event['tool_name']}"
        self._record(kind, message, source="agent", details=details)
        self._refresh()
