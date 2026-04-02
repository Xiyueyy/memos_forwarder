from __future__ import annotations

from astrbot.api.event import AstrMessageEvent


class MemosWorkspaceCommands:
    """Command entrypoints for the Memos workspace forwarder."""

    scheduler = None
    dispatcher = None

    async def memosws_router(self, event: AstrMessageEvent):
        message_text = self._get_message_text(event)
        tokens = message_text.strip().split()
        if not tokens:
            return

        head = tokens[0].lstrip("/").lower()
        if head != "memosws":
            return

        sub = tokens[1].lower() if len(tokens) >= 2 else ""
        route_map = {
            "list": self.memosws_list,
            "status": self.memosws_status,
            "run": self.memosws_run,
            "pause": self.memosws_pause,
            "resume": self.memosws_resume,
            "reset": self.memosws_reset,
            "subscribe": self.memosws_subscribe,
            "unsubscribe": self.memosws_unsubscribe,
            "subscriptions": self.memosws_subscriptions,
            "subs": self.memosws_subscriptions,
        }

        handler = route_map.get(sub)
        if handler is None:
            yield event.plain_result(
                "用法：/memosws [list|status|run [job_id]|pause [job_id]|resume [job_id]|reset|subscribe [job_id]|unsubscribe [job_id]|subscriptions]"
            )
            return

        async for result in handler(event):
            yield result

    async def memosws_list(self, event: AstrMessageEvent):
        scheduler = self.scheduler
        dispatcher = self.dispatcher
        if scheduler is None:
            yield event.plain_result("调度器尚未初始化。")
            return

        config = scheduler.config
        last_results = scheduler.last_results
        paused_jobs = scheduler.paused_jobs

        lines = [
            "Memos 工作区转发概览：",
            f"- sources={len(config.sources)} jobs={len(config.jobs)} targets={len(config.targets)}",
            f"- 调度器：{'运行中' if scheduler.running else '未运行'}",
            f"- 暂停任务：{', '.join(sorted(paused_jobs)) if paused_jobs else '无'}",
            "",
            "任务列表：",
        ]

        for job in config.jobs:
            result = last_results.get(job.id)
            dynamic_count = 0
            if dispatcher is not None:
                dynamic_count = len(await dispatcher.get_job_dynamic_origins(job.id))

            job_status = (
                "已暂停"
                if job.id in paused_jobs
                else ("启用" if job.enabled else "禁用")
            )
            lines.append(
                f"- {job.id} [{job_status}] sources={len(job.source_ids)} "
                f"static_targets={len(job.target_ids)} subscribed_sessions={dynamic_count} "
                f"最近成功={self._format_success_time(result)} 最近错误={self._format_last_error(result)}"
            )

        yield event.plain_result("\n".join(lines))

    async def memosws_status(self, event: AstrMessageEvent):
        scheduler = self.scheduler
        if scheduler is None:
            yield event.plain_result("调度器尚未初始化。")
            return

        config = scheduler.config
        last_results = scheduler.last_results

        success_times = [
            result.started_at for result in last_results.values() if not result.error_summary
        ]
        recent_success = (
            max(success_times).strftime("%Y-%m-%d %H:%M:%S") if success_times else "暂无"
        )
        errors = [result.error_summary for result in last_results.values() if result.error_summary]
        recent_error = errors[-1] if errors else "无"

        lines = [
            "Memos 工作区转发状态：",
            f"- 调度器：{'运行中' if scheduler.running else '未运行'}",
            f"- sources={len(config.sources)} jobs={len(config.jobs)} targets={len(config.targets)}",
            f"- 最近成功：{recent_success}",
            f"- 最近错误：{recent_error}",
        ]
        yield event.plain_result("\n".join(lines))

    async def memosws_run(self, event: AstrMessageEvent):
        scheduler = self.scheduler
        if scheduler is None:
            yield event.plain_result("调度器尚未初始化。")
            return

        job_id = self._extract_param(event)
        ok = await scheduler.run_job_once(job_id=job_id or None)
        if not ok:
            target = job_id or "全部任务"
            yield event.plain_result(f"手动触发失败：未找到或不可执行任务（{target}）")
            return

        if job_id:
            result = scheduler.last_results.get(job_id)
            yield event.plain_result(
                f"已触发任务 {job_id}。最近成功={self._format_success_time(result)} "
                f"最近错误={self._format_last_error(result)}"
            )
            return

        yield event.plain_result("已触发全部启用任务。")

    async def memosws_pause(self, event: AstrMessageEvent):
        scheduler = self.scheduler
        if scheduler is None:
            yield event.plain_result("调度器尚未初始化。")
            return

        job_id = self._extract_param(event)
        if not job_id:
            yield event.plain_result("用法：/memosws pause [job_id]")
            return

        ok = await scheduler.pause_job(job_id)
        if not ok:
            yield event.plain_result(f"暂停失败：任务不存在或未启用（{job_id}）")
            return

        result = scheduler.last_results.get(job_id)
        yield event.plain_result(
            f"任务已暂停：{job_id}。最近成功={self._format_success_time(result)} "
            f"最近错误={self._format_last_error(result)}"
        )

    async def memosws_resume(self, event: AstrMessageEvent):
        scheduler = self.scheduler
        if scheduler is None:
            yield event.plain_result("调度器尚未初始化。")
            return

        job_id = self._extract_param(event)
        if not job_id:
            yield event.plain_result("用法：/memosws resume [job_id]")
            return

        ok = scheduler.resume_job(job_id)
        if not ok:
            yield event.plain_result(f"恢复失败：任务不存在或未启用（{job_id}）")
            return

        result = scheduler.last_results.get(job_id)
        yield event.plain_result(
            f"任务已恢复：{job_id}。最近成功={self._format_success_time(result)} "
            f"最近错误={self._format_last_error(result)}"
        )

    async def memosws_reset(self, event: AstrMessageEvent):
        scheduler = self.scheduler
        if scheduler is None:
            yield event.plain_result("调度器尚未初始化。")
            return

        deleted = await scheduler.storage.clear_seen()
        yield event.plain_result(f"已清空去重记录：{deleted} 条。")

    async def memosws_subscribe(self, event: AstrMessageEvent):
        dispatcher = self.dispatcher
        if dispatcher is None:
            yield event.plain_result("发送器尚未初始化。")
            return

        job_id, error_message = self._resolve_job_id(self._extract_param(event))
        if error_message:
            yield event.plain_result(error_message)
            return

        origin = self._event_origin(event)
        if not origin:
            yield event.plain_result("当前会话没有 unified_msg_origin，无法订阅。")
            return

        normalized_origin = dispatcher.normalize_origin(origin)
        subscribed_jobs = await dispatcher.get_session_subscriptions(origin)
        if job_id in subscribed_jobs:
            yield event.plain_result(f"当前会话已经订阅任务 {job_id}。")
            return

        ok = await dispatcher.subscribe(job_id, origin)
        if not ok:
            yield event.plain_result(f"订阅失败：无法保存任务 {job_id} 的订阅记录。")
            return

        yield event.plain_result(
            f"已订阅任务 {job_id}。\n当前会话：{normalized_origin}"
        )

    async def memosws_unsubscribe(self, event: AstrMessageEvent):
        dispatcher = self.dispatcher
        if dispatcher is None:
            yield event.plain_result("发送器尚未初始化。")
            return

        job_id, error_message = self._resolve_job_id(self._extract_param(event))
        if error_message:
            yield event.plain_result(error_message)
            return

        origin = self._event_origin(event)
        if not origin:
            yield event.plain_result("当前会话没有 unified_msg_origin，无法退订。")
            return

        normalized_origin = dispatcher.normalize_origin(origin)
        subscribed_jobs = await dispatcher.get_session_subscriptions(origin)
        if job_id not in subscribed_jobs:
            yield event.plain_result(f"当前会话没有订阅任务 {job_id}。")
            return

        dynamic_origins = await dispatcher.get_job_dynamic_origins(job_id)
        if normalized_origin not in dynamic_origins:
            yield event.plain_result(
                f"当前会话会收到任务 {job_id}，但它来自静态目标配置。\n"
                "请到面板里的 targets/jobs 配置中移除，不能用 unsubscribe 删除。"
            )
            return

        ok = await dispatcher.unsubscribe(job_id, origin)
        if not ok:
            yield event.plain_result(f"退订失败：无法更新任务 {job_id} 的订阅记录。")
            return

        yield event.plain_result(
            f"已退订任务 {job_id}。\n当前会话：{normalized_origin}"
        )

    async def memosws_subscriptions(self, event: AstrMessageEvent):
        dispatcher = self.dispatcher
        if dispatcher is None:
            yield event.plain_result("发送器尚未初始化。")
            return

        origin = self._event_origin(event)
        if not origin:
            yield event.plain_result("当前会话没有 unified_msg_origin，无法读取订阅。")
            return

        normalized_origin = dispatcher.normalize_origin(origin)
        job_ids = await dispatcher.get_session_subscriptions(origin)
        if not job_ids:
            yield event.plain_result(
                f"当前会话还没有订阅任何任务。\n当前会话：{normalized_origin}"
            )
            return

        lines = [
            f"当前会话：{normalized_origin}",
            "已订阅任务：",
        ]
        lines.extend(f"- {job_id}" for job_id in job_ids)
        yield event.plain_result("\n".join(lines))

    def _resolve_job_id(self, requested_job_id: str) -> tuple[str | None, str | None]:
        dispatcher = self.dispatcher
        enabled_job_ids = []
        if dispatcher is not None:
            enabled_job_ids = dispatcher.enabled_job_ids()
        elif self.scheduler is not None:
            enabled_job_ids = [job.id for job in self.scheduler.config.jobs if job.enabled]

        if requested_job_id:
            if requested_job_id in enabled_job_ids:
                return requested_job_id, None
            return None, f"任务不存在或未启用：{requested_job_id}"

        if len(enabled_job_ids) == 1:
            return enabled_job_ids[0], None
        if not enabled_job_ids:
            return None, "当前没有可用的启用任务。"

        return None, "当前有多个启用任务，请显式指定 job_id。可用任务：" + ", ".join(enabled_job_ids)

    @staticmethod
    def _extract_param(event: AstrMessageEvent) -> str:
        message_text = MemosWorkspaceCommands._get_message_text(event)
        tokens = message_text.strip().split()
        return tokens[2].strip() if len(tokens) >= 3 else ""

    @staticmethod
    def _get_message_text(event: AstrMessageEvent) -> str:
        if hasattr(event, "message_str"):
            return str(getattr(event, "message_str") or "")
        if hasattr(event, "get_message_str"):
            getter = getattr(event, "get_message_str")
            return str(getter() if callable(getter) else getter or "")
        return ""

    @staticmethod
    def _event_origin(event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "").strip()

    @staticmethod
    def _format_success_time(result) -> str:
        if result is None or result.error_summary:
            return "暂无"
        return result.started_at.strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _format_last_error(result) -> str:
        if result is None or not result.error_summary:
            return "无"
        return result.error_summary
