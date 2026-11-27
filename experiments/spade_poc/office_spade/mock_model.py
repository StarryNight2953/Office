"""Explicit scripted OpenAI-compatible model fixture. No language inference."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4


class MockModelServer:
    def __init__(self):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    if not 0 < length <= 300000:
                        raise ValueError("invalid request length")
                    payload = json.loads(self.rfile.read(length))
                    user = next(
                        message for message in payload["messages"] if message["role"] == "user"
                    )
                    objective = json.loads(user["content"])
                    state = objective["fresh_room_observation"]
                    task = objective["task"]
                    responses = [
                        message for message in payload["messages"] if message["role"] == "tool"
                    ]
                    owner.requests.append(
                        {
                            "room_id": state["room_id"],
                            "tool_results_seen": len(responses),
                            "model": payload["model"],
                            "case": "error"
                            if "MOCK_HTTP_ERROR" in task
                            else "claim_only"
                            if "MOCK_CLAIM_ONLY" in task
                            else "normal",
                        }
                    )
                    if "MOCK_HTTP_ERROR" in task:
                        self.send_json(
                            {
                                "error": {
                                    "message": "scripted upstream failure",
                                    "type": "server_error",
                                }
                            },
                            status=503,
                        )
                        return
                    device = next(
                        item
                        for item in state["context"]["devices"]
                        if item["device_type"] == "hvac"
                    )
                    expected = next(
                        item
                        for item in objective["required_device_postconditions"]
                        if item["device_id"] == device["device_id"]
                    )
                    tool = None
                    if "MOCK_CLAIM_ONLY" in task:
                        content = "Scripted fixture claims completion without executing tools."
                    elif len(responses) == 0:
                        tool = {
                            "name": "HvacApp__set_hvac",
                            "arguments": {
                                "device_id": device["device_id"],
                                "power_on": True,
                                "mode": "cooling",
                                "target_temperature_c": expected["target_temperature_c"],
                                "fan_level": 3,
                            },
                        }
                        content = None
                    elif len(responses) == 1:
                        tool = {
                            "name": "DeviceRegistryApp__get_device_state",
                            "arguments": {"device_id": device["device_id"]},
                        }
                        content = None
                    else:
                        content = "Scripted fixture executed HVAC command and read device state."
                    message = {"role": "assistant", "content": content}
                    if tool:
                        message["tool_calls"] = [
                            {
                                "id": "call_" + uuid4().hex,
                                "type": "function",
                                "function": {
                                    "name": tool["name"],
                                    "arguments": json.dumps(tool["arguments"]),
                                },
                            }
                        ]
                    self.send_json(
                        {
                            "id": "chatcmpl-" + uuid4().hex,
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": "offline-building-fixture",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": message,
                                    "finish_reason": "tool_calls" if tool else "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 0,
                                "completion_tokens": 0,
                                "total_tokens": 0,
                            },
                        }
                    )
                except (ValueError, KeyError, StopIteration, TypeError):
                    self.send_json({"error": {"message": "invalid fixture request"}}, status=400)

            def send_json(self, value, status=200):
                encoded = json.dumps(value, allow_nan=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
        )

    @property
    def environment(self):
        return {
            "OFFICE_LLM_MODEL": "offline-building-fixture",
            "OFFICE_LLM_BASE_URL": f"http://127.0.0.1:{self.server.server_port}/v1",
            "OFFICE_LLM_API_KEY": "EMPTY",
        }

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
