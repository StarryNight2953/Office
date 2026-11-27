"""The reviewed catalog of twenty non-anomalous Kechuang L3 scenarios."""

from __future__ import annotations

from smartoffice.apps.building_world.types import BuildingEventType
from smartoffice.scenarios.building_kechuang.l3.specs import (
    BuildingL3Spec,
    EvaluationSpec,
    InteractionPlan,
    MeetingPlan,
    OccupancyPhase,
    OutdoorPoint,
    OutdoorProfile,
    PrintBatch,
    day_start,
)


def _profile(
    profile_id: str,
    temperatures: tuple[float, float, float],
    humidities: tuple[float, float, float],
    *,
    pm25: tuple[float, float, float] = (18.0, 22.0, 16.0),
    solar: tuple[float, float, float] = (60.0, 600.0, 120.0),
) -> OutdoorProfile:
    return OutdoorProfile(
        profile_id,
        tuple(
            OutdoorPoint(minute, temperature, humidity, particles, irradiance)
            for minute, temperature, humidity, particles, irradiance in zip(
                (0, 300, 600), temperatures, humidities, pm25, solar
            )
        ),
    )


SUMMER = _profile("summer_hot_humid", (29.0, 36.0, 31.0), (78.0, 68.0, 76.0))
WINTER = _profile("winter_cold_dry", (-18.0, -10.0, -16.0), (22.0, 28.0, 24.0))
SHOULDER = _profile(
    "shoulder_mild", (17.0, 23.0, 19.0), (48.0, 44.0, 52.0), solar=(30, 350, 60)
)
SOLAR_PEAK = _profile(
    "solar_peak", (25.0, 34.0, 29.0), (55.0, 48.0, 58.0), solar=(40, 950, 100)
)
POLLUTED = _profile(
    "outdoor_pm25_high",
    (20.0, 25.0, 21.0),
    (52.0, 46.0, 55.0),
    pm25=(115.0, 165.0, 130.0),
)


def _phase(minute: int, episode: str, **counts: int) -> OccupancyPhase:
    return OccupancyPhase(minute, tuple(counts.items()), episode)


def _meeting(
    activity_id: str,
    room_id: str,
    start: int,
    end: int,
    attendees: int,
    title: str,
    capabilities: tuple[str, ...] = ("projector",),
) -> MeetingPlan:
    return MeetingPlan(activity_id, room_id, start, end, attendees, title, capabilities)


def _interaction(
    minute: int,
    subject: str,
    description: str,
    **payload: object,
) -> InteractionPlan:
    return InteractionPlan(
        minute,
        BuildingEventType.SCHEDULE_CHANGED,
        subject,
        description,
        tuple(payload.items()),
    )


def _print(
    document: str,
    copies: int,
    pages: int,
    submit: int,
    deadline: int,
    priority: int = 1,
    reveal: int = 0,
) -> PrintBatch:
    return PrintBatch(document, copies, pages, submit, deadline, priority, reveal)


def _mixed_phases() -> tuple[OccupancyPhase, ...]:
    return (
        _phase(30, "opening", k1324=4, k1316=0, k1315=0),
        _phase(120, "morning_activity", k1324=12, k1316=7, k1315=0),
        _phase(240, "midday_transition", k1324=5, k1316=0, k1315=0),
        _phase(330, "afternoon_activity", k1324=14, k1316=0, k1315=16),
        _phase(540, "closeout", k1324=0, k1316=0, k1315=0),
    )


def _parallel_phases() -> tuple[OccupancyPhase, ...]:
    return (
        _phase(30, "opening", k1324=5, k1316=0, k1315=0),
        _phase(120, "preparation", k1324=12, k1316=0, k1315=0),
        _phase(180, "parallel_operation", k1324=15, k1316=8, k1315=18),
        _phase(360, "transition", k1324=10, k1316=6, k1315=0),
        _phase(540, "closeout", k1324=0, k1316=0, k1315=0),
    )


def _spec(
    number: int,
    slug: str,
    title: str,
    family: str,
    question: str,
    *,
    month: int,
    day: int,
    outdoor: OutdoorProfile,
    phases: tuple[OccupancyPhase, ...],
    meetings: tuple[MeetingPlan, ...],
    mode: str,
    target: float,
    humidifier: bool = False,
    purifier: bool = False,
    presentation: bool = False,
    print_batches: tuple[PrintBatch, ...] = (),
    interaction: InteractionPlan | None = None,
    power_limit_w: float | None = None,
    metrics: tuple[str, ...] = (),
    max_unoccupied_energy_kwh: float | None = None,
    max_comfort_violation_ratio: float = 0.9,
    max_pm25_ug_m3: float = 50.0,
    initial_temperature_c: float | None = None,
    initial_humidity_pct: float | None = None,
    preparation_lead_minutes: int | None = None,
) -> BuildingL3Spec:
    if initial_temperature_c is None:
        initial_temperature_c = {
            "cooling": 27.0,
            "heating": 20.0,
            "fan": 22.0,
        }[mode]
    if initial_humidity_pct is None:
        initial_humidity_pct = 35.0 if mode == "heating" else 50.0
    if preparation_lead_minutes is None:
        preparation_lead_minutes = {
            "cooling": 60,
            "heating": 90,
            "fan": 30,
        }[mode]
    return BuildingL3Spec(
        number=number,
        slug=slug,
        title=title,
        family=family,
        research_question=question,
        start_at=day_start(month, day),
        duration_minutes=600,
        outdoor=outdoor,
        phases=phases,
        meetings=meetings,
        target_temperature_c=target,
        hvac_mode=mode,
        initial_indoor_temperature_c=initial_temperature_c,
        initial_indoor_humidity_pct=initial_humidity_pct,
        preparation_lead_minutes=preparation_lead_minutes,
        use_humidifier=humidifier,
        use_purifier=purifier,
        use_presentation=presentation,
        print_batches=print_batches,
        interaction=interaction,
        power_limit_w=power_limit_w,
        primary_metrics=metrics,
        evaluation=EvaluationSpec(
            max_unoccupied_energy_kwh=max_unoccupied_energy_kwh,
            max_occupied_comfort_violation_ratio=max_comfort_violation_ratio,
            max_peak_pm25_ug_m3=max_pm25_ug_m3,
        ),
    )


MIXED_MEETINGS = (
    _meeting("morning-seminar", "k1316", 120, 210, 7, "Morning seminar"),
    _meeting(
        "afternoon-conference",
        "k1315",
        330,
        450,
        16,
        "Afternoon conference",
        ("projector", "audio", "microphone"),
    ),
)

L3_SPECS = (
    _spec(
        1,
        "summer_hot_humid_day",
        "夏季高温高湿综合工作日",
        "season",
        "如何协调制冷、湿度与无人预冷能耗？",
        month=7,
        day=6,
        outdoor=SUMMER,
        phases=_mixed_phases(),
        meetings=MIXED_MEETINGS,
        mode="cooling",
        target=24.0,
        purifier=True,
        presentation=True,
        metrics=("occupied_comfort", "humidity", "energy"),
    ),
    _spec(
        2,
        "winter_cold_dry_day",
        "冬季低温干燥综合工作日",
        "season",
        "供暖、加湿和新风热损失如何权衡？",
        month=1,
        day=12,
        outdoor=WINTER,
        phases=_mixed_phases(),
        meetings=MIXED_MEETINGS,
        mode="heating",
        target=22.0,
        humidifier=True,
        presentation=True,
        metrics=("temperature", "humidity", "heating_energy"),
    ),
    _spec(
        3,
        "shoulder_low_energy_day",
        "春秋过渡季低能耗运行日",
        "season",
        "舒适条件下如何减少不必要 HVAC 动作？",
        month=4,
        day=13,
        outdoor=SHOULDER,
        phases=_mixed_phases(),
        meetings=MIXED_MEETINGS,
        mode="fan",
        target=22.0,
        presentation=True,
        metrics=("energy", "action_count", "comfort"),
    ),
    _spec(
        4,
        "solar_peak_day",
        "强日照下午峰值负荷日",
        "season",
        "如何在太阳得热峰值前完成预处理？",
        month=6,
        day=18,
        outdoor=SOLAR_PEAK,
        phases=_mixed_phases(),
        meetings=MIXED_MEETINGS,
        mode="cooling",
        target=23.5,
        presentation=True,
        metrics=("peak_temperature", "peak_power", "recovery"),
    ),
    _spec(
        5,
        "outdoor_pm25_day",
        "室外 PM2.5 偏高活动日",
        "season",
        "高颗粒物室外空气下如何联合控制 CO2 与 PM2.5？",
        month=10,
        day=20,
        outdoor=POLLUTED,
        phases=_mixed_phases(),
        meetings=MIXED_MEETINGS,
        mode="fan",
        target=22.0,
        purifier=True,
        presentation=True,
        metrics=("co2", "pm25", "air_energy"),
        max_pm25_ug_m3=160.0,
    ),
    _spec(
        6,
        "k1324_staggered_office_day",
        "K1324 分批到达与午间降载工作日",
        "occupancy",
        "办公人数阶梯变化时如何及时增减载？",
        month=9,
        day=7,
        outdoor=SUMMER,
        phases=(
            _phase(30, "opening", k1324=3),
            _phase(90, "arrival_ramp", k1324=9),
            _phase(180, "full_office", k1324=16),
            _phase(270, "lunch_setback", k1324=4),
            _phase(360, "afternoon_return", k1324=14),
            _phase(540, "closeout", k1324=0),
        ),
        meetings=(),
        mode="cooling",
        target=24.0,
        purifier=True,
        metrics=("occupied_comfort", "occupancy_response", "unoccupied_energy"),
    ),
    _spec(
        7,
        "k1316_consecutive_seminars",
        "K1316 连续两场研讨周转日",
        "occupancy",
        "短换场如何继承并消除第一场的热量和 CO2？",
        month=9,
        day=8,
        outdoor=SUMMER,
        phases=(
            _phase(60, "preparation", k1316=0),
            _phase(90, "seminar_one", k1316=8),
            _phase(210, "turnover", k1316=0),
            _phase(240, "seminar_two", k1316=7),
            _phase(390, "closeout", k1316=0),
        ),
        meetings=(
            _meeting("seminar-one", "k1316", 90, 210, 8, "Seminar one"),
            _meeting("seminar-two", "k1316", 240, 390, 7, "Seminar two"),
        ),
        mode="cooling",
        target=24.0,
        presentation=True,
        metrics=("turnover_recovery", "second_readiness", "energy"),
    ),
    _spec(
        8,
        "k1315_defense_day",
        "K1315 多阶段答辩会议日",
        "occupancy",
        "答辩各阶段的环境和会议设备模式如何切换？",
        month=9,
        day=9,
        outdoor=SUMMER,
        phases=(
            _phase(45, "materials", k1315=2),
            _phase(120, "presentation", k1315=18),
            _phase(240, "questions", k1315=18),
            _phase(330, "closed_review", k1315=6),
            _phase(420, "summary", k1315=18),
            _phase(480, "closeout", k1315=0),
        ),
        meetings=(
            _meeting(
                "doctoral-defense",
                "k1315",
                120,
                480,
                18,
                "Doctoral defense",
                ("projector", "audio", "microphone"),
            ),
        ),
        mode="cooling",
        target=24.0,
        purifier=True,
        presentation=True,
        print_batches=(_print("defense_materials", 18, 3, 45, 110, 3),),
        metrics=("stage_readiness", "equipment_state", "cleanup"),
    ),
    _spec(
        9,
        "parallel_meetings",
        "K1315/K1316 同时会议协调日",
        "occupancy",
        "并行会议如何保持房间状态隔离？",
        month=9,
        day=10,
        outdoor=SUMMER,
        phases=_parallel_phases(),
        meetings=(
            _meeting(
                "formal-conference",
                "k1315",
                180,
                360,
                18,
                "Formal conference",
                ("projector", "audio"),
            ),
            _meeting("parallel-seminar", "k1316", 180, 360, 8, "Parallel seminar"),
        ),
        mode="cooling",
        target=24.0,
        presentation=True,
        metrics=("room_isolation", "parallel_completion", "comfort"),
    ),
    _spec(
        10,
        "three_room_high_occupancy",
        "三房间高占用综合运营日",
        "occupancy",
        "三房间接近容量时如何保持全楼稳定？",
        month=9,
        day=11,
        outdoor=SUMMER,
        phases=_parallel_phases(),
        meetings=(
            _meeting(
                "capacity-conference", "k1315", 180, 360, 18, "Capacity conference"
            ),
            _meeting("capacity-seminar", "k1316", 180, 360, 8, "Capacity seminar"),
        ),
        mode="cooling",
        target=23.5,
        purifier=True,
        presentation=True,
        metrics=("building_air_quality", "peak_load", "cleanup"),
    ),
    _spec(
        11,
        "meeting_downsize",
        "人数减少后的会议室降级调整日",
        "interaction",
        "人数减少后能否清理旧承诺并迁移至较小房间？",
        month=9,
        day=14,
        outdoor=SHOULDER,
        phases=(
            _phase(30, "baseline_plan", k1315=0, k1316=0),
            _phase(120, "replanning", k1315=0, k1316=0),
            _phase(240, "downsized_meeting", k1315=0, k1316=6),
            _phase(390, "closeout", k1315=0, k1316=0),
        ),
        meetings=(
            _meeting("downsized-meeting", "k1316", 240, 390, 6, "Downsized meeting"),
        ),
        mode="fan",
        target=22.0,
        presentation=True,
        interaction=_interaction(
            120,
            "downsized-meeting",
            "attendance reduced; move K1315 plan to K1316",
            from_room="k1315",
            to_room="k1316",
            old_attendees=12,
            attendees=6,
        ),
        metrics=("intent", "cleanup", "extra_energy"),
    ),
    _spec(
        12,
        "meeting_upgrade",
        "人数增加后的会议室升级调整日",
        "interaction",
        "人数超出小研讨室容量后能否升级房间？",
        month=9,
        day=15,
        outdoor=SUMMER,
        phases=(
            _phase(30, "baseline_plan", k1316=0, k1315=0),
            _phase(120, "replanning", k1316=0, k1315=0),
            _phase(240, "upgraded_meeting", k1316=0, k1315=14),
            _phase(390, "closeout", k1316=0, k1315=0),
        ),
        meetings=(
            _meeting(
                "upgraded-meeting",
                "k1315",
                240,
                390,
                14,
                "Upgraded meeting",
                ("projector", "audio"),
            ),
        ),
        mode="cooling",
        target=23.0,
        presentation=True,
        interaction=_interaction(
            120,
            "upgraded-meeting",
            "attendance increased; move K1316 plan to K1315",
            from_room="k1316",
            to_room="k1315",
            old_attendees=6,
            attendees=14,
        ),
        metrics=("capacity_compliance", "readiness", "plan_pollution"),
    ),
    _spec(
        13,
        "meeting_advanced",
        "会议提前后的压缩准备日",
        "interaction",
        "会议提前时应如何排序关键与非关键准备？",
        month=9,
        day=16,
        outdoor=SUMMER,
        phases=(
            _phase(30, "baseline_plan", k1315=0),
            _phase(90, "compressed_preparation", k1315=2),
            _phase(150, "advanced_meeting", k1315=16),
            _phase(300, "closeout", k1315=0),
        ),
        meetings=(
            _meeting(
                "advanced-meeting",
                "k1315",
                150,
                300,
                16,
                "Advanced meeting",
                ("projector", "audio"),
            ),
        ),
        mode="cooling",
        target=24.0,
        presentation=True,
        print_batches=(_print("advanced_agenda", 16, 2, 90, 140, 3, reveal=90),),
        interaction=_interaction(
            90,
            "advanced-meeting",
            "meeting advanced by ninety minutes",
            old_start_minute=240,
            new_start_minute=150,
        ),
        metrics=("critical_readiness", "task_tradeoff", "energy"),
    ),
    _spec(
        14,
        "equipment_requirement_added",
        "会议设备需求追加日",
        "interaction",
        "如何只修补新增设备需求而保持原计划？",
        month=9,
        day=17,
        outdoor=SHOULDER,
        phases=(
            _phase(30, "baseline_plan", k1315=0),
            _phase(150, "requirement_patch", k1315=2),
            _phase(240, "conference", k1315=12),
            _phase(390, "closeout", k1315=0),
        ),
        meetings=(
            _meeting(
                "equipment-added",
                "k1315",
                240,
                390,
                12,
                "Hybrid conference",
                ("projector", "audio", "microphone"),
            ),
        ),
        mode="fan",
        target=22.0,
        presentation=True,
        interaction=_interaction(
            150,
            "equipment-added",
            "microphone and printed handout requirements added",
            added_audio=True,
            added_printing=True,
        ),
        print_batches=(_print("hybrid_handout", 12, 2, 150, 225, 2, reveal=150),),
        metrics=("local_replan", "duplicate_actions", "readiness"),
    ),
    _spec(
        15,
        "early_finish_recovery",
        "活动提前结束与资源恢复日",
        "interaction",
        "活动提前结束后能否及时释放资源且不扰动后续活动？",
        month=9,
        day=18,
        outdoor=SUMMER,
        phases=(
            _phase(30, "opening", k1315=0, k1316=0),
            _phase(120, "first_activity", k1315=14, k1316=0),
            _phase(240, "early_release", k1315=0, k1316=0),
            _phase(330, "later_activity", k1315=0, k1316=7),
            _phase(480, "closeout", k1315=0, k1316=0),
        ),
        meetings=(
            _meeting("early-finish", "k1315", 120, 240, 14, "Early finish conference"),
            _meeting("later-seminar", "k1316", 330, 480, 7, "Later seminar"),
        ),
        mode="cooling",
        target=24.0,
        presentation=True,
        interaction=InteractionPlan(
            240,
            BuildingEventType.MEETING_ENDED_EARLY,
            "early-finish",
            "conference ended earlier than original plan",
            (("original_end_minute", 300),),
        ),
        metrics=("response_latency", "unoccupied_energy", "later_stability"),
    ),
    _spec(
        16,
        "power_limited_parallel_day",
        "全楼总功率受限的并行活动日",
        "constraint",
        "功率上限下如何错峰预处理和按优先级供能？",
        month=9,
        day=21,
        outdoor=SUMMER,
        phases=_parallel_phases(),
        meetings=(
            _meeting(
                "priority-conference", "k1315", 180, 360, 18, "Priority conference"
            ),
            _meeting("limited-seminar", "k1316", 180, 360, 7, "Power-limited seminar"),
        ),
        mode="cooling",
        target=24.0,
        presentation=True,
        power_limit_w=4200.0,
        metrics=("power_violation", "readiness", "comfort"),
    ),
    _spec(
        17,
        "strict_unoccupied_energy",
        "严格无人能耗预算工作日",
        "constraint",
        "长活动间隔中如何避免过早或持续预处理？",
        month=9,
        day=22,
        outdoor=SUMMER,
        phases=(
            _phase(30, "opening", k1324=4, k1315=0),
            _phase(120, "morning_work", k1324=12, k1315=0),
            _phase(240, "long_gap", k1324=0, k1315=0),
            _phase(420, "late_meeting", k1324=0, k1315=15),
            _phase(540, "closeout", k1324=0, k1315=0),
        ),
        meetings=(_meeting("late-meeting", "k1315", 420, 540, 15, "Late meeting"),),
        mode="cooling",
        target=24.0,
        presentation=True,
        metrics=("unoccupied_energy", "preparation_readiness", "start_count"),
        # Includes one bounded 60-minute preparation window for the late
        # meeting; continuous operation through the three-hour gap exceeds it.
        max_unoccupied_energy_kwh=3.0,
    ),
    _spec(
        18,
        "printing_material_coordination",
        "多活动打印与材料协调日",
        "constraint",
        "共享打印时间下如何安排材料批次与优先级？",
        month=9,
        day=23,
        outdoor=SHOULDER,
        phases=(
            _phase(30, "print_planning", k1315=2, k1316=0),
            _phase(150, "seminar", k1315=0, k1316=7),
            _phase(300, "turnover", k1315=2, k1316=0),
            _phase(390, "conference", k1315=16, k1316=0),
            _phase(540, "closeout", k1315=0, k1316=0),
        ),
        meetings=(
            _meeting("materials-seminar", "k1316", 150, 270, 7, "Materials seminar"),
            _meeting(
                "materials-conference", "k1315", 390, 540, 16, "Materials conference"
            ),
        ),
        mode="fan",
        target=22.0,
        presentation=True,
        print_batches=(
            _print("seminar_notes", 7, 3, 30, 135, 3),
            _print("conference_pack", 16, 4, 300, 375, 2),
            _print("nameplates", 16, 1, 300, 380, 1),
        ),
        metrics=("materials_on_time", "printer_runtime", "job_order"),
    ),
    _spec(
        19,
        "winter_ventilation_heating_tradeoff",
        "冬季高占用新风—供暖权衡日",
        "constraint",
        "高 CO2 风险与寒冷新风热损失如何平衡？",
        month=1,
        day=20,
        outdoor=WINTER,
        phases=(
            _phase(30, "opening", k1324=4, k1315=0),
            _phase(120, "occupancy_ramp", k1324=16, k1315=0),
            _phase(240, "high_occupancy", k1324=16, k1315=19),
            _phase(420, "recovery", k1324=8, k1315=0),
            _phase(540, "closeout", k1324=0, k1315=0),
        ),
        meetings=(
            _meeting(
                "winter-full-conference",
                "k1315",
                240,
                420,
                19,
                "Winter full conference",
            ),
        ),
        mode="heating",
        target=22.0,
        humidifier=True,
        presentation=True,
        metrics=("co2", "temperature", "heating_energy", "oscillation"),
    ),
    _spec(
        20,
        "multi_commitment_priority_day",
        "多承诺优先级综合日",
        "constraint",
        "多房间、多承诺和计划修改下如何维持全楼 Agenda？",
        month=9,
        day=24,
        outdoor=SUMMER,
        phases=_parallel_phases(),
        meetings=(
            _meeting(
                "priority-seminar-one", "k1316", 120, 210, 8, "Priority seminar one"
            ),
            _meeting(
                "priority-seminar-two", "k1316", 270, 360, 7, "Priority seminar two"
            ),
            _meeting(
                "priority-formal-meeting",
                "k1315",
                180,
                390,
                18,
                "Priority formal meeting",
                ("projector", "audio"),
            ),
        ),
        mode="cooling",
        target=24.0,
        purifier=True,
        presentation=True,
        print_batches=(_print("priority_meeting_pack", 18, 3, 120, 165, 3),),
        interaction=_interaction(
            240,
            "priority-seminar-two",
            "second seminar attendance reduced",
            old_attendees=8,
            attendees=7,
        ),
        power_limit_w=4600.0,
        metrics=("commitment_rate", "priority", "building_stability", "cleanup"),
    ),
)

L3_SPEC_BY_NUMBER = {spec.number: spec for spec in L3_SPECS}

if len(L3_SPECS) != 20 or len(L3_SPEC_BY_NUMBER) != 20:
    raise RuntimeError("the Kechuang L3 catalog must contain exactly 20 unique specs")
for _spec_item in L3_SPECS:
    _spec_item.validate()
