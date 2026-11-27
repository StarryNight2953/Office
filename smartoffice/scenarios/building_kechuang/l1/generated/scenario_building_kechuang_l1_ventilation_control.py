"""Generated from l1/manifest.json; do not edit this module directly."""

from smartoffice.scenarios.building_kechuang.l1.base import (
    ManifestDrivenBuildingL1Scenario,
    contract_timestamp,
)
from smartoffice.scenarios.building_kechuang.l1.manifest import load_contract
from smartoffice.scenarios.registry import register_scenario

CONTRACT = load_contract("scenario_building_kechuang_l1_ventilation_control")


@register_scenario("scenario_building_kechuang_l1_ventilation_control")
class ScenarioBuildingKechuangL1VentilationControl(ManifestDrivenBuildingL1Scenario):
    contract_id = "scenario_building_kechuang_l1_ventilation_control"
    start_time: float | None = contract_timestamp(CONTRACT)
    duration: float | None = float(CONTRACT["duration_seconds"])
    time_increment_in_seconds: int = 60
    scenario_input: str = CONTRACT["task"]["prompt"]
