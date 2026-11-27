"""Twenty thin Scenario classes backed by the shared L3 execution core."""

from smartoffice.scenarios.building_kechuang.l3.base import SpecDrivenBuildingL3Scenario
from smartoffice.scenarios.building_kechuang.l3.catalog import L3_SPECS
from smartoffice.scenarios.registry import register_scenario


def _registered(number: int):
    """Bind a catalog Spec and registry ID to an explicit scenario class."""

    spec = L3_SPECS[number - 1]

    def decorate(cls):
        cls.spec = spec
        return register_scenario(spec.scenario_id)(cls)

    return decorate


@_registered(1)
class ScenarioKechuangL3SummerHotHumidDay(SpecDrivenBuildingL3Scenario):
    pass


@_registered(2)
class ScenarioKechuangL3WinterColdDryDay(SpecDrivenBuildingL3Scenario):
    pass


@_registered(3)
class ScenarioKechuangL3ShoulderLowEnergyDay(SpecDrivenBuildingL3Scenario):
    pass


@_registered(4)
class ScenarioKechuangL3SolarPeakDay(SpecDrivenBuildingL3Scenario):
    pass


@_registered(5)
class ScenarioKechuangL3OutdoorPm25Day(SpecDrivenBuildingL3Scenario):
    pass


@_registered(6)
class ScenarioKechuangL3K1324StaggeredOfficeDay(SpecDrivenBuildingL3Scenario):
    pass


@_registered(7)
class ScenarioKechuangL3K1316ConsecutiveSeminars(SpecDrivenBuildingL3Scenario):
    pass


@_registered(8)
class ScenarioKechuangL3K1315DefenseDay(SpecDrivenBuildingL3Scenario):
    pass


@_registered(9)
class ScenarioKechuangL3ParallelMeetings(SpecDrivenBuildingL3Scenario):
    pass


@_registered(10)
class ScenarioKechuangL3ThreeRoomHighOccupancy(SpecDrivenBuildingL3Scenario):
    pass


@_registered(11)
class ScenarioKechuangL3MeetingDownsize(SpecDrivenBuildingL3Scenario):
    pass


@_registered(12)
class ScenarioKechuangL3MeetingUpgrade(SpecDrivenBuildingL3Scenario):
    pass


@_registered(13)
class ScenarioKechuangL3MeetingAdvanced(SpecDrivenBuildingL3Scenario):
    pass


@_registered(14)
class ScenarioKechuangL3EquipmentRequirementAdded(SpecDrivenBuildingL3Scenario):
    pass


@_registered(15)
class ScenarioKechuangL3EarlyFinishRecovery(SpecDrivenBuildingL3Scenario):
    pass


@_registered(16)
class ScenarioKechuangL3PowerLimitedParallelDay(SpecDrivenBuildingL3Scenario):
    pass


@_registered(17)
class ScenarioKechuangL3StrictUnoccupiedEnergy(SpecDrivenBuildingL3Scenario):
    pass


@_registered(18)
class ScenarioKechuangL3PrintingMaterialCoordination(SpecDrivenBuildingL3Scenario):
    pass


@_registered(19)
class ScenarioKechuangL3WinterVentilationHeatingTradeoff(SpecDrivenBuildingL3Scenario):
    pass


@_registered(20)
class ScenarioKechuangL3MultiCommitmentPriorityDay(SpecDrivenBuildingL3Scenario):
    pass


L3_SCENARIO_CLASSES = (
    ScenarioKechuangL3SummerHotHumidDay,
    ScenarioKechuangL3WinterColdDryDay,
    ScenarioKechuangL3ShoulderLowEnergyDay,
    ScenarioKechuangL3SolarPeakDay,
    ScenarioKechuangL3OutdoorPm25Day,
    ScenarioKechuangL3K1324StaggeredOfficeDay,
    ScenarioKechuangL3K1316ConsecutiveSeminars,
    ScenarioKechuangL3K1315DefenseDay,
    ScenarioKechuangL3ParallelMeetings,
    ScenarioKechuangL3ThreeRoomHighOccupancy,
    ScenarioKechuangL3MeetingDownsize,
    ScenarioKechuangL3MeetingUpgrade,
    ScenarioKechuangL3MeetingAdvanced,
    ScenarioKechuangL3EquipmentRequirementAdded,
    ScenarioKechuangL3EarlyFinishRecovery,
    ScenarioKechuangL3PowerLimitedParallelDay,
    ScenarioKechuangL3StrictUnoccupiedEnergy,
    ScenarioKechuangL3PrintingMaterialCoordination,
    ScenarioKechuangL3WinterVentilationHeatingTradeoff,
    ScenarioKechuangL3MultiCommitmentPriorityDay,
)
