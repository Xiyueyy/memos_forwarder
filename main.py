from __future__ import annotations

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .card_renderer import MemoCardRenderer
from .commands import MemosWorkspaceCommands
from .config import MemosWorkspaceForwarderConfig
from .dispatcher import MemoDispatcher
from .fetcher import MemosFetcher
from .scheduler import MemosWorkspaceScheduler
from .storage import MemoForwarderStorage


@register(
    "astrbot_plugin_memos_workspace_forwarder",
    "Codex",
    "Forward authenticated Memos memos to AstrBot targets with card rendering.",
    "0.2.0",
    "",
)
class MemosWorkspaceForwarderPlugin(Star, MemosWorkspaceCommands):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context, config)

        runtime_source = config if config is not None else context
        plugin_config = MemosWorkspaceForwarderConfig.from_context(runtime_source)
        storage = MemoForwarderStorage(
            plugin_name="astrbot_plugin_memos_workspace_forwarder",
            get_kv_data=getattr(self, "get_kv_data", None),
            put_kv_data=getattr(self, "put_kv_data", None),
            delete_kv_data=getattr(self, "delete_kv_data", None),
        )
        fetcher = MemosFetcher(config=plugin_config, storage=storage)
        renderer = MemoCardRenderer(plugin_config, storage.plugin_cache_dir())
        dispatcher = MemoDispatcher(context=context, config=plugin_config, renderer=renderer)

        self.scheduler = MemosWorkspaceScheduler(
            config=plugin_config,
            fetcher=fetcher,
            dispatcher=dispatcher,
            storage=storage,
        )

    async def initialize(self):
        await self.scheduler.start()

    async def terminate(self):
        await self.scheduler.stop()

    @filter.regex(r"^/?memosws(?:\s+.*)?$")
    async def _memosws_router(self, event: AstrMessageEvent):
        async for result in MemosWorkspaceCommands.memosws_router(self, event):
            yield result
