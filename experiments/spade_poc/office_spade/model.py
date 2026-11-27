"""Bounded SPADE-LLM tool loop with canonical postcondition verification."""

import asyncio
import json
import logging
import math
import os
import time
from urllib.parse import urlsplit


def model_configuration():
    model = os.getenv("OFFICE_LLM_MODEL") or os.getenv("VLLM_MODEL") or ""
    url = os.getenv("OFFICE_LLM_BASE_URL") or os.getenv("VLLM_API_BASE") or ""
    key = os.getenv("OFFICE_LLM_API_KEY") or os.getenv("VLLM_API_KEY") or "EMPTY"
    parsed = urlsplit(url)
    configured = bool(model.strip() and parsed.scheme in {"http", "https"} and parsed.hostname)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        configured = False
    return {
        "configured": configured,
        "model": model.strip(),
        "base_url": url.rstrip("/"),
        "api_key": key,
    }


def public_readiness():
    configuration = model_configuration()
    return {
        "configured": configuration["configured"],
        "model": configuration["model"] or None,
        "reason": None
        if configuration["configured"]
        else "Set OFFICE_LLM_MODEL and OFFICE_LLM_BASE_URL (OpenAI-compatible /v1 endpoint).",
    }


def validate_expectations(expectations, state):
    devices = {item["device_id"] for item in state["context"]["devices"]}
    allowed = {"device_id", "power_on", "mode", "target_temperature_c", "level", "brightness_pct"}
    if not isinstance(expectations, list) or not 1 <= len(expectations) <= 5:
        raise ValueError("llm_task requires 1 to 5 explicit device expectations")
    for expected in expectations:
        if (
            not isinstance(expected, dict)
            or not expected.keys() <= allowed
            or expected.get("device_id") not in devices
            or len(expected) < 2
        ):
            raise ValueError("invalid or foreign-room device expectation")
        for name, value in expected.items():
            if name == "power_on" and type(value) is not bool:
                raise ValueError("power_on expectation must be boolean")
            if name in {"device_id", "mode"} and not isinstance(value, str):
                raise ValueError("device_id and mode must be text")
            if name in {"target_temperature_c", "level", "brightness_pct"} and (
                type(value) not in {int, float} or not math.isfinite(value)
            ):
                raise ValueError("numeric expectations must be finite numbers")


def verify_expectations(expectations, state):
    devices = {item["device_id"]: item for item in state["context"]["devices"]}
    checks = []
    for expected in expectations:
        actual = devices.get(expected["device_id"], {})
        for name, target in expected.items():
            if name == "device_id":
                continue
            observed = (
                actual.get("settings", {}).get(name)
                if name == "brightness_pct"
                else actual.get(name)
            )
            equal = (
                (type(observed) is bool and observed is target)
                if type(target) is bool
                else (
                    abs(observed - target) < 1e-6
                    if type(target) in {int, float} and type(observed) in {int, float}
                    else observed == target
                )
            )
            checks.append(
                {
                    "device_id": expected["device_id"],
                    "field": name,
                    "expected": target,
                    "observed": observed,
                    "passed": bool(equal),
                }
            )
    return {"passed": bool(checks) and all(check["passed"] for check in checks), "checks": checks}


class ModelTaskRunner:
    def __init__(self, client, log):
        self.client, self.log = client, log

    async def run(self, task_id, task, expectations, initial):
        if not isinstance(task, str) or not 0 < len(task) <= 4000:
            raise ValueError("task must be text of 1 to 4000 characters")
        validate_expectations(expectations, initial)
        configuration = model_configuration()
        if not configuration["configured"]:
            raise ValueError(public_readiness()["reason"])
        # Disable optional provider telemetry / model-cost downloads in local runs.
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        os.environ.setdefault("LITELLM_TELEMETRY", "False")
        from spade_llm import ContextManager, LLMProvider, LLMTool
        from spade_llm.context import create_assistant_tool_call_message

        logging.getLogger("spade_llm.providers").setLevel(logging.CRITICAL)
        model = configuration["model"]
        provider = LLMProvider(
            model=model if model.startswith("openai/") else "openai/" + model,
            base_url=configuration["base_url"],
            api_key=configuration["api_key"],
            temperature=0.1,
            timeout=20,
            max_tokens=1200,
            num_retries=0,
        )
        context = ContextManager(
            max_tokens=16000,
            system_prompt=(
                f"You control only room {initial['room_id']} in a Building simulation. "
                "Use the supplied real tools and exact resource IDs. Never access another room. "
                "Device setpoints are not measured room temperatures. Do not advance world time. "
                "Perform the requested changes, read back device state, then give a short summary. "
                "Report rejections honestly; do not repeat an unchanged rejected command."
            ),
        )
        context.add_message_dict(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": task,
                        "required_device_postconditions": expectations,
                        "fresh_room_observation": initial,
                    }
                ),
            },
            task_id,
        )
        catalog = await asyncio.to_thread(self.client.catalog)
        history = []

        def make_tool(schema):
            async def execute(**arguments):
                result = await asyncio.to_thread(self.client.invoke, schema["name"], arguments)
                history.append({"name": schema["name"], "arguments": arguments, "result": result})
                self.log.write(
                    "llm_tool_finished",
                    task_id=task_id,
                    room_id=initial["room_id"],
                    tool=schema["name"],
                    arguments=arguments,
                    result=result,
                )
                return result

            return LLMTool(
                name=schema["name"],
                description=schema["description"],
                parameters=schema["parameters"],
                func=execute,
            )

        tools = [make_tool(schema) for schema in catalog]
        by_name = {tool.name: tool for tool in tools}
        started, llm_calls, tool_calls, summary = time.monotonic(), 0, 0, None
        failure = None
        try:
            async with asyncio.timeout(100):
                for _ in range(8):
                    llm_calls += 1
                    self.log.write(
                        "llm_call_started",
                        task_id=task_id,
                        room_id=initial["room_id"],
                        call_index=llm_calls,
                        model=model,
                    )
                    response = await provider.get_llm_response(context, tools, task_id)
                    calls = response.get("tool_calls", [])
                    self.log.write(
                        "llm_call_finished",
                        task_id=task_id,
                        room_id=initial["room_id"],
                        call_index=llm_calls,
                        requested_tools=len(calls),
                    )
                    if not calls:
                        summary = response.get("text") or ""
                        break
                    if tool_calls + len(calls) > 12:
                        raise ValueError("model tool budget exceeded")
                    context.add_message_dict(create_assistant_tool_call_message(calls), task_id)
                    for call in calls:
                        tool_calls += 1
                        tool = by_name.get(call.get("name"))
                        arguments = call.get("arguments")
                        if tool is None or not isinstance(arguments, dict):
                            raise ValueError("model requested an invalid tool")
                        try:
                            result = await tool.execute(**arguments)
                        except (ValueError, TypeError, ConnectionError) as exc:
                            result = {"status": "rejected", "reason": str(exc)}
                            history.append(
                                {"name": tool.name, "arguments": arguments, "result": result}
                            )
                        context.add_tool_result(tool.name, json.dumps(result), call["id"], task_id)
                if summary is None:
                    raise ValueError("model iteration budget exceeded")
        except Exception as exc:
            # Provider errors can contain sensitive response bodies. Keep only type.
            failure = type(exc).__name__
            self.log.write(
                "llm_failed", task_id=task_id, room_id=initial["room_id"], error_code=failure
            )
        final = await asyncio.to_thread(self.client.snapshot)
        verification = verify_expectations(expectations, final)
        completed = failure is None and verification["passed"] and bool(summary)
        return {
            "status": "completed" if completed else "failed",
            "state": final,
            "summary": (summary or "")[:2000],
            "verification": verification,
            "error_code": failure or (None if completed else "postcondition_unmet"),
            "llm_calls": llm_calls,
            "tool_calls": tool_calls,
            "token_usage": None,
            "tool_history": history,
            "model_elapsed_ms": round((time.monotonic() - started) * 1000, 2),
        }
