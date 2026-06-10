from __future__ import annotations

import json
import time
import traceback
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..config import BemineSettings, config_bool, config_list
from ..db import BemineDB
from ..deploy.heartbeat import write_heartbeat, current_head_sha
from ..kernel import BemineKernel
from ..index.status import _redact, collect_fleet_status, render_status
from .session_watcher import check_sessions

DiscordSender = Callable[[str], Mapping[str, Any]]

DEFAULT_DAILY_DIGEST_SCHEDULE = "0 18 * * *"
DEFAULT_SESSION_WATCHER_SCHEDULE = "*/2 * * * *"


@dataclass(frozen=True)
class JobDefinition:
    job_id: str
    name: str
    trigger_type: str
    schedule: str
    config: dict[str, Any]
    enabled: bool = True


@dataclass(frozen=True)
class JobRunResult:
    job_id: str
    status: str
    output: str
    notified: bool = False


def default_jobs(settings: BemineSettings | None = None) -> tuple[JobDefinition, ...]:
    from ..config import config_string

    devices = (settings.status_devices if settings else ["local", "mbp", "mbp2", "k2"])
    # Who to DM with session updates. Empty -> falls back to the home channel.
    notify_user_id = config_string("discord", "notify_user_id", "")
    session_config: dict[str, Any] = {
        "type": "session_watcher",
        "devices": devices,
        # The watcher self-delivers (per-project channel + DM fallback), so the generic
        # job notifier stays off to avoid a duplicate send.
        "notify_discord": False,
        "only_when_events": True,
    }
    if notify_user_id:
        session_config["dm_user_id"] = notify_user_id
    return (
        JobDefinition(
            job_id="session_watcher",
            name="Session watcher",
            trigger_type="cron",
            schedule=DEFAULT_SESSION_WATCHER_SCHEDULE,
            config=session_config,
        ),
        JobDefinition(
            job_id="daily_digest",
            name="Daily project digest",
            trigger_type="cron",
            schedule=DEFAULT_DAILY_DIGEST_SCHEDULE,
            config={
                "type": "daily_digest",
                "devices": devices,
                "status_limit": 12,
                "rundown_limit": 10,
                "notify_discord": True,
            },
        ),
        JobDefinition(
            job_id="dream",
            name="Dream memory condensation",
            trigger_type="cron",
            schedule=config_string("dream", "schedule", "15 3 * * *"),
            config={
                "type": "dream",
                "limit": 100,
                "notify_discord": False,
            },
        ),
    )


def seed_default_jobs(
    db: BemineDB,
    *,
    settings: BemineSettings | None = None,
    now: datetime | None = None,
) -> None:
    now = now or _now()
    for job in default_jobs(settings):
        if db.get_job(job.job_id) is not None:
            continue
        db.upsert_job(
            job_id=job.job_id,
            name=job.name,
            enabled=job.enabled,
            trigger_type=job.trigger_type,
            schedule=job.schedule,
            config_json=json.dumps(job.config, sort_keys=True),
            next_run_at=next_run_after(job.schedule, now).isoformat(),
        )


def render_jobs(rows: list[Mapping[str, Any]]) -> str:
    if not rows:
        return "No jobs configured.\n"
    lines = ["Configured jobs:"]
    for row in rows:
        enabled = "enabled" if int(row["enabled"]) else "disabled"
        next_run = row["next_run_at"] or "unscheduled"
        last_run = row["last_run_at"] or "never"
        failures = int(row["failure_count"] or 0)
        lines.append(
            f"- {row['job_id']} [{enabled}] {row['trigger_type']} `{row['schedule']}`"
        )
        lines.append(f"  next: {next_run} last: {last_run} failures: {failures}")
    lines.append("")
    return "\n".join(lines)


def upsert_reminder_job(
    db: BemineDB,
    *,
    job_id: str,
    schedule: str,
    text: str,
    notify_discord: bool = True,
    now: datetime | None = None,
) -> None:
    text = text.strip()
    if not text:
        raise ValueError("reminder text is required")
    if not job_id.strip():
        raise ValueError("job_id is required")
    now = now or _now()
    db.upsert_job(
        job_id=job_id.strip(),
        name=f"Reminder: {text[:48]}",
        enabled=True,
        trigger_type="cron",
        schedule=schedule,
        config_json=json.dumps(
            {
                "type": "reminder",
                "text": text,
                "notify_discord": notify_discord,
            },
            sort_keys=True,
        ),
        next_run_at=next_run_after(schedule, now).isoformat(),
    )


def due_jobs(db: BemineDB, *, now: datetime | None = None) -> list[Mapping[str, Any]]:
    now = now or _now()
    rows = db.list_jobs(enabled=True)
    due: list[Mapping[str, Any]] = []
    for row in rows:
        next_run = _parse_datetime(row["next_run_at"])
        if next_run is None:
            next_run = next_run_after(row["schedule"], now - timedelta(minutes=1))
            db.update_job_after_run(
                job_id=row["job_id"],
                last_run_at=row["last_run_at"] or "",
                next_run_at=next_run.isoformat(),
                success=True,
            )
        if next_run <= now:
            due.append(row)
    return due


def run_due_jobs(
    settings: BemineSettings,
    *,
    now: datetime | None = None,
    sender: DiscordSender | None = None,
) -> list[JobRunResult]:
    kernel = BemineKernel(settings)
    seed_default_jobs(kernel.db, settings=settings, now=now)
    return [run_job_row(settings, row, sender=sender, now=now) for row in due_jobs(kernel.db, now=now)]


def run_job_by_id(
    settings: BemineSettings,
    job_id: str,
    *,
    sender: DiscordSender | None = None,
    now: datetime | None = None,
) -> JobRunResult:
    kernel = BemineKernel(settings)
    seed_default_jobs(kernel.db, settings=settings, now=now)
    row = kernel.db.get_job(job_id)
    if row is None:
        raise ValueError(f"unknown job: {job_id}")
    return run_job_row(settings, row, sender=sender, now=now)


def run_job_row(
    settings: BemineSettings,
    row: Mapping[str, Any],
    *,
    sender: DiscordSender | None = None,
    now: datetime | None = None,
) -> JobRunResult:
    now = now or _now()
    db = BemineDB(settings.db_path)
    job_id = str(row["job_id"])
    started_at = now.isoformat()
    status = "success"
    notified = False
    try:
        config = json.loads(str(row["config_json"] or "{}"))
        if not isinstance(config, dict):
            raise ValueError("job config_json must be an object")
        output = _run_job_type(settings, job_id, config)
        # Only notify when there is something to send (session_watcher returns "" when
        # there are no events — don't post an empty message or touch an unbound `sent`).
        if config.get("notify_discord", False) and output.strip():
            sender = sender or _discord_sender(
                db, channel_id=config.get("channel_id"), dm_user_id=config.get("dm_user_id")
            )
            sent = dict(sender(output))
            notified = bool(sent.get("id") or sent.get("message_id"))
            _record_job_notification(db, job_id, sent)
    except Exception:
        status = "failed"
        output = traceback.format_exc(limit=8)

    finished = _now()
    output = _redact(output)
    db.record_job_run(
        run_id=f"jobrun_{uuid.uuid4().hex}",
        job_id=job_id,
        started_at=started_at,
        finished_at=finished.isoformat(),
        status=status,
        output=output,
    )
    db.update_job_after_run(
        job_id=job_id,
        last_run_at=finished.isoformat(),
        next_run_at=next_run_after(str(row["schedule"]), finished).isoformat(),
        success=status == "success",
    )
    db.record_audit_event(
        category="job",
        event_type="job_run_finished",
        actor="bemine-daemon",
        device_id=settings.device_id,
        payload_json=json.dumps(
            {
                "job_id": job_id,
                "status": status,
                "notified": notified,
            },
            sort_keys=True,
        ),
    )
    return JobRunResult(job_id=job_id, status=status, output=output, notified=notified)


def generate_daily_digest(settings: BemineSettings, config: Mapping[str, Any]) -> str:
    kernel = BemineKernel(settings)
    scan = kernel.scan_local()
    devices = _list_config(config, "devices", settings.status_devices)
    status_limit = int(config.get("status_limit") or 12)
    rundown_limit = int(config.get("rundown_limit") or 10)
    statuses, errors = collect_fleet_status(settings, devices=devices)

    attention = [item for item in statuses if item.asks_user][:5]
    lines = [
        "**Bemine daily digest**",
        f"Time: {_now().isoformat()}",
        f"Scan: {scan.sessions} sessions, {scan.projects} projects, {scan.devices} devices indexed.",
        "",
        "**Attention needed**",
    ]
    if attention:
        for item in attention:
            lines.append(
                f"- {item.device_id} {item.runtime} `{item.cwd}`: {item.ask_summary}"
            )
    else:
        lines.append("- No obvious agent requests detected.")

    lines.extend(
        [
            "",
            "**Recent sessions**",
            render_status(statuses, errors, limit=status_limit).strip(),
            "",
            "**Project rundown**",
            kernel.project_rundown(limit=rundown_limit).strip(),
            "",
        ]
    )
    return _redact("\n".join(lines))


def next_run_after(schedule: str, after: datetime) -> datetime:
    fields = schedule.split()
    if len(fields) != 5:
        raise ValueError(f"unsupported cron schedule: {schedule!r}")
    minute_set, hour_set, day_set, month_set, weekday_set = [
        _parse_cron_field(field, minimum, maximum)
        for field, minimum, maximum in zip(
            fields,
            (0, 0, 1, 1, 0),
            (59, 23, 31, 12, 7),
        )
    ]
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    deadline = candidate + timedelta(days=370)
    while candidate <= deadline:
        cron_weekday = (candidate.weekday() + 1) % 7
        if (
            candidate.minute in minute_set
            and candidate.hour in hour_set
            and candidate.day in day_set
            and candidate.month in month_set
            and (cron_weekday in weekday_set or (cron_weekday == 0 and 7 in weekday_set))
        ):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError(f"could not resolve next run for schedule: {schedule!r}")


class BemineDaemon:
    def __init__(self, settings: BemineSettings, *, poll_seconds: int = 30) -> None:
        self.settings = settings
        self.poll_seconds = poll_seconds
        try:
            self._boot_sha = current_head_sha()
        except Exception:
            self._boot_sha = ""

    def run_once(self) -> list[JobRunResult]:
        self._deploy_tick()
        self._continuity_tick()
        results = run_due_jobs(self.settings)
        self._tick_watchers()
        self._recovery_tick()
        return results

    def run_forever(self) -> None:
        db = BemineDB(self.settings.db_path)
        seed_default_jobs(db)
        while True:
            self._deploy_tick()
            self._continuity_tick()
            run_due_jobs(self.settings)
            self._tick_watchers()
            self._recovery_tick()
            time.sleep(self.poll_seconds)

    def _deploy_tick(self) -> None:
        from bemine.deploy.pipeline import run_deploy, maybe_confirm_deploy, reap_stale_deploy
        from bemine.deploy.state import DeployState, state_path
        try:
            write_heartbeat(self.settings, "daemon", self._boot_sha)
        except Exception:
            pass
        try:
            st = DeployState.load(state_path(self.settings))
            if st.phase == "requested":
                run_deploy(self.settings, channel_id=st.channel_id, self_restart_daemon=True)
            elif st.phase == "deploying":
                reap_stale_deploy(self.settings)
            else:
                maybe_confirm_deploy(self.settings)
        except Exception:
            pass

    def _tick_watchers(self) -> None:
        from .project_watcher import tick_all_watchers
        try:
            db = BemineDB(self.settings.db_path)
            tick_all_watchers(self.settings, db)
        except Exception:
            pass

    def _recovery_tick(self) -> None:
        import time as _time
        from bemine.jobs import auto_recovery as ar
        try:
            if not ar.recovery_enabled():
                return
            now = _time.time()
            # The daemon polls every ~30s, but recovery acts on its own (slower) cadence.
            if now - getattr(self, "_last_recovery_ts", 0.0) < ar._cfg_poll_interval():
                return
            self._last_recovery_ts = now
            ar.tick_recovery(self.settings, ar.default_recovery_deps(self.settings),
                             now=now)
        except Exception:
            pass

    def _continuity_tick(self) -> None:
        try:
            if not _continuity_enabled(self.settings):
                return
            from bemine.workers.workers import resume_missing_local_workers

            resume_missing_local_workers(self.settings)
        except Exception:
            pass


def _run_job_type(settings: BemineSettings, job_id: str, config: Mapping[str, Any]) -> str:
    job_type = str(config.get("type") or job_id)
    if job_type == "daily_digest":
        return generate_daily_digest(settings, config)
    if job_type == "reminder":
        return generate_reminder(job_id, config)
    if job_type == "session_watcher":
        return generate_session_watcher_report(settings, config)
    if job_type == "dream":
        from bemine.dreams import run_dream, render_dream

        result = run_dream(settings, limit=int(config.get("limit") or 100))
        return render_dream(result)
    raise ValueError(f"unsupported job type: {job_type}")


def _continuity_enabled(settings: BemineSettings) -> bool:
    if not config_bool("continuity", "enabled", True):
        return False
    devices = config_list("continuity", "devices", ["cdjk"])
    return settings.device_id in devices




def generate_session_watcher_report(settings: BemineSettings, config: Mapping[str, Any]) -> str:
    from .session_watcher import deliver_events

    state_path = settings.state_dir / "session_watcher_state.json"
    events = check_sessions(settings, state_path)
    if not events:
        return "" if config.get("only_when_events", True) else "No session changes detected."
    # Self-delivers to a channel per project (auto-created); undelivered events are
    # re-armed via state_path so the next poll retries them (at-least-once). The job has
    # notify_discord disabled so run_job_row does not also send.
    return "Session watcher delivered — " + deliver_events(
        settings, events, state_path=state_path
    )

def generate_reminder(job_id: str, config: Mapping[str, Any]) -> str:
    text = str(config.get("text") or "").strip()
    if not text:
        raise ValueError("reminder text is required")
    return _redact(
        "\n".join(
            [
                "**Bemine reminder**",
                f"Job: `{job_id}`",
                f"Time: {_now().isoformat()}",
                "",
                text,
            ]
        )
    )


def _discord_sender(
    db: BemineDB, channel_id: str | None = None, dm_user_id: str | None = None
) -> DiscordSender:
    from bemine.discord.config import Settings
    from bemine.discord.send import send_message, open_dm_channel
    from bemine.discord.store import Store
    import asyncio

    base = Settings.load()
    settings = Settings(
        token=base.token,
        home_channel_id=base.home_channel_id,
        allowed_users=base.allowed_users,
        allow_all_users=base.allow_all_users,
        mayor_home=db.path.parent,
        db_path=db.path,
        inbox_dir=db.path.parent / "inbox",
    )
    settings.require_token()
    if dm_user_id:
        # Deliver to the user's DM (resolve the bot<->user DM channel).
        target_channel_id = asyncio.run(open_dm_channel(settings, str(dm_user_id)))
    else:
        target_channel_id = (channel_id or settings.home_channel_id).strip()
        if not target_channel_id:
            settings.require_home_channel()

    def sender(text: str) -> Mapping[str, Any]:
        result = asyncio.run(
            send_message(
                settings=settings,
                channel_id=target_channel_id,
                text=text,
                files=[],
            )
        )
        Store(db.path).record_outbound(
            discord_message_id=str(result.get("id") or ""),
            channel_id=target_channel_id,
            content=text,
            files=[],
        )
        return result

    return sender


def _record_job_notification(db: BemineDB, job_id: str, sent: Mapping[str, Any]) -> None:
    db.record_audit_event(
        category="job",
        event_type="job_notification_sent",
        actor="bemine-daemon",
        payload_json=json.dumps(
            {
                "job_id": job_id,
                "channel_id": str(sent.get("channel_id") or ""),
                "discord_message_id": str(sent.get("id") or sent.get("message_id") or ""),
            },
            sort_keys=True,
        ),
    )


def _parse_cron_field(field: str, minimum: int, maximum: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, raw_step = part.split("/", 1)
            step = int(raw_step)
            if step <= 0:
                raise ValueError("cron step must be positive")
        if part == "*":
            start, end = minimum, maximum
        elif "-" in part:
            raw_start, raw_end = part.split("-", 1)
            start, end = int(raw_start), int(raw_end)
        else:
            start = end = int(part)
        if start < minimum or end > maximum or start > end:
            raise ValueError(f"cron field {field!r} outside {minimum}-{maximum}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError("cron field cannot be empty")
    return values


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_now().tzinfo)
    return parsed


def _list_config(config: Mapping[str, Any], key: str, default: list[str]) -> list[str]:
    raw = config.get(key)
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return list(default)


def _now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)
