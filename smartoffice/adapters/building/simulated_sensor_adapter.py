"""Push-to-pull bridge for readings produced by the observation model."""

from smartoffice.adapters.building.base_sensor_adapter import PushSensorAdapter


class SimulatedSensorAdapter(PushSensorAdapter):
    """Named adapter type used to distinguish simulation from real providers."""
