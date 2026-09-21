"""Load and validate the human-reviewed Building L1 manifest."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).with_name("manifest.json")
WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
SCENARIO_ID_PATTERN = re.compile(
    r"^scenario_building_kechuang_l1_[a-z0-9]+(?:_[a-z0-9]+)*$"
)

ALLOWED_TOOLS = {
    "AirDeviceApp__set_air_purifier",
    "AirDeviceApp__set_humidifier",
    "BuildingOperationsApp__get_power_budget_status",
    "BuildingWorldApp__get_building_overview",
    "HvacApp__set_hvac",
    "LightingApp__set_lighting",
    "MeetingEquipmentApp__get_meeting_equipment_readiness",
    "MeetingEquipmentApp__set_audio_system",
    "MeetingEquipmentApp__set_projector",
    "OccupancyApp__get_person_presence",
    "PrintingApp__get_print_requests",
    "PrintingApp__set_printer_power",
    "PrintingApp__submit_print_job",
    "ResourceAllocationApp__get_active_reservations",
    "RoomApp__find_available_rooms",
    "ScheduleApp__cancel_meeting",
    "ScheduleApp__create_meeting",
    "ScheduleApp__get_person_schedule",
    "ScheduleApp__get_room_schedule",
    "SystemApp__advance_time",
    "VentilationApp__set_ventilation",
}
WRITE_TOOLS = {
    "AirDeviceApp__set_air_purifier",
    "AirDeviceApp__set_humidifier",
    "HvacApp__set_hvac",
    "LightingApp__set_lighting",
    "MeetingEquipmentApp__set_audio_system",
    "MeetingEquipmentApp__set_projector",
    "PrintingApp__set_printer_power",
    "PrintingApp__submit_print_job",
    "ScheduleApp__cancel_meeting",
    "ScheduleApp__create_meeting",
    "VentilationApp__set_ventilation",
}


class ManifestError(ValueError):
    """Raised when reviewed L1 data is incomplete or internally inconsistent."""


def load_manifest(path: str | Path | None = None) -> dict[str, Any]:
    manifest_path = Path(path) if path is not None else MANIFEST_PATH
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    validate_manifest(manifest)
    return manifest


def accepted_contracts(
    manifest: dict[str, Any], selected_ids: set[str] | None = None
) -> list[dict[str, Any]]:
    contracts = list(manifest["contracts"])
    by_id = {item["scenario_id"]: item for item in contracts}
    if selected_ids is not None:
        missing = selected_ids - set(by_id)
        if missing:
            raise ManifestError(f"unknown scenario ids: {sorted(missing)}")
        rejected = [
            scenario_id
            for scenario_id in selected_ids
            if by_id[scenario_id]["review_status"] != "accepted_l1"
        ]
        if rejected:
            raise ManifestError(
                "selected entries are not accepted_l1: " + ", ".join(sorted(rejected))
            )
        contracts = [item for item in contracts if item["scenario_id"] in selected_ids]
    else:
        contracts = [
            item for item in contracts if item["review_status"] == "accepted_l1"
        ]
    return sorted(contracts, key=lambda item: item["scenario_id"])


def load_contract(scenario_id: str) -> dict[str, Any]:
    manifest = load_manifest()
    matches = [
        item for item in manifest["contracts"] if item["scenario_id"] == scenario_id
    ]
    if len(matches) != 1:
        raise ManifestError(f"manifest contains no unique contract for {scenario_id!r}")
    contract = matches[0]
    if contract["review_status"] != "accepted_l1":
        raise ManifestError(f"contract {scenario_id!r} is not accepted_l1")
    return contract


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 2:
        raise ManifestError("schema_version must be 2")
    contracts = manifest.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        raise ManifestError("contracts must be a non-empty list")
    scope = manifest.get("scope")
    if not isinstance(scope, dict):
        raise ManifestError("scope must be an object")
    if scope.get("fully_split"):
        if scope.get("source_l3_reviewed") != list(range(1, 21)):
            raise ManifestError("fully split manifest must cover L3-01 through L3-20")
        if scope.get("accepted_l1_count") != len(contracts):
            raise ManifestError("scope accepted_l1_count differs from contracts")
        if not scope.get("candidate_disposition_complete"):
            raise ManifestError("fully split manifest requires complete dispositions")

    scenario_ids: set[str] = set()
    class_names: set[str] = set()
    claimed_actions: dict[tuple[str, str], str] = {}
    for contract in contracts:
        scenario_id = _required_string(contract, "scenario_id")
        class_name = _required_string(contract, "class_name")
        if not SCENARIO_ID_PATTERN.fullmatch(scenario_id):
            raise ManifestError(f"invalid L1 scenario_id: {scenario_id}")
        if scenario_id in scenario_ids:
            raise ManifestError(f"duplicate scenario_id: {scenario_id}")
        if class_name in class_names:
            raise ManifestError(f"duplicate class_name: {class_name}")
        scenario_ids.add(scenario_id)
        class_names.add(class_name)

        if contract.get("review_status") != "accepted_l1":
            raise ManifestError(
                f"manifest entry {scenario_id} must be accepted_l1"
            )
        review = contract.get("review")
        if not isinstance(review, dict) or review.get("decision") != "accepted_l1":
            raise ManifestError(f"{scenario_id}: accepted review decision is required")
        if not isinstance(review.get("basis"), list) or not review["basis"]:
            raise ManifestError(f"{scenario_id}: review.basis cannot be empty")
        _required_string(review, "reviewed_at")
        _required_string(contract, "behavior_family")
        start_at = datetime.fromisoformat(_required_string(contract, "start_at"))
        if start_at.tzinfo is None:
            raise ManifestError(f"{scenario_id}: start_at must be timezone-aware")
        if not isinstance(contract.get("duration_seconds"), int) or contract[
            "duration_seconds"
        ] <= 0:
            raise ManifestError(f"{scenario_id}: duration_seconds must be positive")
        task = contract.get("task")
        if not isinstance(task, dict):
            raise ManifestError(f"{scenario_id}: task must be an object")
        _required_string(task, "goal")
        _required_string(task, "prompt")
        _required_string(task, "completion_report")

        source = contract.get("source")
        if not isinstance(source, dict):
            raise ManifestError(f"{scenario_id}: source must be an object")
        source_kind = _required_string(source, "kind")
        if source_kind == "diagnostic_representative":
            _required_string(source, "l3_scenario_id")
            _required_string(source, "candidate_id")
            _required_string(source, "diagnostic_file")
            _required_string(source, "source_fingerprint")
        elif source_kind == "supplemental_fixture":
            _required_string(source, "fixture_scenario_id")
        else:
            raise ManifestError(f"{scenario_id}: unsupported source kind {source_kind}")
        action_ids = source.get("action_ids")
        if not isinstance(action_ids, list) or not action_ids:
            raise ManifestError(f"{scenario_id}: source.action_ids cannot be empty")
        if len(action_ids) != len(set(action_ids)):
            raise ManifestError(f"{scenario_id}: duplicate source action ids")

        coverage = contract.get("coverage")
        if not isinstance(coverage, dict):
            raise ManifestError(f"{scenario_id}: coverage must be an object")
        variants = coverage.get("source_variants")
        if not isinstance(variants, list):
            raise ManifestError(f"{scenario_id}: coverage.source_variants must be a list")
        if coverage.get("merged_variant_count") != len(variants):
            raise ManifestError(f"{scenario_id}: merged_variant_count is inconsistent")
        obligations = coverage.get("obligation_refs")
        if not isinstance(obligations, list) or not obligations:
            raise ManifestError(f"{scenario_id}: coverage.obligation_refs cannot be empty")
        for obligation in obligations:
            if not isinstance(obligation, dict):
                raise ManifestError(f"{scenario_id}: invalid obligation reference")
            _required_string(obligation, "source_l3_id")
            _required_string(obligation, "obligation_id")
        representative_covered = source_kind == "supplemental_fixture"
        for variant in variants:
            if not isinstance(variant, dict):
                raise ManifestError(f"{scenario_id}: invalid source variant")
            variant_kind = _required_string(variant, "kind")
            if variant_kind not in {"diagnostic_candidate", "diagnostic_context"}:
                raise ManifestError(
                    f"{scenario_id}: unsupported coverage variant kind {variant_kind}"
                )
            source_l3_id = _required_string(variant, "l3_scenario_id")
            _required_string(variant, "diagnostic_file")
            _required_string(variant, "source_fingerprint")
            if variant_kind == "diagnostic_candidate":
                _required_string(variant, "candidate_id")
            variant_actions = variant.get("action_ids")
            if not isinstance(variant_actions, list) or not variant_actions:
                raise ManifestError(f"{scenario_id}: variant action_ids cannot be empty")
            for action_id in variant_actions:
                key = (source_l3_id, str(action_id))
                if key in claimed_actions:
                    raise ManifestError(
                        f"source action {key} is claimed by both "
                        f"{claimed_actions[key]} and {scenario_id}"
                    )
                claimed_actions[key] = scenario_id
            if (
                source_kind == "diagnostic_representative"
                and source_l3_id == source["l3_scenario_id"]
                and variant.get("candidate_id") == source["candidate_id"]
                and set(action_ids) <= set(variant_actions)
            ):
                representative_covered = True
        if not representative_covered:
            raise ManifestError(
                f"{scenario_id}: representative source is absent from coverage variants"
            )

        initial_state = contract.get("initial_state")
        if not isinstance(initial_state, dict):
            raise ManifestError(f"{scenario_id}: initial_state must be an object")
        for key in (
            "devices",
            "rooms",
            "print_requests",
            "print_jobs",
            "schedule",
            "reservations",
        ):
            if not isinstance(initial_state.get(key, []), list):
                raise ManifestError(f"{scenario_id}: initial_state.{key} must be a list")
        if not isinstance(initial_state.get("id_counters", {}), dict):
            raise ManifestError(f"{scenario_id}: initial_state.id_counters must be an object")

        steps = contract.get("oracle_steps")
        if not isinstance(steps, list) or not steps:
            raise ManifestError(f"{scenario_id}: oracle_steps cannot be empty")
        step_ids: set[str] = set()
        owned_actions: set[str] = set()
        for step in steps:
            step_id = _required_string(step, "id")
            tool = _required_string(step, "tool")
            if step_id in step_ids:
                raise ManifestError(f"{scenario_id}: duplicate step id {step_id}")
            if tool not in ALLOWED_TOOLS:
                raise ManifestError(f"{scenario_id}: unsupported tool {tool}")
            if not isinstance(step.get("args", {}), dict):
                raise ManifestError(f"{scenario_id}/{step_id}: args must be an object")
            step_ids.add(step_id)
            linked = step.get("source_action_ids", [])
            if tool in WRITE_TOOLS and not linked:
                raise ManifestError(
                    f"{scenario_id}/{step_id}: write step lacks source_action_ids"
                )
            if not isinstance(linked, list):
                raise ManifestError(
                    f"{scenario_id}/{step_id}: source_action_ids must be a list"
                )
            owned_actions.update(str(item) for item in linked)
        if owned_actions != set(action_ids):
            raise ManifestError(
                f"{scenario_id}: oracle/source ownership differs: "
                f"owned={sorted(owned_actions)}, source={sorted(action_ids)}"
            )

        validation = contract.get("validation")
        if not isinstance(validation, dict):
            raise ManifestError(f"{scenario_id}: validation must be an object")
        if validation.get("kind") not in {
            "device_state",
            "printing",
            "meeting_booking",
            "meeting_change",
        }:
            raise ManifestError(f"{scenario_id}: unsupported validation kind")
        confirmation_tools = validation.get("confirmation_tools")
        if not isinstance(confirmation_tools, list) or not confirmation_tools:
            raise ManifestError(f"{scenario_id}: confirmation_tools cannot be empty")
        if not set(confirmation_tools) <= ALLOWED_TOOLS:
            raise ManifestError(f"{scenario_id}: unsupported confirmation tool")
        meaningful = validation.get("meaningful_source_action_ids", [])
        if not isinstance(meaningful, list) or not set(meaningful) <= set(action_ids):
            raise ManifestError(
                f"{scenario_id}: meaningful source actions must belong to the contract"
            )
        if scope.get("fully_split") and not meaningful:
            raise ManifestError(
                f"{scenario_id}: fully split contract needs a meaningful source action"
            )
        if validation["kind"] in {"meeting_booking", "meeting_change"}:
            expected_meeting = validation.get("expected_meeting")
            if not isinstance(expected_meeting, dict):
                raise ManifestError(f"{scenario_id}: expected_meeting is required")
            for key in (
                "room_id",
                "organizer_id",
                "start_at",
                "end_at",
            ):
                _required_string(expected_meeting, key)
        if validation["kind"] == "meeting_change":
            _required_string(validation, "old_meeting_id")
            _required_string(validation, "old_reservation_id")


def validate_source_links(
    manifest: dict[str, Any], workspace_root: str | Path = WORKSPACE_ROOT
) -> None:
    """Fail if an accepted decision no longer matches its diagnostic source."""

    root = Path(workspace_root)
    diagnostics: dict[Path, dict[str, Any]] = {}
    for contract in accepted_contracts(manifest):
        for variant in contract["coverage"]["source_variants"]:
            diagnostic_path = root / variant["diagnostic_file"]
            if diagnostic_path not in diagnostics:
                with diagnostic_path.open("r", encoding="utf-8") as handle:
                    diagnostics[diagnostic_path] = json.load(handle)
            diagnostic = diagnostics[diagnostic_path]
            if diagnostic.get("source_l3_id") != variant["l3_scenario_id"]:
                raise ManifestError(
                    f"{contract['scenario_id']}: diagnostic L3 identity changed"
                )
            if diagnostic.get("source_fingerprint") != variant["source_fingerprint"]:
                raise ManifestError(
                    f"{contract['scenario_id']}: diagnostic source fingerprint changed"
                )
            claimed = set(variant["action_ids"])
            if variant["kind"] == "diagnostic_candidate":
                candidates = [
                    item
                    for item in diagnostic.get("candidates", [])
                    if item.get("id") == variant["candidate_id"]
                ]
                if len(candidates) != 1:
                    raise ManifestError(
                        f"{contract['scenario_id']}: source candidate missing or duplicated"
                    )
                available = set(candidates[0].get("source_action_ids", []))
                available_evidence = set(
                    candidates[0].get("source_evidence_ids", [])
                )
                claimed_evidence = set(variant.get("evidence_ids", []))
                if not claimed_evidence <= available_evidence:
                    raise ManifestError(
                        f"{contract['scenario_id']}: source evidence drifted: "
                        f"{sorted(claimed_evidence - available_evidence)}"
                    )
            else:
                available = {
                    item.get("source_id")
                    for item in diagnostic.get("l3_context", [])
                    if item.get("source_id")
                }
            if not claimed <= available:
                raise ManifestError(
                    f"{contract['scenario_id']}: source actions drifted: "
                    f"{sorted(claimed - available)}"
                )


def _required_string(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{key} must be a non-empty string")
    return value
