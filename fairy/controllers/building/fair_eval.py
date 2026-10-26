"""Opt-in execution parity helpers for Building L3 mode comparisons."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fairy.apps.building_world.operations_app import BuildingOperationsApp
from fairy.controllers.building.room_agents import DEFAULT_ROOM_TOOL_NAMES


def install_single_agent_room_preflight(
    agent: Any, operations: BuildingOperationsApp
) -> None:
    """Apply the room worker's device preflight to single-agent room tools.

    Only the executable tool map is wrapped. The model sees exactly the same
    function names, descriptions and argument schemas as before.
    """

    tool_map: dict[str, Callable[..., Any]] | None = getattr(agent, "tools_map", None)
    if tool_map is None:
        raise ValueError("single agent has no tool map")
    for tool_name, tool in tuple(tool_map.items()):
        if tool_name not in DEFAULT_ROOM_TOOL_NAMES:
            continue

        def guarded(*args: Any, _tool=tool, _name=tool_name, **kwargs: Any) -> Any:
            call_args = {
                item.name: value
                for item, value in zip(getattr(_tool, "args", ()), args, strict=False)
            }
            call_args.update(kwargs)
            rejection = operations.preflight_room_device_command(_name, call_args)
            if rejection is not None:
                return rejection
            return _tool(*args, **kwargs)

        tool_map[tool_name] = guarded
