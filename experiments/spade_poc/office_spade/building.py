"""Single canonical Building runtime, accessed through room-scoped capabilities."""

import copy
import json
import os
import secrets
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4

from .core import ROOMS, EventLog, write_json

ROOT = Path(__file__).resolve().parents[3]
START_AT = datetime(2026, 9, 9, 1, tzinfo=timezone.utc)
ALLOWED = frozenset(
    {
        "DeviceRegistryApp__list_devices",
        "DeviceRegistryApp__get_device_state",
        "HvacApp__set_hvac",
        "LightingApp__set_lighting",
        "BuildingSensorApp__read_zone_sensors",
    }
)


class BuildingBoundary:
    def __init__(self, log_directory: Path):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from smartoffice.apps.building_world import (
            BuildingSensorApp,
            DeviceRegistryApp,
            HvacApp,
            LightingApp,
        )
        from smartoffice.apps.building_world.room_loader import load_room_configuration
        from smartoffice.apps.building_world.runtime import (
            BuildingWorldRuntime,
            constant_outdoor_provider,
        )
        from smartoffice.controllers.building.room_agents import RoomContextApp, RoomScopedToolset
        from smartoffice.physics.building.models import OutdoorConditions
        from smartoffice.time_manager import TimeManager

        self.lock = threading.RLock()
        self.log = EventLog(log_directory, "building")
        self.runtime = BuildingWorldRuntime.from_room_configurations(
            [
                load_room_configuration(ROOT / "smartoffice/configs/rooms" / f"{room}.yaml")
                for room in ("k1324", "k1316", "k1315")
            ],
            start_at=START_AT,
            observation_seed=0,
            max_timestep_seconds=60,
            outdoor_provider=constant_outdoor_provider(
                OutdoorConditions(
                    air_temperature_c=30,
                    relative_humidity_pct=65,
                    co2_ppm=420,
                    pm25_ug_m3=25,
                    solar_irradiance_w_m2=300,
                )
            ),
        )
        self.world = self.runtime.world
        self.clock = TimeManager(include_wall_time=False)
        self.clock.reset(START_AT.timestamp())
        self.world.register_time_manager(self.clock)
        self.apps = [
            DeviceRegistryApp(self.world),
            HvacApp(self.world),
            LightingApp(self.world),
            BuildingSensorApp(self.runtime.sensors),
        ]
        for app in self.apps:
            app.register_time_manager(self.clock)
        self.contexts, self.tools = {}, {}
        for room in ROOMS:
            context = RoomContextApp(self.world, room)
            context.register_time_manager(self.clock)
            self.contexts[room] = context
            tools = RoomScopedToolset(
                world=self.world,
                room_id=room,
                apps=self.apps,
                allowed_tool_names=ALLOWED,
                execution_lock=self.lock,
            ).get_tools()
            self.tools[room] = {tool.to_open_ai()["function"]["name"]: tool for tool in tools}
        self.runtime.initialize_observations()
        self.calls = {}

    def snapshot(self, room):
        if room not in ROOMS:
            raise ValueError("unknown room")
        with self.lock:
            context = self.contexts[room].get_room_snapshot()
            observations = [
                self.tools[room]["BuildingSensorApp__read_zone_sensors"](zone_id=zone["zone_id"])
                for zone in context["zones"]
            ]
            return {
                "room_id": room,
                "data_source": "building_simulation",
                "simulation_time": self.runtime.current_time.isoformat(),
                "context": context,
                "observations": observations,
            }

    def catalog(self, room):
        if room not in ROOMS:
            raise ValueError("unknown room")
        return [tool.to_open_ai()["function"] for tool in self.tools[room].values()]

    def invoke(self, room, name, arguments, call_id):
        if room not in ROOMS or not isinstance(arguments, dict):
            raise ValueError("invalid room or arguments")
        if not isinstance(call_id, str) or not 0 < len(call_id) <= 128:
            raise ValueError("invalid call_id")
        fingerprint = json.dumps([name, arguments], sort_keys=True, allow_nan=False)
        key = (room, call_id)
        with self.lock:
            if key in self.calls:
                previous, result = self.calls[key]
                if previous != fingerprint:
                    raise ValueError("call_id reused with different arguments")
                return copy.deepcopy(result)
            if len(self.calls) >= 5000:
                raise ValueError("prototype call ledger full")
            tool = self.tools[room].get(name)
            if tool is None:
                raise PermissionError("tool is not allowed for this room")
            self.log.write(
                "tool_requested", room_id=room, tool=name, call_id=call_id, arguments=arguments
            )
            try:
                result = tool(**arguments)
            except (ValueError, TypeError, PermissionError) as exc:
                self.log.write(
                    "tool_rejected",
                    room_id=room,
                    tool=name,
                    call_id=call_id,
                    reason=type(exc).__name__,
                )
                raise
            self.calls[key] = (fingerprint, copy.deepcopy(result))
            self.log.write("tool_finished", room_id=room, tool=name, call_id=call_id, result=result)
            return result

    def advance(self, seconds):
        if type(seconds) is not int or not 1 <= seconds <= 1800:
            raise ValueError("seconds must be an integer between 1 and 1800")
        with self.lock:
            result = self.runtime.advance_to(self.runtime.current_time + timedelta(seconds=seconds))
            self.clock.set_offset((result.ended_at - START_AT).total_seconds())
            self.log.write(
                "simulation_advanced", seconds=seconds, simulation_time=result.ended_at.isoformat()
            )
            return {"simulation_time": result.ended_at.isoformat()}


class BuildingClient:
    def __init__(self, url, token, room_id=None):
        self.url, self.token, self.room_id = url.rstrip("/"), token, room_id
        self.opener = build_opener(ProxyHandler({}))

    def request(self, action, **payload):
        body = json.dumps(
            {"action": action, "room_id": self.room_id, **payload}, allow_nan=False
        ).encode()
        request = Request(
            self.url + "/rpc",
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise ValueError(f"Building request rejected ({exc.code})") from None
        except (URLError, TimeoutError):
            raise ConnectionError("Building environment service unavailable") from None
        if not result.get("ok"):
            raise ValueError(result.get("error", "Building request failed"))
        return result["data"]

    def snapshot(self):
        return self.request("snapshot")

    def catalog(self):
        return self.request("catalog")

    def invoke(self, name, arguments, call_id=None):
        return self.request("tool", name=name, arguments=arguments, call_id=call_id or uuid4().hex)


def serve_building(args):
    tokens = json.loads(os.environ["OFFICE_BUILDING_TOKENS"])
    admin = os.environ["OFFICE_BUILDING_ADMIN_TOKEN"]
    boundary = BuildingBoundary(args.run_dir)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            try:
                if self.path != "/rpc":
                    raise ValueError("unknown endpoint")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32768:
                    raise ValueError("invalid request length")
                credential = self.headers.get("Authorization", "").removeprefix("Bearer ")
                assigned = next(
                    (
                        room
                        for room, token in tokens.items()
                        if secrets.compare_digest(credential, token)
                    ),
                    None,
                )
                is_admin = secrets.compare_digest(credential, admin)
                if assigned is None and not is_admin:
                    self.send_error(403)
                    return
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("request must be an object")
                room = payload.get("room_id")
                if not is_admin and room != assigned:
                    raise PermissionError("room scope violation")
                action = payload.get("action")
                if action == "advance" and is_admin:
                    result = boundary.advance(payload.get("seconds"))
                elif action == "snapshot":
                    result = boundary.snapshot(room)
                elif action == "catalog":
                    result = boundary.catalog(room)
                elif action == "tool":
                    result = boundary.invoke(
                        room, payload.get("name"), payload.get("arguments"), payload.get("call_id")
                    )
                else:
                    raise PermissionError("operation is not allowed")
                response = {"ok": True, "data": result}
            except (ValueError, TypeError, PermissionError, KeyError) as exc:
                response = {"ok": False, "error": str(exc)}
            encoded = json.dumps(response, allow_nan=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    args.run_dir.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.timeout = 0.2
    write_json(
        args.run_dir / "building.ready.json",
        {
            "pid": os.getpid(),
            "url": f"http://127.0.0.1:{server.server_port}",
            "world_rooms": list(boundary.world.rooms),
            "data_source": "building_simulation",
        },
    )
    try:
        while not (args.run_dir / "building.stop").exists():
            server.handle_request()
    finally:
        server.server_close()
