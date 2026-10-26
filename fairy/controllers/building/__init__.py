"""Deterministic and lazily loaded control primitives for Building World."""

_EVENT_QUEUE_EXPORTS = {
    "BuildingEventQueue",
    "ScheduledBuildingEvent",
}

_TRIGGER_POLICY_EXPORTS = {
    "BuildingTriggerPolicy",
    "TriggerDecision",
}

_ROOM_AGENT_EXPORTS = {
    "DEFAULT_ROOM_TOOL_NAMES",
    "ROOM_AGENT_SYSTEM_PROMPT",
    "RoomAgentDefinition",
    "RoomAgentRegistry",
    "RoomAgentSession",
    "RoomContextApp",
    "RoomScopedToolset",
    "RoomScopeViolation",
    "resolve_event_room_ids",
}

_MULTI_AGENT_EXPORTS = {
    "DEFAULT_SUPERVISOR_APP_NAMES",
    "SUPERVISOR_COORDINATION_PROMPT",
    "SUPERVISOR_SYSTEM_PROMPT",
    "BuildingMultiAgentSystem",
    "BuildingSupervisorAgent",
    "RoomAgentManagerApp",
    "RoomEventRoute",
    "RoomEventRouter",
    "build_building_multi_agent_system",
    "select_supervisor_toolsets",
}


def __getattr__(name: str):
    """Load control modules without creating Building Runtime import cycles."""

    if name in _EVENT_QUEUE_EXPORTS:
        # Building World imports Runtime eagerly from its public package.  Load
        # that package first so event_queue never becomes the half-initialized
        # side of a Runtime <-> BuildingEventType cycle.
        import fairy.apps.building_world
        from fairy.controllers.building import event_queue

        return getattr(event_queue, name)
    if name in _TRIGGER_POLICY_EXPORTS:
        import fairy.apps.building_world  # noqa: F401
        from fairy.controllers.building import trigger_policy

        return getattr(trigger_policy, name)
    if name in _ROOM_AGENT_EXPORTS:
        from fairy.controllers.building import room_agents

        return getattr(room_agents, name)
    if name in _MULTI_AGENT_EXPORTS:
        from fairy.controllers.building import multi_agent

        return getattr(multi_agent, name)
    raise AttributeError(name)

__all__ = [
    "DEFAULT_ROOM_TOOL_NAMES",
    "DEFAULT_SUPERVISOR_APP_NAMES",
    "ROOM_AGENT_SYSTEM_PROMPT",
    "SUPERVISOR_COORDINATION_PROMPT",
    "SUPERVISOR_SYSTEM_PROMPT",
    "BuildingEventQueue",
    "BuildingMultiAgentSystem",
    "BuildingSupervisorAgent",
    "BuildingTriggerPolicy",
    "RoomAgentDefinition",
    "RoomAgentManagerApp",
    "RoomAgentRegistry",
    "RoomAgentSession",
    "RoomContextApp",
    "RoomEventRoute",
    "RoomEventRouter",
    "RoomScopeViolation",
    "RoomScopedToolset",
    "ScheduledBuildingEvent",
    "TriggerDecision",
    "build_building_multi_agent_system",
    "resolve_event_room_ids",
    "select_supervisor_toolsets",
]
