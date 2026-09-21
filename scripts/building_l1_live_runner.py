"""Run staged Kechuang Building L1 experiments against a live Qwen endpoint."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from fairy.scenarios.building_kechuang.l1.manifest import (
    accepted_contracts,
    load_manifest,
)
from scripts import building_l3_live_runner as shared


def _all_scenario_ids() -> tuple[str, ...]:
    return tuple(
        contract["scenario_id"]
        for contract in accepted_contracts(load_manifest())
    )


def _scenario_id(behavior_family: str) -> str:
    matches = [
        contract["scenario_id"]
        for contract in accepted_contracts(load_manifest())
        if contract["behavior_family"] == behavior_family
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one accepted L1 for {behavior_family!r}, found {matches}"
        )
    return matches[0]


# Smoke isolates the common read-control-confirm pattern. Diagnostic adds the
# main stateful and multi-object business families before spending a full run
# on every accepted contract in the manifest.
STAGES = {
    "smoke": (_scenario_id("hvac_control"),),
    "repair": (
        _scenario_id("ventilation_control"),
        _scenario_id("power_constrained_adjustment"),
    ),
    "meeting": (
        _scenario_id("meeting_room_booking"),
        _scenario_id("meeting_requirement_change"),
    ),
    "diagnostic": (
        _scenario_id("hvac_control"),
        _scenario_id("printer_control"),
        _scenario_id("meeting_room_booking"),
        _scenario_id("meeting_requirement_change"),
        _scenario_id("meeting_room_recovery"),
        _scenario_id("power_constrained_adjustment"),
    ),
    "all": _all_scenario_ids(),
}


def scenario_ids_for_stage(stage: str) -> tuple[str, ...]:
    """Return stable registry IDs for one L1 experiment stage."""

    return STAGES[stage]


def run_stage(args: argparse.Namespace) -> Path:
    """Run one L1 stage through the shared Building live-model protocol."""

    return shared.run_scenarios(
        args,
        scenario_ids_for_stage(args.stage),
        experiment_title="Building L1 live-model experiment",
    )


def _default_output_root() -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return Path("runs") / f"building_qwen_l1_{timestamp}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=tuple(STAGES), default="smoke")
    parser.add_argument("--endpoint", default=shared.DEFAULT_ENDPOINT)
    parser.add_argument("--model", default=shared.DEFAULT_MODEL)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--max-tool-calls", type=int, default=80)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rerun", action="store_true")
    args = parser.parse_args()
    args.output_root = args.output_root or _default_output_root()
    shared.configure_direct_endpoint(args.endpoint)

    if args.dry_run:
        print(f"stage={args.stage} output_root={args.output_root}")
        run_stage(args)
        return
    if not args.skip_preflight:
        result = shared.preflight(args.endpoint, args.model, timeout_seconds=30.0)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if args.preflight_only:
        return
    summary_path = run_stage(args)
    print(summary_path)


if __name__ == "__main__":
    main()
