from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

try:
    from astrbot.api.star import StarTools
except ImportError:  # pragma: no cover
    StarTools = None


class MemoForwarderStorage:
    """存储层：负责去重与轻量状态持久化。"""

    SOURCE_STATE_PREFIX = "source_state:"
    CONTENT_KEY_PREFIX = "content_seen:"
    CONTENT_INDEX_KEY = "content_seen_index"

    def __init__(
        self,
        plugin_name: str = "astrbot_plugin_memos_workspace_forwarder",
        get_kv_data: Callable[[str], Awaitable[Any]] | None = None,
        put_kv_data: Callable[[str, Any], Awaitable[Any]] | None = None,
        delete_kv_data: Callable[[str], Awaitable[Any]] | None = None,
        storage_dir: str | Path | None = None,
    ) -> None:
        self._plugin_name = plugin_name
        self._get_kv_data = get_kv_data
        self._put_kv_data = put_kv_data
        self._delete_kv_data = delete_kv_data
        self._fallback_store: dict[str, str] = {}
        self._seen_ids: set[str] = set()
        cache_root = Path(storage_dir) if storage_dir is not None else self.plugin_cache_dir()
        self._state_path = cache_root / "state.json"
        self._state_loaded = False
        self._disk_state: dict[str, Any] = {"kv": {}}

    async def get(self, key: str, default: Any = None) -> Any:
        await self._ensure_state_loaded()
        kv_store = self._disk_state.setdefault("kv", {})
        if key in kv_store:
            return kv_store[key]

        raw = await self._read_raw_from_backend(key)
        decoded = self._decode_value(raw)
        if decoded is None:
            return default
        kv_store[key] = decoded
        self._flush_state()
        return decoded

    async def put(self, key: str, value: Any) -> None:
        await self._ensure_state_loaded()
        self._disk_state.setdefault("kv", {})[key] = value
        self._flush_state()

        encoded = json.dumps(value, ensure_ascii=False)
        if self._put_kv_data is None:
            self._fallback_store[key] = encoded
            return
        await self._put_kv_data(key, encoded)

    async def delete(self, key: str) -> None:
        await self._ensure_state_loaded()
        self._disk_state.setdefault("kv", {}).pop(key, None)
        self._flush_state()

        if self._delete_kv_data is None:
            self._fallback_store.pop(key, None)
            return
        await self._delete_kv_data(key)

    async def has_seen(self, item_id: str) -> bool:
        if item_id in self._seen_ids:
            record = await self.get(self._content_key(item_id), default=None)
            if record and not self._is_expired(record):
                return True

        record = await self.get(self._content_key(item_id), default=None)
        if not record:
            self._seen_ids.discard(item_id)
            return False

        if self._is_expired(record):
            await self.delete(self._content_key(item_id))
            self._seen_ids.discard(item_id)
            return False

        self._seen_ids.add(item_id)
        return True

    async def mark_seen(self, item_id: str, ttl_seconds: int = 86400) -> None:
        self._seen_ids.add(item_id)
        expire_at = int(time.time()) + max(int(ttl_seconds), 0)
        await self.put(
            self._content_key(item_id),
            {
                "id": item_id,
                "expire_at": expire_at,
                "updated_at": int(time.time()),
            },
        )
        seen_index = await self.get(self.CONTENT_INDEX_KEY, default=[])
        if not isinstance(seen_index, list):
            seen_index = []
        if item_id not in seen_index:
            seen_index.append(item_id)
            await self.put(self.CONTENT_INDEX_KEY, seen_index)

    async def clear_seen(self) -> int:
        seen_index = await self.get(self.CONTENT_INDEX_KEY, default=[])
        if not isinstance(seen_index, list):
            seen_index = []
        deleted = 0
        for item_id in seen_index:
            await self.delete(self._content_key(str(item_id)))
            deleted += 1
        await self.delete(self.CONTENT_INDEX_KEY)
        self._seen_ids.clear()
        return deleted

    async def get_source_state(self, source_id: str) -> dict[str, Any]:
        return await self.get(self._source_state_key(source_id), default={})

    async def update_source_state(
        self,
        source_id: str,
        *,
        creator_name: str | None = None,
        creator_ref: str | None = None,
        last_success_time: int | None = None,
        bootstrap_done: bool | None = None,
    ) -> dict[str, Any]:
        state = await self.get_source_state(source_id)
        if creator_name is not None:
            state["creator_name"] = creator_name
        if creator_ref is not None:
            state["creator_ref"] = creator_ref
        if last_success_time is not None:
            state["last_success_time"] = int(last_success_time)
        if bootstrap_done is not None:
            state["bootstrap_done"] = bool(bootstrap_done)
        await self.put(self._source_state_key(source_id), state)
        return state

    def plugin_cache_dir(self) -> Path:
        if StarTools is not None:
            try:
                return Path(StarTools.get_data_dir(self._plugin_name))
            except Exception:
                pass
        return Path("data") / "plugin_data" / self._plugin_name

    async def _ensure_state_loaded(self) -> None:
        if self._state_loaded:
            return
        self._state_loaded = True
        try:
            if self._state_path.exists():
                with self._state_path.open("r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                if isinstance(loaded, dict):
                    self._disk_state = loaded
        except (OSError, json.JSONDecodeError):
            self._disk_state = {"kv": {}}
        self._disk_state.setdefault("kv", {})

    def _flush_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._state_path.open("w", encoding="utf-8") as fp:
            json.dump(self._disk_state, fp, ensure_ascii=False, sort_keys=True)

    async def _read_raw_from_backend(self, key: str) -> Any:
        if self._get_kv_data is None:
            return self._fallback_store.get(key)
        try:
            return await self._get_kv_data(key, None)
        except TypeError:
            return await self._get_kv_data(key)

    @staticmethod
    def _decode_value(raw: Any) -> Any:
        if raw in (None, ""):
            return None
        if isinstance(raw, dict) and set(raw.keys()) == {"val"}:
            return MemoForwarderStorage._decode_value(raw.get("val"))
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                return raw
            return MemoForwarderStorage._decode_value(decoded)
        return raw

    @staticmethod
    def _is_expired(record: dict[str, Any]) -> bool:
        expire_at = int(record.get("expire_at", 0) or 0)
        return expire_at > 0 and expire_at < int(time.time())

    @classmethod
    def _source_state_key(cls, source_id: str) -> str:
        return f"{cls.SOURCE_STATE_PREFIX}{source_id}"

    @classmethod
    def _content_key(cls, item_id: str) -> str:
        return f"{cls.CONTENT_KEY_PREFIX}{item_id}"
