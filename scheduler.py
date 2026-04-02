from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from astrbot.api import logger

from .config import JobConfig, MemosWorkspaceForwarderConfig


@dataclass(slots=True)
class JobExecutionResult:
    started_at: datetime
    duration_ms: int
    fetched_count: int
    pushed_count: int
    error_summary: str = ""


class MemosWorkspaceScheduler:
    """任务调度器：按间隔轮询 Memos 源并主动推送。"""

    def __init__(self, config, fetcher, dispatcher, storage) -> None:
        self.config: MemosWorkspaceForwarderConfig = config
        self._fetcher = fetcher
        self._dispatcher = dispatcher
        self.storage = storage
        self.running = False
        self._job_tasks: dict[str, asyncio.Task] = {}
        self._job_locks: dict[str, asyncio.Lock] = {}
        self._job_results: dict[str, JobExecutionResult] = {}
        self._paused_jobs: set[str] = set()

    @property
    def last_results(self) -> dict[str, JobExecutionResult]:
        return self._job_results

    @property
    def paused_jobs(self) -> set[str]:
        return self._paused_jobs

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        for job in self.config.jobs:
            if not job.enabled:
                continue
            self._job_locks.setdefault(job.id, asyncio.Lock())
            self._job_tasks[job.id] = asyncio.create_task(
                self._run_job_loop(job),
                name=f"memosws-job-{job.id}",
            )

    async def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        tasks = list(self._job_tasks.values())
        self._job_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run_job_once(self, job_id: str | None = None) -> bool:
        jobs = self._select_jobs(job_id)
        if not jobs:
            return False
        for job in jobs:
            await self._execute_job(job)
        return True

    async def pause_job(self, job_id: str) -> bool:
        job = next((job for job in self.config.jobs if job.id == job_id and job.enabled), None)
        if job is None:
            return False
        self._paused_jobs.add(job_id)
        return True

    def resume_job(self, job_id: str) -> bool:
        job = next((job for job in self.config.jobs if job.id == job_id and job.enabled), None)
        if job is None:
            return False
        self._paused_jobs.discard(job_id)
        return True

    def _select_jobs(self, job_id: str | None) -> list[JobConfig]:
        if job_id:
            job = next((job for job in self.config.jobs if job.id == job_id and job.enabled), None)
            return [job] if job is not None else []
        return [job for job in self.config.jobs if job.enabled]

    async def _run_job_loop(self, job: JobConfig) -> None:
        startup_delay = max(int(self.config.startup_delay_seconds), 0)
        if startup_delay > 0:
            await asyncio.sleep(startup_delay)

        while self.running:
            if job.id not in self._paused_jobs:
                await self._execute_job(job)
            await asyncio.sleep(max(int(job.interval_seconds), 1))

    async def _execute_job(self, job: JobConfig) -> None:
        lock = self._job_locks.setdefault(job.id, asyncio.Lock())
        if lock.locked():
            logger.warning("job=%s skipped: previous run still in progress", job.id)
            return

        async with lock:
            started_at = datetime.now()
            started_perf = time.perf_counter()
            fetched_count = 0
            pushed_count = 0
            error_summary = ""
            seen_in_run: set[str] = set()

            try:
                items = await self._fetcher.fetch(job)
                fetched_count = len(items)
                items = self._sort_items(items)

                for item in items:
                    item_id = str(item.get("id", "")).strip()
                    if not item_id:
                        continue
                    if item_id in seen_in_run:
                        continue
                    if await self.storage.has_seen(item_id):
                        continue
                    if pushed_count >= max(int(job.batch_size), 1):
                        break

                    seen_in_run.add(item_id)
                    event_item = dict(item)
                    event_item["job_id"] = job.id
                    dispatch_result = await self._dispatcher.dispatch(event_item)
                    if dispatch_result.success_count > 0:
                        await self.storage.mark_seen(
                            item_id,
                            ttl_seconds=self.config.dedup_ttl_seconds,
                        )
                        pushed_count += 1

                now_ts = int(time.time())
                for source_id in job.source_ids:
                    await self.storage.update_source_state(
                        source_id,
                        creator_name=self._fetcher.get_resolved_creator_name(source_id) or None,
                        creator_ref=self._fetcher.get_resolved_creator_ref(source_id) or None,
                        last_success_time=now_ts,
                        bootstrap_done=True,
                    )
            except Exception as exc:
                error_summary = f"{type(exc).__name__}: {exc}"
                logger.exception("job=%s execution failed", job.id)

            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            self._job_results[job.id] = JobExecutionResult(
                started_at=started_at,
                duration_ms=duration_ms,
                fetched_count=fetched_count,
                pushed_count=pushed_count,
                error_summary=error_summary,
            )
            logger.info(
                "job=%s finished: fetched=%s pushed=%s duration_ms=%s error=%s",
                job.id,
                fetched_count,
                pushed_count,
                duration_ms,
                error_summary or "",
            )

    @classmethod
    def _sort_items(cls, items: list[dict]) -> list[dict]:
        return sorted(
            items,
            key=lambda item: (
                cls._parse_item_timestamp(item.get("published_at")),
                str(item.get("id", "")),
            ),
        )

    @staticmethod
    def _parse_item_timestamp(raw_value) -> datetime:
        text = str(raw_value or "").strip()
        if not text:
            return datetime.fromtimestamp(0, tz=timezone.utc)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return datetime.fromtimestamp(0, tz=timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
