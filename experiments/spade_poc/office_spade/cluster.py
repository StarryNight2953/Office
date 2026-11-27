"""Own and clean up a local XMPP service and three separate Agent processes."""

import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from .core import ROOMS

PROJECT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class LocalCluster:
    def __init__(self, directory: Path, *, building=False, model_environment=None):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=False)
        self.port, self.s2s_port = free_port(), free_port()
        while self.s2s_port == self.port:
            self.s2s_port = free_port()
        self.processes = {}
        self.files = []
        self.responses = queue.Queue()
        self.reader = None
        self.environment = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
            "OFFICE_SPADE_PASSWORD": secrets.token_urlsafe(24),
        }
        self.starts = []
        self.identities = {}
        self.building = building
        self.world_port = free_port()
        self.world_tokens = {room: secrets.token_urlsafe(24) for room in ROOMS}
        self.world_admin = secrets.token_urlsafe(24)
        if model_environment:
            self.environment.update(model_environment)
        temporary = self.directory / "tmp"
        temporary.mkdir()
        self.environment.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary))

    def start_process(self, role, room=None):
        key = room.lower() if room else role
        for suffix in (".stop", ".ready.json"):
            (self.directory / f"{key}{suffix}").unlink(missing_ok=True)
        output = (self.directory / f"{key}.console.log").open("a", encoding="utf-8")
        self.files.append(output)
        command = [
            sys.executable,
            "-u",
            "-m",
            "office_spade",
            role,
            "--run-dir",
            str(self.directory),
            "--port",
            str(self.world_port if role == "building" else self.port),
        ]
        environment = dict(self.environment)
        if self.building and role == "building":
            environment.update(
                OFFICE_BUILDING_TOKENS=json.dumps(self.world_tokens),
                OFFICE_BUILDING_ADMIN_TOKEN=self.world_admin,
            )
        elif self.building and room:
            environment.update(
                OFFICE_ROOM_BACKEND="building",
                OFFICE_BUILDING_URL=f"http://127.0.0.1:{self.world_port}",
                OFFICE_BUILDING_TOKEN=self.world_tokens[room],
            )
        if room:
            command.extend(["--room", room])
        if role == "xmpp":
            command.extend(["--s2s-port", str(self.s2s_port)])
        process = subprocess.Popen(
            command,
            cwd=PROJECT,
            env=environment,
            stdin=subprocess.PIPE if role == "coordinator" else subprocess.DEVNULL,
            stdout=subprocess.PIPE if role == "coordinator" else output,
            stderr=output,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.processes[key] = process
        identity = {"role": key, "launcher_pid": process.pid}
        self.starts.append(identity)
        deadline = time.monotonic() + 25
        ready = self.directory / f"{key}.ready.json"
        while not ready.exists():
            if process.poll() is not None:
                raise RuntimeError(f"{key} exited ({process.returncode}); see {output.name}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{key} startup timed out; see {output.name}")
            time.sleep(0.05)
        self.identities[key] = json.loads(ready.read_text(encoding="utf-8"))
        identity["process_pid"] = self.identities[key]["pid"]
        if role == "coordinator":
            self.reader = threading.Thread(target=self._read_responses, daemon=True)
            self.reader.start()

    def _read_responses(self):
        for line in self.processes["coordinator"].stdout:
            try:
                self.responses.put(json.loads(line))
            except json.JSONDecodeError:
                self.responses.put({"ok": False, "error": f"unexpected stdout: {line.strip()}"})
        self.responses.put({"ok": False, "error": "coordinator stdout closed"})

    def __enter__(self):
        try:
            if self.building:
                self.start_process("building")
            self.start_process("xmpp")
            for room in ROOMS:
                self.start_process("room", room)
            self.start_process("coordinator")
            return self
        except BaseException:
            self.close()
            raise

    def call(self, command, **data):
        request_id = uuid4().hex
        process = self.processes["coordinator"]
        process.stdin.write(
            json.dumps({"request_id": request_id, "command": command, **data}) + "\n"
        )
        process.stdin.flush()
        try:
            response = self.responses.get(timeout=190)
        except queue.Empty as exc:
            raise TimeoutError("coordinator control response timed out; inspect its logs") from exc
        if not response.get("ok") or response.get("request_id") != request_id:
            raise RuntimeError(response)
        return response["data"]

    def tasks(self, *requests):
        return self.call("tasks", tasks=list(requests))

    def stop_process(self, key):
        process = self.processes.get(key)
        if process is None:
            return
        (self.directory / f"{key}.stop").touch()
        if key == "coordinator" and process.stdin and not process.stdin.closed:
            process.stdin.close()  # Unblock readline so executor shutdown can finish.
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                # A Windows venv launcher can own a separate interpreter process.
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    check=False,
                )
            else:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if process.stdout:
            if self.reader and key == "coordinator":
                self.reader.join(timeout=2)
            process.stdout.close()

    def close(self):
        for key in ("coordinator", "k1316", "k1315", "xmpp", "building"):
            self.stop_process(key)
        for stream in self.files:
            stream.close()

    def __exit__(self, *_):
        self.close()


def request(room, operation="get_state", **options):
    payload = {"operation": operation, **options.pop("payload", {})}
    return {"room_id": room, "payload": payload, **options}
