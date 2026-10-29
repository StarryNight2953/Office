"""Existing-room catalog and atomic, separate persistence for visual layouts."""

from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path

from fairy.apps.building_world.room_loader import load_room_configuration
from fairy.visualization.models import HvacParameters, LightingParameters, Scene

ROOM_DIR = Path(__file__).resolve().parents[1] / "configs" / "rooms"
ROOMS = {
    "k1315": ("K1315 · 会议室", 10.0, 5.0),
    "k1316": ("K1316 · 研讨室", 6.0, 5.0),
    "k1324": ("K1324 · 研究生办公室", 10.0, 7.0),
}
NAMES = {
    "desk": "办公桌", "chair": "座椅", "table": "会议桌", "plant": "绿植",
    "cabinet": "储物柜", "lighting": "灯具", "hvac": "空调",
    "sensor": "环境传感器", "projector": "投影仪", "audio": "音响",
    "printer": "打印机", "humidifier": "加湿器", "air_purifier": "净化器",
    "ventilation": "新风", "access_control": "门禁",
}


def room_configuration(room_id: str):
    if room_id not in ROOMS:
        raise KeyError(room_id)
    return load_room_configuration(ROOM_DIR / f"{room_id}.yaml")


def catalog() -> dict:
    from fairy.visualization.runner import agent_configuration

    readiness = agent_configuration()
    rooms = []
    for room_id, (name, width, depth) in ROOMS.items():
        config = room_configuration(room_id)
        rooms.append({
            "id": room_id, "name": name, "capacity": config.room.capacity,
            "area_m2": sum(z.area_m2 for z in config.zones),
            "dimensions": {"width": width, "depth": depth, "height": config.zones[0].height_m},
            "devices": [{
                "device_id": d.device_id, "device_type": d.device_type.value,
                "room_id": d.room_id, "zone_id": d.zone_id,
                "rated_power_w": d.rated_power_w,
                "capabilities": sorted(d.capabilities),
                "functional_area_id": d.functional_area_id,
            } for d in config.devices],
        })
    return {
        "rooms": rooms,
        "assets": [{"id": kind, "name": name, "kind": kind} for kind, name in NAMES.items()],
        "agent_configured": readiness["configured"],
        "agent_readiness": readiness,
    }


def validate_scene(scene: Scene) -> Scene:
    """Validate semantic bindings without starting physics or a model client."""
    config = room_configuration(scene.room_id)
    devices = {d.device_id: d for d in config.devices}
    for obj in scene.objects:
        if obj.device_id is not None:
            device = devices.get(obj.device_id)
            if device is None:
                raise ValueError(f"unknown device binding: {obj.device_id}")
            if obj.kind != device.device_type.value:
                raise ValueError(f"object {obj.id} has the wrong kind for {obj.device_id}")
    normalized = {}
    for device_id, parameters in scene.initial_states.items():
        device = devices.get(device_id)
        if device is None:
            raise ValueError(f"unknown initial-state device: {device_id}")
        if device.device_type.value == "lighting":
            normalized[device_id] = LightingParameters.model_validate(parameters).model_dump()
        elif device.device_type.value == "hvac":
            normalized[device_id] = HvacParameters.model_validate(parameters).model_dump()
        else:
            raise ValueError(f"initial-state editing is not supported for {device_id}")
    return scene.model_copy(update={"initial_states": normalized}, deep=True)


def default_scene(room_id: str) -> Scene:
    config = room_configuration(room_id)
    name, width, depth = ROOMS[room_id]
    objects = []

    def add(kind, position, *, device_id=None, label=None, rotation=(0, 0, 0), scale=(1, 1, 1)):
        objects.append({
            "id": f"{room_id}-{kind}-{len(objects) + 1:03d}",
            "name": label or NAMES[kind], "kind": kind,
            "position": position, "rotation": rotation, "scale": scale,
            "device_id": device_id, "asset_url": None,
        })

    if room_id == "k1324":
        for row in range(3):
            for col in range(4):
                x, z = -3.15 + col * 2.1, -1.8 + row * 1.8
                add("desk", (x, 0, z), label=f"研究工位 {row * 4 + col + 1:02d}")
                add("chair", (x, 0, z + 0.7))
    else:
        add("table", (0, 0, 0), scale=(2 if room_id == "k1315" else 1, 1, 1))
        count = 9 if room_id == "k1315" else 3
        length = 5.6 if room_id == "k1315" else 2.4
        for index in range(count):
            x = -length / 2 + length * index / max(count - 1, 1)
            add("chair", (x, 0, -1.15), rotation=(0, math.pi, 0))
            add("chair", (x, 0, 1.15))
        add("chair", (-length / 2 - 0.65, 0, 0), rotation=(0, -math.pi / 2, 0))
        add("chair", (length / 2 + 0.65, 0, 0), rotation=(0, math.pi / 2, 0))
    add("plant", (-width / 2 + 0.55, 0, -depth / 2 + 0.6))
    add("plant", (width / 2 - 0.55, 0, depth / 2 - 0.6))
    add("cabinet", (-width / 2 + 0.55, 0, depth / 2 - 0.65))
    light_index = 0
    floor_index = 0
    initial_states = {}
    for device in config.devices:
        kind = device.device_type.value
        if kind == "lighting":
            # Two fixtures share a single logical circuit. No additional DeviceSpec.
            x = (light_index - 0.5) * width * 0.42 if room_id == "k1315" else 0
            for z in (-depth * 0.23, depth * 0.23):
                add(kind, (x, config.zones[0].height_m - 0.25, z), device_id=device.device_id,
                    label=f"{NAMES[kind]} · 回路 {light_index + 1}")
            light_index += 1
            initial_states[device.device_id] = LightingParameters().model_dump()
        elif kind == "hvac":
            add(kind, (0, 2.35, -depth / 2 + 0.18), device_id=device.device_id)
            initial_states[device.device_id] = HvacParameters().model_dump()
        elif kind == "ventilation":
            add(kind, (width / 2 - 0.8, 2.4, -depth / 2 + 0.2), device_id=device.device_id)
        elif kind == "projector":
            add(kind, (width * 0.22, 2.5, 0), device_id=device.device_id)
        else:
            add(kind, (-width / 2 + 0.65 + floor_index * 0.95, 0, -depth / 2 + 0.65),
                device_id=device.device_id)
            floor_index += 1
    add("sensor", (width / 2 - 0.15, 1.5, -0.5))
    return validate_scene(Scene.model_validate({
        "id": room_id, "room_id": room_id, "name": name,
        "dimensions": {"width": width, "depth": depth, "height": config.zones[0].height_m},
        "objects": objects, "initial_states": initial_states,
    }))


class SceneStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def load(self, room_id: str) -> Scene:
        if room_id not in ROOMS:
            raise KeyError(room_id)
        path = self.directory / f"{room_id}.json"
        if not path.exists():
            return default_scene(room_id)
        return validate_scene(Scene.model_validate_json(path.read_text(encoding="utf-8")))

    def save(self, room_id: str, scene: Scene) -> Scene:
        if room_id not in ROOMS:
            raise KeyError(room_id)
        if room_id != scene.id:
            raise ValueError("URL room ID and scene ID must match")
        scene = validate_scene(scene)
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, suffix=".tmp",
                                             encoding="utf-8", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(scene.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.directory / f"{room_id}.json")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return scene
