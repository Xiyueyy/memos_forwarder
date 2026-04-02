from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from astrbot.api import logger


class ConfigValidationError(ValueError):
    """Raised when plugin config is invalid."""


VISIBILITY_MODES = {
    "workspace",
    "protected",
    "public",
    "private",
    "workspace_or_public",
    "private_or_workspace",
    "private_or_public",
    "workspace_or_protected",
    "all_mine",
}


@dataclass(slots=True)
class SourceConfig:
    id: str
    base_url: str
    access_token: str
    creator_name: str = ""
    visibility_mode: str = "workspace_or_public"
    raw_filter: str = ""
    page_size: int = 20
    max_pages: int = 3
    timeout: int = 15
    enabled: bool = True


@dataclass(slots=True)
class TargetConfig:
    id: str
    platform: str
    unified_msg_origin: str = ""
    enabled: bool = True


@dataclass(slots=True)
class JobConfig:
    id: str
    source_ids: list[str]
    target_ids: list[str]
    interval_seconds: int = 300
    batch_size: int = 10
    enabled: bool = True


@dataclass(slots=True)
class MemosWorkspaceForwarderConfig:
    sources: list[SourceConfig]
    targets: list[TargetConfig]
    jobs: list[JobConfig]
    dedup_ttl_seconds: int = 7 * 24 * 60 * 60
    startup_delay_seconds: int = 20
    summary_max_chars: int = 280
    forward_images: bool = True
    max_images_per_memo: int = 4
    render_memo_card: bool = True
    card_preview_image_count: int = 4
    standalone_images_when_card_enabled: bool = False
    announcement_template: str = "{display_name} 发了新手记"
    time_format: str = "%Y-%m-%d %H:%M:%S"

    @property
    def poll_interval_seconds(self) -> int:
        enabled_intervals = [
            job.interval_seconds
            for job in self.jobs
            if job.enabled and job.interval_seconds > 0
        ]
        return min(enabled_intervals, default=300)

    @classmethod
    def from_context(cls, context_or_config) -> "MemosWorkspaceForwarderConfig":
        if isinstance(context_or_config, dict):
            runtime_conf = context_or_config
        else:
            runtime_conf = getattr(context_or_config, "config", {}) or {}

        sources_raw = cls._normalize_collection(runtime_conf.get("sources", []))
        targets_raw = cls._normalize_collection(runtime_conf.get("targets", []))
        jobs_raw = cls._normalize_collection(runtime_conf.get("jobs", []))

        sources = [
            SourceConfig(
                id=str(item.get("id", "")).strip(),
                base_url=str(item.get("base_url", "")).strip().rstrip("/"),
                access_token=str(item.get("access_token", "")).strip(),
                creator_name=str(item.get("creator_name", "")).strip(),
                visibility_mode=(
                    str(item.get("visibility_mode", "workspace_or_public")).strip()
                    or "workspace_or_public"
                ),
                raw_filter=str(item.get("raw_filter", "")).strip(),
                page_size=int(item.get("page_size", 20) or 20),
                max_pages=int(item.get("max_pages", 3) or 3),
                timeout=int(item.get("timeout", 15) or 15),
                enabled=bool(item.get("enabled", True)),
            )
            for item in sources_raw
        ]
        targets = [
            TargetConfig(
                id=str(item.get("id", "")).strip(),
                platform=str(item.get("platform", "")).strip(),
                unified_msg_origin=str(item.get("unified_msg_origin", "")).strip(),
                enabled=bool(item.get("enabled", True)),
            )
            for item in targets_raw
        ]
        jobs = [
            JobConfig(
                id=str(item.get("id", "")).strip(),
                source_ids=cls._normalize_id_list(item.get("source_ids", [])),
                target_ids=cls._normalize_id_list(item.get("target_ids", [])),
                interval_seconds=int(item.get("interval_seconds", 300) or 300),
                batch_size=int(item.get("batch_size", 10) or 10),
                enabled=bool(item.get("enabled", True)),
            )
            for item in jobs_raw
        ]

        jobs = cls._build_implicit_job_if_needed(sources, targets, jobs)

        config = cls(
            sources=sources,
            targets=targets,
            jobs=jobs,
            dedup_ttl_seconds=int(
                runtime_conf.get("dedup_ttl_seconds", 7 * 24 * 60 * 60)
                or 7 * 24 * 60 * 60
            ),
            startup_delay_seconds=int(runtime_conf.get("startup_delay_seconds", 20) or 20),
            summary_max_chars=int(runtime_conf.get("summary_max_chars", 280) or 280),
            forward_images=bool(runtime_conf.get("forward_images", True)),
            max_images_per_memo=max(
                0,
                int(runtime_conf.get("max_images_per_memo", 4) or 0),
            ),
            render_memo_card=bool(runtime_conf.get("render_memo_card", True)),
            card_preview_image_count=max(
                0,
                int(runtime_conf.get("card_preview_image_count", 4) or 0),
            ),
            standalone_images_when_card_enabled=bool(
                runtime_conf.get("standalone_images_when_card_enabled", False)
            ),
            announcement_template=(
                str(runtime_conf.get("announcement_template", "{display_name} 发了新手记")).strip()
                or "{display_name} 发了新手记"
            ),
            time_format=(
                str(runtime_conf.get("time_format", "%Y-%m-%d %H:%M:%S")).strip()
                or "%Y-%m-%d %H:%M:%S"
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        self._validate_unique_ids("source", [source.id for source in self.sources])
        self._validate_unique_ids("target", [target.id for target in self.targets])
        self._validate_unique_ids("job", [job.id for job in self.jobs])

        source_ids = {source.id for source in self.sources}
        target_ids = {target.id for target in self.targets}

        for source in self.sources:
            if not source.enabled:
                continue
            if not source.id:
                raise ConfigValidationError("sources.id 不能为空")
            self._validate_url(source.base_url, f"sources[{source.id}].base_url")
            if not source.access_token:
                raise ConfigValidationError(f"sources[{source.id}].access_token 不能为空")
            if source.visibility_mode not in VISIBILITY_MODES:
                raise ConfigValidationError(
                    f"sources[{source.id}].visibility_mode 非法: {source.visibility_mode}"
                )
            if source.page_size <= 0 or source.page_size > 1000:
                raise ConfigValidationError(
                    f"sources[{source.id}].page_size 必须在 1-1000 之间"
                )
            if source.max_pages <= 0:
                raise ConfigValidationError(f"sources[{source.id}].max_pages 必须 > 0")
            if source.timeout <= 0:
                raise ConfigValidationError(f"sources[{source.id}].timeout 必须 > 0")

        for target in self.targets:
            if not target.enabled:
                continue
            if not target.id:
                raise ConfigValidationError("targets.id 不能为空")
            if not target.platform:
                raise ConfigValidationError(f"targets[{target.id}].platform 不能为空")
            if not target.unified_msg_origin:
                raise ConfigValidationError(
                    f"targets[{target.id}].unified_msg_origin 不能为空"
                )

        for job in self.jobs:
            if not job.enabled:
                continue
            if not job.id:
                raise ConfigValidationError("jobs.id 不能为空")
            if not job.source_ids:
                raise ConfigValidationError(f"jobs[{job.id}].source_ids 不能为空")
            if job.interval_seconds <= 0:
                raise ConfigValidationError(f"jobs[{job.id}].interval_seconds 必须 > 0")
            if job.batch_size <= 0:
                raise ConfigValidationError(f"jobs[{job.id}].batch_size 必须 > 0")

            missing_sources = [
                source_id for source_id in job.source_ids if source_id not in source_ids
            ]
            if missing_sources:
                raise ConfigValidationError(
                    f"jobs[{job.id}] 引用了不存在的 source_ids: {missing_sources}"
                )

            missing_targets = [
                target_id for target_id in job.target_ids if target_id not in target_ids
            ]
            if missing_targets:
                raise ConfigValidationError(
                    f"jobs[{job.id}] 引用了不存在的 target_ids: {missing_targets}"
                )

        if self.dedup_ttl_seconds <= 0:
            raise ConfigValidationError("dedup_ttl_seconds 必须 > 0")
        if self.startup_delay_seconds < 0:
            raise ConfigValidationError("startup_delay_seconds 不能 < 0")
        if self.summary_max_chars <= 0:
            raise ConfigValidationError("summary_max_chars 必须 > 0")
        if self.max_images_per_memo < 0:
            raise ConfigValidationError("max_images_per_memo 不能 < 0")
        if self.card_preview_image_count < 0:
            raise ConfigValidationError("card_preview_image_count 不能 < 0")
        if not self.announcement_template:
            raise ConfigValidationError("announcement_template 不能为空")

        if not any(job.enabled for job in self.jobs):
            logger.warning("memos workspace forwarder 没有启用中的 job")

    @staticmethod
    def _normalize_collection(raw_value) -> list[dict]:
        if raw_value is None:
            return []
        if isinstance(raw_value, list):
            return [item for item in raw_value if isinstance(item, dict)]
        return []

    @staticmethod
    def _normalize_id_list(raw_value) -> list[str]:
        if raw_value is None:
            return []
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        normalized: list[str] = []
        for value in values:
            text = str(value).strip()
            if text and text not in normalized:
                normalized.append(text)
        return normalized

    @staticmethod
    def _validate_unique_ids(kind: str, ids: list[str]) -> None:
        duplicates: set[str] = set()
        seen: set[str] = set()
        for item_id in ids:
            if not item_id:
                continue
            if item_id in seen:
                duplicates.add(item_id)
            seen.add(item_id)
        if duplicates:
            raise ConfigValidationError(f"{kind} ID 重复: {sorted(duplicates)}")

    @staticmethod
    def _validate_url(url: str, field_name: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigValidationError(f"{field_name} 不是合法 URL: {url}")

    @classmethod
    def _build_implicit_job_if_needed(
        cls,
        sources: list[SourceConfig],
        targets: list[TargetConfig],
        jobs: list[JobConfig],
    ) -> list[JobConfig]:
        if jobs:
            return jobs

        enabled_sources = [source.id for source in sources if source.enabled and source.id]
        enabled_targets = [target.id for target in targets if target.enabled and target.id]
        if not enabled_sources:
            return jobs

        return [
            JobConfig(
                id="memos_default",
                source_ids=enabled_sources,
                target_ids=enabled_targets,
                interval_seconds=300,
                batch_size=10,
                enabled=True,
            )
        ]
