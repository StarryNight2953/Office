from datetime import datetime

from fairy.apps.system import SystemApp


def test_system_time_exposes_explicit_local_and_utc_values() -> None:
    app = SystemApp()
    timestamp = datetime.fromisoformat("2026-09-10T09:00:00+08:00").timestamp()
    app.time_manager.reset(timestamp)

    current = app.get_current_time()
    advanced = app.advance_time(minutes=15)

    assert current["current_datetime"] == "2026-09-10 01:00:00"
    assert current["current_datetime_local"] == "2026-09-10T09:00:00+08:00"
    assert advanced["current_datetime"] == "2026-09-10 01:15:00"


def test_fair_live_clock_rejects_unguarded_event_bypass() -> None:
    app = SystemApp()
    app.time_manager.reset(datetime.fromisoformat("2026-09-10T09:00:00+08:00").timestamp())
    app.require_guarded_time_advance = True
    before = app.time_manager.offset

    rejected = app.advance_time(minutes=5, stop_on_event=False)

    assert rejected["reason"] == "unguarded_time_advance_forbidden"
    assert app.time_manager.offset == before
    assert app.advance_time(minutes=5, stop_on_event=True)["status"] == "ok"
