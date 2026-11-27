# Kechuang Building Project Memory

Last updated: 2026-08-29

This note is a recovery document for future work on the Kechuang smart-building branch of FAIRY. It records what has been built, why the current architecture looks the way it does, which experiments have already been run, and what should be addressed next.

## 1. Project Direction

The building work started from the original `building_k1324` code, but the user explicitly allowed necessary restructuring instead of preserving the old code shape. The implementation has since moved toward a Kechuang building testbed with three rooms, real/simulated sensors, controllable devices, and L3 scenarios that can be used to stress-test a building agent.

The current design target is not just a static room-booking demo. The goal is an agentic building-control benchmark where the agent must:

- maintain stable indoor environment and energy behavior;
- honor room bookings, meeting preparation, user plan changes, and device requirements;
- use tools through a realistic workflow;
- survive multi-hour or whole-day executable timelines;
- expose both scenario/code problems and model/controller limitations through live model runs.

## 2. Namespace And Room Model

The misleading `smartoffice/scenarios/building_k1324/` scope was migrated to:

```text
smartoffice/scenarios/building_kechuang/
```

Rooms are now modeled as rooms under the Kechuang building rather than treating K1324 as the whole domain.

Current room assumptions:

| Room | Role | Bookable | Capacity | Main setting |
|---|---:|---:|---:|---|
| K1324 | Graduate office | No | 17 | Two single-person professor offices, one 3-seat cubicle, and 12 graduate workstations |
| K1316 | Seminar room | Yes | 8 | Seminar/discussion room with projector, whiteboard, and video conference support |
| K1315 | Conference room | Yes | 20 | Larger meeting room with projector, audio, microphone, printing, video, and richer lighting |

K1324 currently uses one physical zone, `k1324_office_zone`, even though its human-facing description contains subspaces. This is acceptable for the current simulation phase, but field deployment should eventually decide whether the two professor offices and the 3-seat cubicle need separate sensors or thermal zones.

The present areas, capacities, envelope parameters, HVAC capacities, and initial physical states are simulation values. They should be treated as placeholders until measured or calibrated with real sensor data.

## 3. Architecture Snapshot

The building stack is layered as follows:

```text
Scenario layer
  -> goals, hidden interactions, executable timelines, Oracle DAG, validation

BuildingWorldApp layer
  -> business state and callable tools
  -> room, schedule, occupancy, device, HVAC, air, ventilation, lighting,
     meeting equipment, printing, sensor, operations

Runtime layer
  -> event queue, time advancement, scheduled completion, event-boundary wakeups

Physics layer
  -> equipment effects, indoor environment evolution, observation model, sensor hub

Adapter / simulator layer
  -> real protocol adapters and MQTT sensor simulator
```

The most important architectural distinction is the two-layer scenario structure:

```text
build_events_flow()
  describes the reference Agent/Oracle tool workflow.

SystemApp.advance_time() + Runtime
  produce the real minute-by-minute environment evolution and scheduled events.
```

This distinction matters because a scenario with only a nice DAG can look correct while never testing physical behavior. The current L3 scenarios therefore define both the Agent/Oracle workflow and an executable timeline with real runtime events, sensor readings, device effects, scheduled print completions, and final validators.

## 4. Key App And Tool Responsibilities

`BuildingWorldApp` owns the business-facing tool surface. The current tool families are:

- `RoomApp`: room metadata, capacity, bookability, equipment availability.
- `ScheduleApp`: booking and booking updates for K1315/K1316.
- `OccupancyApp`: current and scheduled room occupancy.
- `SensorApp`: current readings through `SensorHub`.
- `HVACApp`: setpoint and HVAC mode/level control.
- `VentilationApp`: fresh-air and ventilation control.
- `AirDeviceApp`: humidifier, purifier, and related air-treatment devices.
- `LightingApp`: room lighting modes.
- `MeetingEquipmentApp`: projector, audio, microphone, and video preparation.
- `PrintingApp`: printing/material requests, priority, deadlines, and scheduled completion.
- `OperationsApp`: operational status, commitments, readiness, power status, and guarded environment-control decisions.

The building controller now has a building-specific configuration and a `building_baseline_react` style controller path. The practical lesson from live testing is that this controller needs stronger context management before the most complex L3 tasks become stable.

## 5. Devices And Resources

Current controllable device assumptions:

| Room | Devices/resources |
|---|---|
| K1324 | HVAC, humidifier, purifier, ventilation |
| K1316 | HVAC, ventilation, projector, lighting |
| K1315 | HVAC, humidifier, purifier, ventilation, projector, audio, microphone, printer, front lighting, audience lighting |

Printing is no longer an instant flag. It is modeled as scheduled work with:

- submission time;
- page count / material requirement;
- deadline;
- priority;
- serialized printer execution;
- runtime completion event.

This lets meeting scenarios test whether the agent coordinates materials before the meeting rather than merely turning on conference equipment.

## 6. Physics And Runtime

The indoor physical stack currently lives under:

```text
smartoffice/physics/building/
```

Important files include:

- `models.py`: building physical state and configuration models.
- `equipment_model.py`: device effects on heat, humidity, airflow, purification, and power.
- `indoor_environment_engine.py`: indoor environmental state evolution.
- `observation_model.py`: sensor observation/noise layer.
- `sensor_api.py`: canonical sensor readings and sensor hub.
- `orchestrator.py`: runtime physics orchestration.

The physical runtime path is:

```text
device commands
  -> EquipmentModel
  -> IndoorEnvironmentEngine
  -> physical truth state
  -> ObservationModel
  -> SensorHub readings
  -> BuildingWorldApp tools
```

The runtime supports:

- event queue;
- stable event ordering;
- checkpoint/snapshot behavior;
- event-boundary wakeups when `advance_time()` crosses relevant user or system events;
- scheduled printing completion;
- initial sensor readings at scenario start.

Known simplification: true asynchronous interruption is not fully implemented. The current design wakes at tool/time-advance boundaries. This is good enough for the first L3 testbed but should be remembered when designing scenarios that depend on second-by-second interruption.

## 7. Sensors, MQTT, And Real Data Plan

The sensor simulator was intentionally placed under:

```text
smartoffice/simulators/building/
```

This location was chosen because the simulator is an external data-source emulator rather than core building physics. The physics core should remain protocol-agnostic.

MQTT data path:

```text
Building Runtime
  -> SensorReading
  -> MQTT publisher
  -> broker
  -> subscriber
  -> adapter
  -> SensorHub
```

Current MQTT topic pattern:

```text
kechuang/<room_id>/environment/<temperature|humidity|co2|pm25>
```

Supported/currently planned quantities:

- indoor air temperature;
- relative humidity;
- CO2;
- PM2.5.

Simulator features already planned/implemented around this idea include accelerated mode, real-time mode, paho MQTT support, authentication/TLS parameters, and QoS/confirmation behavior.

Real deployment should use a shadow mode first:

- keep simulation source and real sensor source simultaneously;
- compare bias, MAE, RMSE, max absolute error, and timestamp offset;
- calibrate model parameters before allowing real data to replace simulated readings.

Recommended hardware rollout:

| Phase | Sensors |
|---|---|
| Phase 0 | Indoor temperature/RH/CO2 combo sensor per relevant room; outdoor temperature/RH from public weather service |
| Phase 1 | PM2.5, occupancy, and power sensing |
| Phase 2 | optional supply/return air temperature, differential pressure, water temperature, or HVAC-side sensors |

Outdoor weather should not be over-instrumented early. Public weather service data is sufficient for early development unless the building envelope model needs local microclimate calibration.

## 8. Scenario Philosophy

The scenario strategy is top-down first, then bottom-up refinement:

- build enough L3 scenarios to cover meaningful whole-day building situations;
- use a shared standardized building workflow so scenarios remain comparable;
- vary the scenario dimensions to create diversity;
- later derive L2 and L1 units from real L3 traces and failure patterns.

The current L1/L2/L3 interpretation follows the earlier agriculture benchmark style:

| Level | Meaning in building benchmark |
|---|---|
| L1 | Minimal atomic decision/action plus verification, such as reading sensors then changing ventilation |
| L2 | Short independent feedback loop, such as preparing one meeting room and verifying readiness |
| L3 | Long-horizon, whole-day or multi-activity scenario composed of multiple L2 loops |

The shared L3 workflow is:

```text
Observe -> Reconcile -> Prioritize -> Plan/Patch -> Act -> Wait -> Verify -> Commit -> Monitor
```

For now, scenarios intentionally focus on normal building operation. Faults, emergencies, abnormal device failure, and disaster events are out of scope until normal operations are stable.

## 9. Current Scenario Inventory

Before the L3 expansion, these normal scenarios existed:

- cross-room room booking;
- K1324 climate coordination;
- K1324 occupancy ramp;
- K1316 standard seminar;
- K1315 standard conference.

The L3 expansion then added 20 complete building scenarios under:

```text
smartoffice/scenarios/building_kechuang/l3/
```

Core files:

- `specs.py`: declarative scenario spec and dynamic outdoor profiles.
- `catalog.py`: 20 scenario definitions.
- `base.py`: shared workflow, Oracle behavior, runtime construction, validation.
- `scenarios.py`: 20 thin registered Scenario classes.

The 20 L3 scenarios are:

| ID | Scenario | Main stress |
|---|---|---|
| L3-01 | Summer hot humid | cooling, dehumidification, comfort under humid heat |
| L3-02 | Winter cold dry | heating, humidification, winter comfort |
| L3-03 | Shoulder low energy | comfort with low-energy operation |
| L3-04 | Solar peak | afternoon thermal load and preconditioning |
| L3-05 | Outdoor PM2.5 | ventilation versus purification |
| L3-06 | K1324 staggered occupancy | office occupancy ramp and recovery |
| L3-07 | K1316 consecutive seminars | back-to-back small meetings |
| L3-08 | K1315 defense day | multi-stage defense meeting with equipment and air-quality load |
| L3-09 | Parallel meetings | resource coordination across K1315/K1316 |
| L3-10 | Three-room high occupancy | high CO2 and multi-room load |
| L3-11 | Meeting downsizing | user changes to smaller room |
| L3-12 | Meeting upgrade | user changes to larger room |
| L3-13 | Meeting advanced | user moves meeting earlier |
| L3-14 | Equipment requirement added | late device/material requirement |
| L3-15 | Early finish recovery | meeting ends early and room returns to normal |
| L3-16 | Power-limited parallel day | power budget and priority handling |
| L3-17 | Strict unoccupied energy | energy discipline during vacancy |
| L3-18 | Printing/material coordination | meeting material preparation |
| L3-19 | Winter ventilation-heating tradeoff | CO2 control while preserving heat |
| L3-20 | Multi-commitment priority day | combined priorities, meetings, printing, and environment |

All 20 scenarios are independently registered. They share one implementation kernel but differ through declarative specs: weather, occupancy timeline, bookings, user interactions, device policies, print jobs, power limits, and primary metrics.

Future user interactions are hidden until runtime reveal time. This avoids leaking later user changes into the agent prompt too early.

Workflow review exports were generated under:

```text
workflow_exports/building_kechuang_l3_review/
```

Those exports are for manual review and are generally treated as generated artifacts.

## 10. Important Fixes Already Made

Major scenario/code issues already fixed:

- K1324 made non-bookable while K1315/K1316 remain bookable.
- Runtime event queue added with stable ordering.
- `advance_time()` can stop at relevant event boundaries.
- Initial sensor readings exist at scenario start.
- Future interactions and printing requirements are hidden until their reveal time.
- Printing is scheduled, serialized, prioritized, and completed by runtime events.
- Meeting readiness reads the latest device state rather than stale assumptions.
- Boundary guards prevent final answers before required commitments are satisfied.
- Tool schemas and parameter ranges were tightened.
- Validator failures now provide more actionable reasons.
- Ventilation heat recovery was added to reduce unrealistic winter overcooling.
- Season-specific initial conditions and meeting prep lead times were added.
- Prep lead times include 30/60/90 minute modes.
- Operational status tools now expose power, commitments, readiness, and environmental control context.
- Air-quality warning thresholds were unified: CO2 warning around 1000 ppm, PM2.5 warning around 35 ug/m3.
- Default hard validator for CO2 is stricter, around 1200 ppm unless scenario-specific logic says otherwise.
- HVAC max level is required when temperature violations require aggressive correction.
- Time-advance preconditions can see the target timestamp.

Offline Oracle replay currently passes 20/20 for the L3 suite. DAG sizes after expansion are roughly 58 to 173 nodes, with median about 91.5. Agriculture L3 scenarios previously had larger DAGs, about 214 to 383 steps, but that difference is not automatically a defect because agriculture daily/field operations have different granularity.

## 11. Live Model Testing Status

The live model target used most recently:

```text
Endpoint: http://100.115.106.71:8000/v1
Model: Qwen3.6-35B-A3B-FP8
Provider: vLLM
Function calls: non-parallel
Thinking: false
Conda env: are
```

The Tailscale IP has changed before, so treat the endpoint as last known rather than permanent.

Runner:

```text
scripts/building_l3_live_runner.py
```

The runner supports preflight, smoke, diagnostic, and full stages. It is designed to resume by reusing run reports and output roots.

Latest recorded final full batch:

```text
runs/building_qwen36_35b_l3_all_final_20260828/summary.md
```

Final result from that run:

```text
17 / 20 passed
2579 tool calls
2597 LLM calls
110,388,889 tokens
8963.70 seconds, about 2 h 29 min
```

Failed or incomplete cases:

| Scenario | Status | Interpretation |
|---|---|---|
| L3-10 | Environment invariant failed | Model reacted too late to peak CO2 in K1315; this is mainly a model control/self-verification issue |
| L3-16 | Context overflow | Controller/runtime issue: prompt exceeded 128k context after hundreds of calls |
| L3-20 | Context overflow | Controller/runtime issue: long multi-commitment scenario overflowed before completion |

Important interpretation:

Qwen3.6-35B should not simply be summarized as "only completes 17 scenarios." A more accurate reading is:

- 17 scenarios completed under the current full-batch setup;
- 1 scenario exposed a model control weakness around proactive high-occupancy CO2 management;
- 2 scenarios were cut off by controller context overflow, not by a proven inability to solve the business task.

Earlier test history:

- The first broad live baseline was much worse, around 2/20 before class-1 scenario and interface fixes.
- Several diagnostic rounds fixed solvability and implementation issues.
- A targeted L3-20 run previously showed a valid pass path with about 162 or 277 calls, which suggests L3-20 is solvable but unstable under long-context full-batch execution.
- Do not combine old intermediate results across different code versions when making final claims.

## 12. Known Remaining Problems

Highest priority: controller context management.

L3-16 and L3-20 show that the agent loop can accumulate too many raw observations, tool calls, and repeated reflections. The next controller iteration should add:

- structured stage summaries;
- rolling history token budgets;
- compaction of old tool traces;
- preservation of only system prompt, current objective, uncompleted commitments, current stage, recent raw messages, and latest room/device/sensor state;
- duplicate observation/action suppression;
- context-budget preflight before each LLM call;
- possibly a batched transaction tool for observe plus action plus power verification plus safe time advance.

Second priority: proactive air-quality strategy.

L3-10 showed that the agent can notice high CO2 too late and then understate the severity in natural-language reporting. The controller or prompt should make high-occupancy CO2 control more proactive:

- forecast CO2 risk from occupancy and meeting duration;
- increase ventilation before peak load;
- keep aggressive ventilation/HVAC active long enough;
- perform peak-aware final self-checks;
- treat high CO2 as a failed invariant, not just a mild comfort note.

Third priority: evaluation hardening.

The scenario suite should not rely only on one final boolean. Add a Metric Registry that maps each scenario's `primary_metrics` to automatic checks and produces comparable reports:

- Oracle baseline;
- do-nothing baseline;
- intentionally wrong policy baseline;
- live model policy;
- multi-seed success rate;
- scenario-specific metric table.

Fourth priority: physical calibration and real data integration.

Once MQTT shadow data exists, calibrate:

- thermal capacitance;
- envelope loss;
- infiltration/ventilation rate;
- HVAC heating/cooling strength;
- humidification/dehumidification behavior;
- purifier and PM2.5 decay;
- CO2 generation and removal;
- heat recovery parameters.

Fifth priority: derive L2/L1.

After L3 traces stabilize, extract reusable L2 and L1 tasks from actual failure and success traces. This should produce a more faithful hierarchy than inventing all atomic tasks first.

## 13. Useful Commands

Environment setup:

```bash
conda activate are
python -m pip install -r requirements-building.txt
```

Local building tests:

```bash
PYTHONPATH=. pytest -q tests/test_building_*.py
```

Export L3 workflows for manual review:

```bash
conda run -n are env PYTHONPATH=. python -m smartoffice.scenarios.building_kechuang.export_l3_workflows
```

Live model preflight:

```bash
conda run -n are env PYTHONPATH=. python scripts/building_l3_live_runner.py \
  --preflight-only \
  --endpoint http://100.115.106.71:8000/v1 \
  --model Qwen3.6-35B-A3B-FP8
```

Live model full run:

```bash
conda run -n are env PYTHONPATH=. python scripts/building_l3_live_runner.py \
  --stage all \
  --endpoint http://100.115.106.71:8000/v1 \
  --model Qwen3.6-35B-A3B-FP8 \
  --output-root runs/building_qwen36_35b_l3_all_<date_or_tag>
```

MQTT simulator smoke run:

```bash
python -m smartoffice.simulators.building --broker localhost --accelerated --steps 10
```

## 14. Documentation And Artifacts

Important documentation:

| File | Purpose |
|---|---|
| `smartoffice/apps/building_world/README.md` | BuildingWorld app/tool architecture |
| `smartoffice/scenarios/building_kechuang/README.md` | Kechuang scenario entry point |
| `smartoffice/scenarios/building_kechuang/scenarios_scripts.md` | scenario description template and drafting ideas |
| `smartoffice/scenarios/building_kechuang/l3_scenario_expansion_execution_plan.md` | L3 expansion plan and guiding philosophy |
| `smartoffice/scenarios/building_kechuang/l3_implementation_audit.md` | implementation audit notes |
| `smartoffice/scenarios/building_kechuang/l3_live_model_audit_20260825.md` | live model audit record |
| `smartoffice/scenarios/building_kechuang/experiment_status.md` | experiment status and run summaries |
| `smartoffice/scenarios/building_kechuang/live_model_experiment_guide.md` | live test guide |
| `smartoffice/configs/rooms/k1324_sensor_selection.md` | sensor selection notes |
| `smartoffice/simulators/building/README.md` | MQTT simulator notes |
| `workflow_exports/building_kechuang_l3_review/README.md` | exported workflow review index |

Important code areas:

| Path | Purpose |
|---|---|
| `smartoffice/apps/building_world/` | building app and callable tools |
| `smartoffice/physics/building/` | physics, equipment effects, observations, sensor hub |
| `smartoffice/adapters/building/` | real sensor adapter interfaces |
| `smartoffice/simulators/building/` | MQTT sensor simulator |
| `smartoffice/scenarios/building_kechuang/` | room scenarios and L3 suite |
| `scripts/building_l3_live_runner.py` | live model runner |
| `tests/test_building_*.py` | building regression tests |

## 15. Git And Reproducibility Notes

At the time this memory note was written, the current HEAD was:

```text
75763e3 规划智慧建筑L3场景扩展
```

The worktree already contained many modified and untracked building files from the implementation and testing process. Do not assume all current work has been committed. Before any future large edit, inspect:

```bash
git status --short
```

Recommended next git practice:

- create a boundary commit after reviewing the current building changes;
- keep generated workflow exports and run folders out of normal source commits unless intentionally archiving an experiment;
- commit controller/context fixes separately from scenario-spec changes;
- commit model-run reports separately from source behavior changes.

## 16. Immediate Next Work Plan

The next work should focus on reliability rather than adding more L3 scenarios.

Recommended order:

1. Add controller context compaction and repeated-action suppression.
2. Retest L3-16 and L3-20 with the same Qwen endpoint.
3. Add proactive CO2 strategy guidance and/or controller rule support.
4. Retest L3-10.
5. Run all 20 scenarios again with at least one full batch.
6. Add Metric Registry and baselines.
7. Start MQTT shadow deployment with real K1315/K1316/K1324 sensors.
8. Calibrate physics from shadow-mode data.
9. Derive L2/L1 tasks from stable L3 traces.

## 17. One-Paragraph Recall

The Kechuang building branch is now a three-room smart-building benchmark with K1324 office, K1316 seminar room, and K1315 conference room. It contains a BuildingWorld app/tool layer, a runtime-backed indoor physics engine, sensor adapters, an MQTT simulator, meeting/printing/device tools, and 20 declaration-driven L3 scenarios sharing a standardized whole-day workflow. Offline Oracle replay passes all 20. The latest recorded Qwen3.6-35B live full run passed 17/20: L3-10 exposed late CO2 control, while L3-16 and L3-20 exposed controller context overflow. The next serious work is controller memory/compaction, proactive CO2 strategy, richer automatic metrics, and real MQTT shadow calibration.
