"""Local HTTP/WebSocket boundary; the Runner alone owns mutable FAIRY state."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from fairy.visualization.models import AdvanceTime, AgentRequest, CreateRun, DeviceCommand, Scene
from fairy.visualization.scenes import SceneStore, catalog

REPO_ROOT = Path(__file__).resolve().parents[2]


def create_app(data_dir: str | Path | None = None, *, runner_factory=None) -> FastAPI:
    store = SceneStore(data_dir or os.getenv("OFFICE_SCENE_DIR", str(REPO_ROOT / ".office" / "scenes")))

    @asynccontextmanager
    async def lifespan(application):
        yield
        if application.state.runner is not None:
            await asyncio.to_thread(application.state.runner.close)

    app = FastAPI(title="Office Scene Lab", version="0.1.0", lifespan=lifespan)
    app.state.runner = None
    app.state.store = store
    app.state.lifecycle_lock = asyncio.Lock()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type"],
    )

    @app.exception_handler(ValueError)
    async def value_error(_request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(KeyError)
    async def unknown_resource(_request, exc):
        return JSONResponse(status_code=404, content={"detail": f"Unknown resource: {exc.args[0]}"})

    def current(run_id: str):
        runner = app.state.runner
        if runner is None or runner.run_id != run_id:
            raise HTTPException(404, "This experiment is no longer active; create or reconnect to the current run.")
        return runner

    async def call(method, *args):
        try:
            return await asyncio.to_thread(method, *args)
        except TimeoutError as exc:
            raise HTTPException(409, "The experiment is busy; wait for the current tool boundary.") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    async def replace_run(scene_id: str):
        # One active experiment per local service. Validate the saved layout first.
        async with app.state.lifecycle_lock:
            scene = store.load(scene_id)
            factory = runner_factory
            if factory is None:
                from fairy.visualization.runner import Runner
                factory = Runner
            old = app.state.runner
            if old is not None:
                await call(old.close)
                app.state.runner = None
            new = await call(factory, scene.model_dump(mode="json"))
            app.state.runner = new
            return new.snapshot()

    @app.get("/api/health")
    def health():
        return {"status": "ok", "service": "office-scene-lab"}

    @app.get("/api/catalog")
    def get_catalog():
        return catalog()

    @app.get("/api/scenes/{room_id}")
    def get_scene(room_id: str):
        return store.load(room_id)

    @app.put("/api/scenes/{room_id}")
    def save_scene(room_id: str, scene: Scene):
        return store.save(room_id, scene)

    @app.post("/api/runs")
    async def create_run(request: CreateRun):
        return await replace_run(request.scene_id)

    @app.get("/api/runs/current")
    def get_current():
        return app.state.runner.snapshot() if app.state.runner is not None else None

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        return current(run_id).snapshot()

    @app.post("/api/runs/{run_id}/commands")
    async def command(run_id: str, request: DeviceCommand):
        return await call(current(run_id).command, request.device_id, request.parameters)

    @app.post("/api/runs/{run_id}/advance")
    async def advance(run_id: str, request: AdvanceTime):
        return await call(current(run_id).advance, request.seconds)

    @app.post("/api/runs/{run_id}/agent")
    async def agent(run_id: str, request: AgentRequest):
        return await call(current(run_id).start_agent, request.mode, request.task)

    @app.post("/api/runs/{run_id}/stop")
    async def stop(run_id: str):
        return await call(current(run_id).stop_agent)

    @app.post("/api/runs/{run_id}/reset")
    async def reset(run_id: str):
        scene_id = current(run_id).snapshot()["scene_id"]
        return await replace_run(scene_id)

    @app.websocket("/api/runs/{run_id}/events")
    async def events(websocket: WebSocket, run_id: str):
        runner = app.state.runner
        if runner is None or runner.run_id != run_id:
            await websocket.close(code=1008, reason="Experiment is no longer active")
            return
        await websocket.accept()
        last_revision = -1
        try:
            while app.state.runner is runner:
                snapshot = runner.snapshot()
                if snapshot["revision"] != last_revision:
                    await websocket.send_json({"type": "snapshot", "data": snapshot})
                    last_revision = snapshot["revision"]
                # Observe disconnects even when the experiment is idle. Only the
                # immutable display cache is sampled; this never advances physics.
                try:
                    await asyncio.wait_for(websocket.receive_text(), timeout=0.2)
                except asyncio.TimeoutError:
                    pass
            await websocket.close(code=1000, reason="Experiment replaced")
        except (WebSocketDisconnect, RuntimeError):
            return

    dist = REPO_ROOT / "web" / "dist"
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
    model_dir = dist / "models" if (dist / "models").is_dir() else REPO_ROOT / "web" / "public" / "models"
    if model_dir.is_dir():
        app.mount("/models", StaticFiles(directory=model_dir), name="models")

    @app.get("/", include_in_schema=False)
    def index():
        if (dist / "index.html").is_file():
            return FileResponse(dist / "index.html")
        return JSONResponse(status_code=503, content={
            "detail": "Frontend has not been built. Run npm install and npm run build in web/, then restart this service. For development run npm run dev and open http://localhost:5173.",
            "api_docs": "/docs",
        })

    return app


app = create_app()
