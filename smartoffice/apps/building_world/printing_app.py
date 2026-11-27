"""Deterministic printer controls and time-aware meeting print jobs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Annotated, Any, Literal

from smartoffice.apps.app import App
from smartoffice.apps.building_world.building_world_app import BuildingWorldApp
from smartoffice.apps.building_world.types import BuildingEventType, DeviceHealth, DeviceType
from smartoffice.tool_utils import OperationType, app_tool, data_tool
from smartoffice.types import event_registered
from smartoffice.utils.type_utils import type_check

if TYPE_CHECKING:
    from smartoffice.apps.building_world.runtime import BuildingWorldRuntime
    from smartoffice.controllers.building.event_queue import ScheduledBuildingEvent


class PrintingApp(App):
    """Own print-job workflow while World remains the device-state authority."""

    def __init__(
        self,
        world: BuildingWorldApp,
        runtime: BuildingWorldRuntime | None = None,
    ) -> None:
        super().__init__(name="PrintingApp")
        self.world = world
        self.runtime = runtime
        self.jobs: dict[str, dict[str, Any]] = {}
        # Requests are public business requirements.  Scenarios may publish a
        # request at briefing time or only when a later user change occurs;
        # the Agent never needs privileged access to the scenario Spec.
        self.requests: dict[str, dict[str, Any]] = {}
        if runtime is not None:
            runtime.register_event_handler(
                BuildingEventType.PRINT_JOB_COMPLETED,
                self._complete_scheduled_job,
            )

    def reset(self) -> None:
        super().reset()
        self.jobs = {}
        self.requests = {}

    def add_print_request(
        self,
        *,
        request_id: str,
        document_name: str,
        copies: int,
        pages_per_copy: int,
        requested_by: str,
        submit_at: datetime,
        ready_by: datetime,
        priority: int = 1,
    ) -> None:
        """Publish one scenario/user print requirement to the Agent-facing App."""

        if request_id in self.requests:
            return
        if copies <= 0 or pages_per_copy <= 0 or not 1 <= priority <= 3:
            raise ValueError("invalid print request")
        if submit_at.tzinfo is None or ready_by.tzinfo is None:
            raise ValueError("print request timestamps must be timezone-aware")
        if submit_at >= ready_by:
            raise ValueError("print request submit_at must precede ready_by")
        self.requests[request_id] = {
            "request_id": request_id,
            "document_name": document_name,
            "copies": copies,
            "pages_per_copy": pages_per_copy,
            "requested_by": requested_by,
            "priority": priority,
            "submit_at": submit_at.isoformat(),
            "ready_by": ready_by.isoformat(),
            "status": "pending",
            "job_id": None,
        }

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_print_requests(
        self, status: Literal["pending", "submitted", "completed", "all"] = "pending"
    ) -> dict[str, Any]:
        """List public print requirements, including exact quantities and deadlines."""

        requests = [
            dict(item)
            for item in self.requests.values()
            if status == "all" or item["status"] == status
        ]
        return {"status": "ok", "requests": requests, "count": len(requests)}

    @type_check
    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def set_printer_power(self, device_id: str, power_on: bool) -> dict[str, Any]:
        spec = self.world.devices.get(device_id)
        state = self.world.device_states.get(device_id)
        if spec is None or state is None:
            return _rejected("unknown_device", device_id=device_id)
        if spec.device_type != DeviceType.PRINTER:
            return _rejected("wrong_device_type", device_id=device_id)
        if state.health != DeviceHealth.ONLINE:
            return _rejected("device_unavailable", health=state.health.value)
        state.power_on = power_on
        state.mode = "ready" if power_on else "off"
        state.level = 1 if power_on else 0
        event = self.world.publish_building_event(
            BuildingEventType.DEVICE_STATE_CHANGED,
            source=self.name,
            subject_id=device_id,
            payload={"power_on": power_on},
        )
        return {
            "status": "accepted",
            "device_id": device_id,
            "event_id": event.event_id,
        }

    @type_check
    @app_tool()
    @event_registered(operation_type=OperationType.WRITE)
    def submit_print_job(
        self,
        printer_id: str,
        document_name: str,
        copies: Annotated[int, {"minimum": 1}],
        pages_per_copy: Annotated[int, {"minimum": 1}],
        requested_by: str,
        priority: Literal[1, 2, 3] = 1,
    ) -> dict[str, Any]:
        spec = self.world.devices.get(printer_id)
        state = self.world.device_states.get(printer_id)
        if spec is None or state is None:
            return _rejected("unknown_device", device_id=printer_id)
        if spec.device_type != DeviceType.PRINTER:
            return _rejected("wrong_device_type", device_id=printer_id)
        if state.health != DeviceHealth.ONLINE or not state.power_on:
            return _rejected("printer_not_ready", device_id=printer_id)
        if copies <= 0 or pages_per_copy <= 0:
            return _rejected("invalid_page_count")
        if not 1 <= priority <= 3:
            return _rejected("invalid_priority")

        now = self._now()
        matching_requests = [
            request
            for request in self.requests.values()
            if request["status"] == "pending"
            and request["document_name"] == document_name
            and request["copies"] == copies
            and request["pages_per_copy"] == pages_per_copy
            and request["priority"] == priority
        ]
        request = matching_requests[0] if len(matching_requests) == 1 else None
        pages = copies * pages_per_copy
        pages_per_minute = float(spec.parameters.get("pages_per_minute", 20.0))
        duration_seconds = max(1.0, pages / pages_per_minute * 60.0)
        # One physical printer cannot process jobs concurrently. Jobs already
        # accepted retain their slot; callers submit same-time jobs in priority
        # order to obtain deterministic, replay-stable scheduling.
        available_at_timestamp = max(
            (
                float(job["ready_at_timestamp"])
                for job in self.jobs.values()
                if job["printer_id"] == printer_id
            ),
            default=now.timestamp(),
        )
        started_at_timestamp = max(now.timestamp(), available_at_timestamp)
        job_id = self.world.next_id("print-job")
        self.jobs[job_id] = {
            "job_id": job_id,
            "printer_id": printer_id,
            "document_name": document_name,
            "copies": copies,
            "pages_per_copy": pages_per_copy,
            "requested_by": requested_by,
            "priority": priority,
            "request_id": request["request_id"] if request is not None else None,
            "status": (
                "printing"
                if started_at_timestamp <= now.timestamp()
                else "queued"
            ),
            "submitted_at": now.isoformat(),
            "started_at_timestamp": started_at_timestamp,
            "ready_at_timestamp": started_at_timestamp + duration_seconds,
        }
        if self.runtime is not None:
            self.runtime.schedule_event(
                scheduled_id=f"complete-{job_id}",
                execute_at=datetime.fromtimestamp(
                    started_at_timestamp + duration_seconds,
                    tz=timezone.utc,
                ),
                event_type=BuildingEventType.PRINT_JOB_COMPLETED,
                source=self.name,
                subject_id=job_id,
                payload={"printer_id": printer_id},
            )
        if request is not None:
            request["status"] = "submitted"
            request["job_id"] = job_id
        event = self.world.publish_building_event(
            BuildingEventType.PRINT_JOB_SUBMITTED,
            source=self.name,
            subject_id=job_id,
            payload={"printer_id": printer_id, "pages": pages},
        )
        return {
            "status": "accepted",
            "job_id": job_id,
            "event_id": event.event_id,
            "estimated_duration_seconds": duration_seconds,
            "queued_seconds": started_at_timestamp - now.timestamp(),
        }

    @type_check
    @app_tool()
    @data_tool()
    @event_registered(operation_type=OperationType.READ)
    def get_print_job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            return _rejected("unknown_print_job", job_id=job_id)
        now_timestamp = self._now().timestamp()
        if job["status"] == "queued" and now_timestamp >= float(
            job["started_at_timestamp"]
        ):
            job["status"] = "printing"
        if job["status"] in {"queued", "printing"} and now_timestamp >= float(
            job["ready_at_timestamp"]
        ):
            self._complete_job(job_id, publish_event=self.runtime is None)
        return {
            key: value
            for key, value in job.items()
            if key not in {"started_at_timestamp", "ready_at_timestamp"}
        }

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.time_manager.time(), tz=timezone.utc)

    def _complete_scheduled_job(
        self,
        scheduled: ScheduledBuildingEvent,
        _world: BuildingWorldApp,
    ) -> None:
        """Apply completion before Runtime publishes the scheduled fact."""

        self._complete_job(scheduled.subject_id, publish_event=False)

    def _complete_job(self, job_id: str, *, publish_event: bool) -> None:
        job = self.jobs.get(job_id)
        if job is None or job["status"] == "completed":
            return
        job["status"] = "completed"
        request_id = job.get("request_id")
        if request_id in self.requests:
            self.requests[request_id]["status"] = "completed"
        if publish_event:
            self.world.publish_building_event(
                BuildingEventType.PRINT_JOB_COMPLETED,
                source=self.name,
                subject_id=job_id,
                payload={"printer_id": job["printer_id"]},
            )


def _rejected(reason: str, **details: Any) -> dict[str, Any]:
    return {"status": "rejected", "reason": reason, **details}
