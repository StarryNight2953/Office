# Kechuang Building Scenario Test Guide

Last updated: 2026-09-02

This guide explains how to run and inspect the Kechuang smart-building scenarios. It covers three levels of testing:

1. local regression tests with `pytest`;
2. deterministic workflow/Oracle review;
3. live model tests through the FAIRY CLI and vLLM.

## 1. Environment

Recommended conda environment:

```bash
conda activate are
```

Install or refresh building dependencies:

```bash
python -m pip install -r requirements-building.txt
```

Most commands should be run from the repository root:

```bash
cd /Users/ouyangkun/PHD/Project/Office
```

When invoking Python modules directly, set `PYTHONPATH` to the repository root:

```bash
export PYTHONPATH=.
```

If using `conda run`, prefer:

```bash
conda run -n are env PYTHONPATH=. <command>
```

## 2. What Can Be Tested

The current Kechuang building package contains:

- basic room scenarios;
- K1324 office scenarios;
- K1316 seminar scenarios;
- K1315 conference scenarios;
- 20 full L3 scenarios under `fairy/scenarios/building_kechuang/l3/`.

Important scenario files:

| File | Purpose |
|---|---|
| `fairy/scenarios/building_kechuang/README.md` | Scenario overview |
| `fairy/scenarios/building_kechuang/l3/catalog.py` | 20 L3 scenario specs |
| `fairy/scenarios/building_kechuang/l3/scenarios.py` | Registered L3 scenario classes |
| `fairy/scenarios/building_kechuang/l3/base.py` | Shared L3 workflow and validation |
| `fairy/scenarios/building_kechuang/export_l3_workflows.py` | Export workflow review files |
| `scripts/building_l3_live_runner.py` | Batch runner for live model tests |

## 3. Fast Local Regression Tests

Run all building-related tests:

```bash
PYTHONPATH=. pytest -q tests/test_building_*.py
```

If using conda without activating the shell:

```bash
conda run -n are env PYTHONPATH=. pytest -q tests/test_building_*.py
```

Run only L3 scenario tests:

```bash
conda run -n are env PYTHONPATH=. pytest -q tests/test_building_l3_scenarios.py
```

Run workflow export tests:

```bash
conda run -n are env PYTHONPATH=. pytest -q tests/test_building_l3_workflow_export.py
```

Run live-runner unit tests without contacting a model service:

```bash
conda run -n are env PYTHONPATH=. pytest -q tests/test_building_l3_live_runner.py
```

Expected meaning:

- These tests validate code contracts, deterministic scenario construction, Oracle replay, workflow export behavior, and helper logic.
- They do not prove that a real LLM can solve the scenarios.

## 4. Export L3 Workflows For Manual Review

Export all 20 L3 workflow descriptions:

```bash
conda run -n are env PYTHONPATH=. python -m fairy.scenarios.building_kechuang.export_l3_workflows
```

Default output:

```text
workflow_exports/building_kechuang_l3_review/
```

Open the index:

```text
workflow_exports/building_kechuang_l3_review/README.md
```

The export normally includes:

- one Markdown file per scenario for human review;
- one JSON file per scenario for exact workflow details;
- a review index.

Use this when checking whether the scenario workflow itself is reasonable before spending time on live model inference.

## 5. Run One Scenario Through FAIRY CLI

Use this when you want to test a specific scenario with a real model or with the normal agent execution path.

General shape:

```bash
conda run -n are env PYTHONPATH=. python -m fairy.cli \
  --agent \
  --scenario <scenario_id> \
  --controller building_baseline_react \
  --provider vllm \
  --model <model_id> \
  --endpoint <endpoint> \
  --temperature 0.1 \
  --parallel-tool-calls false \
  --max-tool-calls 350 \
  --timeout-seconds 180 \
  --max-output-tokens 4096 \
  --output-dir runs/<run_name>/<scenario_id>
```

Example using the current known vLLM endpoint:

```bash
conda run -n are env PYTHONPATH=. python -m fairy.cli \
  --agent \
  --scenario scenario_building_kechuang_l3_18_printing_material_coordination \
  --controller building_baseline_react \
  --provider vllm \
  --model Qwen3.6-35B-A3B-FP8 \
  --endpoint http://100.115.106.71:8000/v1 \
  --temperature 0.1 \
  --parallel-tool-calls false \
  --max-tool-calls 350 \
  --timeout-seconds 180 \
  --max-output-tokens 4096 \
  --output-dir runs/building_l3_single_demo/scenario_building_kechuang_l3_18_printing_material_coordination
```

Notes:

- Replace the endpoint if the Tailscale IP changes.
- Replace the model name with the exact ID returned by `/v1/models`.
- `parallel-tool-calls` is currently set to `false` because the building controller expects sequential tool use.

## 6. Live Model Preflight

Before running any scenario, verify that the model service supports both model discovery and function calls:

```bash
conda run -n are env PYTHONPATH=. python scripts/building_l3_live_runner.py \
  --preflight-only \
  --endpoint http://100.115.106.71:8000/v1 \
  --model Qwen3.6-35B-A3B-FP8
```

If this fails:

- check whether the model server is running;
- check the Tailscale IP and port;
- check whether `/v1/models` returns the expected model ID;
- check whether the served model supports OpenAI-compatible function calling.

Direct endpoint check:

```bash
curl http://100.115.106.71:8000/v1/models
```

## 7. Batch Live Model Runs

The batch runner is the preferred way to run multiple L3 scenarios.

Smoke test:

```bash
conda run -n are env PYTHONPATH=. python scripts/building_l3_live_runner.py \
  --stage smoke \
  --endpoint http://100.115.106.71:8000/v1 \
  --model Qwen3.6-35B-A3B-FP8 \
  --output-root runs/building_qwen_l3_smoke
```

Diagnostic batch:

```bash
conda run -n are env PYTHONPATH=. python scripts/building_l3_live_runner.py \
  --stage diagnostic \
  --endpoint http://100.115.106.71:8000/v1 \
  --model Qwen3.6-35B-A3B-FP8 \
  --output-root runs/building_qwen_l3_diagnostic
```

Full 20-scenario run:

```bash
conda run -n are env PYTHONPATH=. python scripts/building_l3_live_runner.py \
  --stage all \
  --endpoint http://100.115.106.71:8000/v1 \
  --model Qwen3.6-35B-A3B-FP8 \
  --output-root runs/building_qwen_l3_all
```

Force rerun instead of reusing existing reports:

```bash
conda run -n are env PYTHONPATH=. python scripts/building_l3_live_runner.py \
  --stage all \
  --rerun \
  --endpoint http://100.115.106.71:8000/v1 \
  --model Qwen3.6-35B-A3B-FP8 \
  --output-root runs/building_qwen_l3_all
```

## 8. Runner Stages

Current stage meanings:

| Stage | Purpose |
|---|---|
| `smoke` | Run a small representative scenario first |
| `diagnostic` | Run selected scenarios that isolate common failure modes |
| `repair` | Regression anchors for repaired controller/scenario issues |
| `constraint_repair` | Focus on power/context-heavy cases |
| `environment_repair` | Focus on environment-control repairs |
| `final_repairs` | Focus on late repaired cells |
| `comfort_repair` | Focus on comfort repair case |
| `all` | Run all 20 L3 scenarios |

Recommended order:

1. `--preflight-only`
2. `--stage smoke`
3. `--stage diagnostic`
4. `--stage all`

## 9. Output Files

For a batch run, inspect files in this order:

| File | Meaning |
|---|---|
| `summary.md` | Best starting point: pass/fail, validation, calls, tokens, wall time |
| `summary.json` | Machine-readable aggregate result |
| `<scenario>/<scenario>.run_report.json` | Final scenario status and validation details |
| `<scenario>/<scenario>.progress.jsonl` | Runtime progress heartbeat |
| `<scenario>/<scenario>.runtime_log.jsonl` | Full LLM/tool event log |
| `<scenario>/<scenario>.agent_workflow.json` | Actual agent workflow trace |
| `<scenario>/stdout.log` | Process stdout |
| `<scenario>/stderr.log` | Process stderr and model/API errors |

Example:

```bash
sed -n '1,120p' runs/building_qwen_l3_all/summary.md
```

## 10. How To Interpret Results

A scenario is successful only when both conditions hold:

- the agent run completes normally;
- scenario validation returns true.

Common categories:

| Result pattern | Interpretation |
|---|---|
| completed + validation true | Scenario passed |
| completed + validation false | Agent finished, but final hard state failed |
| failed + API/context error | Likely model service or controller context issue |
| failed + max tool calls | Agent loop did not converge within budget |
| failed + tool schema error | Model produced invalid tool arguments or tool schema is unclear |
| failed + timeout | Model/server latency or agent loop is too slow |

For analysis, separate problems into two classes:

| Class | Meaning | Typical fix |
|---|---|---|
| Scenario/code issue | Tool interface, validation, physics, event timing, or scenario spec is wrong or unfair | Fix code/spec and rerun Oracle/local tests |
| Model/controller issue | Scenario is valid, but model policy, planning, context handling, or tool-use ability fails | Improve prompt/controller/context strategy or use stronger model |

## 11. Known Baseline Result

Latest recorded full live-model batch:

```text
runs/building_qwen36_35b_l3_all_final_20260828/summary.md
```

Recorded result:

```text
17 / 20 passed
```

Important interpretation:

- L3-10 exposed late high-occupancy CO2 control.
- L3-16 and L3-20 were mainly cut off by controller context overflow.
- This should not be summarized as "the model can only solve 17 scenarios"; two failures are currently controller/runtime scalability problems.

## 12. Recommended Demo Scenario

For a short demonstration, use:

```text
scenario_building_kechuang_l3_18_printing_material_coordination
```

Reasons:

- it covers meeting preparation, printing, deadline, equipment, environment control, and final validation;
- it is easier to explain than the most complex multi-commitment cases;
- it has passed in the recorded live-model run.

For a stress demonstration, use:

```text
scenario_building_kechuang_l3_10_three_room_high_occupancy
```

This is useful for showing why proactive CO2 control and final self-checking matter.

## 13. Common Troubleshooting

Model name mismatch:

```text
requested model is not served
```

Fix:

```bash
curl http://100.115.106.71:8000/v1/models
```

Then rerun with the exact returned model ID.

Network timeout:

- confirm Tailscale connection;
- confirm model host IP and port;
- confirm vLLM is listening on `0.0.0.0:8000` or the intended interface;
- if pushing to GitHub or downloading dependencies, use the local proxy if needed.

Context overflow:

- reduce stage size;
- run the scenario individually;
- lower repeated observations if controller supports it;
- prioritize controller context compaction work.

Max tool calls:

- increase `--max-tool-calls` only for diagnosis;
- inspect repeated actions in `runtime_log.jsonl`;
- check whether the agent is stuck in an observe-act loop.

Validation false:

- open the scenario `run_report.json`;
- inspect final validation reasons;
- compare with `runtime_log.jsonl`;
- classify as scenario/code issue or model/controller issue before changing anything.

## 14. Minimal Checklist Before A Serious Run

Before running expensive live tests:

- pull or confirm the intended git commit;
- run `PYTHONPATH=. pytest -q tests/test_building_*.py`;
- export workflows if scenario specs changed;
- run live model preflight;
- run smoke;
- run diagnostic;
- only then run all 20 scenarios;
- archive the output root name with date, model, endpoint, and code commit.

Suggested output naming:

```text
runs/building_<model_short_name>_l3_<stage>_<yyyymmdd>/
```

Example:

```text
runs/building_qwen36_35b_l3_all_20260902/
```
