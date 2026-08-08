from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from ._loader import load_personification_module

admin_commands = load_personification_module("plugin.personification.handlers.persona_admin_commands")
scheduler_registration = load_personification_module("plugin.personification.jobs.scheduler_registration")


class _Job:
    def __init__(self, job_id: str, *, next_run_time=None, trigger: str = "interval[0:15:00]") -> None:  # noqa: ANN001
        self.id = job_id
        self.next_run_time = next_run_time
        self.trigger = trigger


class _Scheduler:
    running = True

    def __init__(self, jobs: list[_Job]) -> None:
        self._jobs = jobs

    def get_jobs(self) -> list[_Job]:
        return list(self._jobs)


def _bundle(jobs: list[_Job]) -> SimpleNamespace:
    return SimpleNamespace(
        scheduler=_Scheduler(jobs),
        plugin_config=SimpleNamespace(
            personification_qzone_enabled=False,
            personification_proactive_enabled=True,
            personification_group_idle_enabled=False,
            personification_qzone_proactive_enabled=False,
            personification_qzone_social_enabled=False,
            personification_qzone_inbound_enabled=False,
        ),
    )


def test_render_scheduler_status_lists_registered_jobs_and_missing_enabled_jobs() -> None:
    next_run = datetime(2026, 5, 2, 23, 59, tzinfo=timezone.utc)
    text = admin_commands.render_scheduler_status(
        _bundle(
            [
                _Job("personification_daily_fav_report", next_run_time=next_run, trigger="cron[23:59]"),
                _Job("personification_background_intelligence"),
            ]
        )
    )

    assert "定时任务状态" in text
    assert "Scheduler：运行中" in text
    assert "每日群好感统计：已注册" in text
    assert "主动私聊：未注册（开关：开" in text
    assert "后台智能维护：已注册" in text


def test_render_scheduler_status_lists_qzone_social_job_when_enabled() -> None:
    bundle = _bundle([_Job("personification_qzone_social_scan")])
    bundle.plugin_config.personification_qzone_social_enabled = True

    text = admin_commands.render_scheduler_status(bundle)

    assert "好友空间互动：已注册" in text


def test_render_scheduler_status_lists_qzone_inbound_job_when_enabled() -> None:
    bundle = _bundle([_Job("personification_qzone_inbound_poll")])
    bundle.plugin_config.personification_qzone_inbound_enabled = True

    text = admin_commands.render_scheduler_status(bundle)

    assert "空间消息轮询：已注册" in text


def test_scheduler_status_summary_counts_missing_enabled_jobs() -> None:
    text = admin_commands._render_scheduler_status_summary(_bundle([_Job("personification_daily_fav_report")]))

    assert "定时任务：运行中" in text
    assert "已注册 1 个" in text
    assert "缺失 2 个" in text


def test_qzone_jobs_define_overlap_and_misfire_policy() -> None:
    calls = []

    class Recorder:
        def add_job(self, *args, **kwargs):  # noqa: ANN002, ANN003
            calls.append((args, kwargs))

    logger = SimpleNamespace(info=lambda *_args: None, error=lambda *_args: None)
    scheduler_registration.register_qzone_social_scan_job(
        scheduler=Recorder(), qzone_social_scan_job=lambda: None, interval_minutes=30, logger=logger,
    )
    scheduler_registration.register_qzone_inbound_poll_job(
        scheduler=Recorder(), qzone_inbound_poll_job=lambda: None, interval_minutes=3, logger=logger,
    )

    assert len(calls) == 2
    assert all(call[1]["max_instances"] == 1 for call in calls)
    assert all(call[1]["coalesce"] is True for call in calls)
    assert all(call[1]["misfire_grace_time"] > 0 for call in calls)
