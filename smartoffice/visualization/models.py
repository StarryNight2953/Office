"""Serializable layout contracts, independent of simulation checkpoints."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RoomId = Literal["k1315", "k1316", "k1324"]
ObjectKind = Literal[
    "desk", "chair", "table", "plant", "cabinet", "lighting", "hvac",
    "sensor", "projector", "audio", "printer", "humidifier", "air_purifier",
    "ventilation", "access_control",
]
Finite = Annotated[float, Field(allow_inf_nan=False)]
Vector3 = tuple[Finite, Finite, Finite]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Dimensions(Contract):
    width: float = Field(gt=0, le=100, allow_inf_nan=False)
    depth: float = Field(gt=0, le=100, allow_inf_nan=False)
    height: float = Field(gt=0, le=20, allow_inf_nan=False)


class SceneObject(Contract):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    kind: ObjectKind
    position: Vector3 = (0, 0, 0)
    rotation: Vector3 = (0, 0, 0)
    scale: Vector3 = (1, 1, 1)
    device_id: str | None = Field(default=None, max_length=100)
    asset_url: str | None = None

    @field_validator("position")
    @classmethod
    def bounded_position(cls, value: Vector3) -> Vector3:
        if any(abs(v) > 1000 for v in value):
            raise ValueError("position must be within 1000 metres of the origin")
        return value

    @field_validator("scale")
    @classmethod
    def positive_scale(cls, value: Vector3) -> Vector3:
        if any(v <= 0 or v > 100 for v in value):
            raise ValueError("scale components must be in (0, 100]")
        return value

    @field_validator("asset_url")
    @classmethod
    def local_model(cls, value: str | None) -> str | None:
        if value is None:
            return value
        import re
        if not re.fullmatch(r"/models/[A-Za-z0-9_/-]+\.glb", value) or ".." in value:
            raise ValueError("asset_url must be a packaged /models/*.glb asset")
        return value


class LightingParameters(Contract):
    power_on: bool = False
    brightness_pct: int = Field(default=75, ge=0, le=100)
    color_temperature_k: int = Field(default=4000, ge=2700, le=6500)
    scene: str = Field(default="work", min_length=1, max_length=60)

    @model_validator(mode="after")
    def active_brightness(self):
        if self.power_on and self.brightness_pct == 0:
            raise ValueError("active lighting requires nonzero brightness")
        return self


class HvacParameters(Contract):
    power_on: bool = False
    mode: Literal["off", "cooling", "heating", "fan"] = "cooling"
    target_temperature_c: float = Field(default=24, ge=16, le=30, allow_inf_nan=False)
    fan_level: int = Field(default=2, ge=0, le=3)

    @model_validator(mode="after")
    def active_mode(self):
        if self.power_on and (self.mode == "off" or self.fan_level == 0):
            raise ValueError("active HVAC requires an operating mode and fan level")
        return self


class Scene(Contract):
    schema_version: Literal[1] = 1
    id: RoomId
    room_id: RoomId
    name: str = Field(min_length=1, max_length=100)
    schematic: Literal[True] = True
    dimensions: Dimensions
    objects: list[SceneObject] = Field(max_length=500)
    initial_states: dict[str, dict] = Field(default_factory=dict)

    @model_validator(mode="after")
    def identity(self):
        if self.id != self.room_id:
            raise ValueError("scene ID must match the existing room ID")
        if len({o.id for o in self.objects}) != len(self.objects):
            raise ValueError("visual object IDs must be unique")
        return self


class CreateRun(Contract):
    scene_id: RoomId


class DeviceCommand(Contract):
    device_id: str = Field(min_length=1, max_length=100)
    parameters: dict


class AdvanceTime(Contract):
    seconds: float = Field(gt=0, le=3600, allow_inf_nan=False)


class AgentRequest(Contract):
    mode: Literal["demo", "llm"] = "demo"
    task: str | None = Field(default=None, max_length=4000)
