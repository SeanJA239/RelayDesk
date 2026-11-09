"""Create exactly one GitLab issue per ticket, however often a sync is triggered.

Idempotency does not rely on the trigger: webhooks may repeat or be lost, and the
process may die between creating an issue and recording it. Before creating, the
issue is looked up by its title prefix `[FB-<编号>]`, so a retry adopts an issue
created by an earlier, interrupted attempt. Assumes one bridge process; the
per-ticket lock only serialises triggers within that process.
"""

import asyncio
import logging
import re
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from feedback_bridge.baserow import SyncState, Ticket, TicketGone
from feedback_bridge.gitlab import Issue

log = logging.getLogger(__name__)


class Store(Protocol):
    async def ticket(self, row_id: int) -> Ticket: ...
    async def sync_state(self, ticket_id: int) -> SyncState: ...
    async def save_sync(self, ticket_id: int, state: SyncState, **values) -> None: ...
    async def download(self, url: str) -> bytes: ...


class IssueTracker(Protocol):
    async def find_by_title_prefix(self, project: str, prefix: str) -> Issue | None: ...
    async def create_issue(
        self, project: str, title: str, description: str
    ) -> Issue: ...
    async def upload(self, project: str, name: str, content: bytes) -> str: ...


class Outcome(StrEnum):
    CREATED = "created"
    ADOPTED = "adopted"
    ALREADY_SYNCED = "already_synced"
    DEFERRED = "deferred"
    FAILED = "failed"
    GONE = "gone"


def fenced(text: str) -> str:
    """Quote customer text verbatim so it cannot mention users or reference issues."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


class Syncer:
    def __init__(
        self,
        store: Store,
        gitlab: IssueTracker,
        *,
        base_backoff: timedelta = timedelta(minutes=1),
        max_backoff: timedelta = timedelta(hours=1),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.gitlab = gitlab
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.now = now
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def _retry_due(self, state: SyncState) -> bool:
        if state.attempts == 0 or state.updated_at is None:
            return True
        wait = min(self.base_backoff * 2 ** (state.attempts - 1), self.max_backoff)
        return self.now() >= state.updated_at + wait

    async def sync(self, ticket_id: int, *, respect_backoff: bool = False) -> Outcome:
        async with self._locks[ticket_id]:
            state = await self.store.sync_state(ticket_id)
            if state.synced:
                return Outcome.ALREADY_SYNCED
            if respect_backoff and not self._retry_due(state):
                return Outcome.DEFERRED
            attempts = state.attempts + 1
            try:
                outcome, issue = await self._ensure_issue(ticket_id)
            except TicketGone:
                log.info("ticket %s was deleted before sync", ticket_id)
                return Outcome.GONE
            except Exception as exc:
                # Recorded on the ticket and retried by the reconcile loop.
                log.exception("sync of ticket %s failed", ticket_id)
                await self.store.save_sync(
                    ticket_id,
                    state,
                    状态="失败",
                    尝试次数=attempts,
                    最近错误=f"{type(exc).__name__}: {exc}"[:2000],
                )
                return Outcome.FAILED
            await self.store.save_sync(
                ticket_id,
                state,
                状态="已同步",
                尝试次数=attempts,
                最近错误="",
                **{"Issue IID": issue.iid, "Issue 链接": issue.url},
            )
            log.info("ticket %s %s issue %s", ticket_id, outcome, issue.url)
            return outcome

    async def _ensure_issue(self, ticket_id: int) -> tuple[Outcome, Issue]:
        ticket = await self.store.ticket(ticket_id)
        prefix = f"[{ticket.key}]"
        existing = await self.gitlab.find_by_title_prefix(ticket.project, prefix)
        if existing is not None:
            return Outcome.ADOPTED, existing
        issue = await self.gitlab.create_issue(
            ticket.project, f"{prefix} {ticket.title}", await self._describe(ticket)
        )
        return Outcome.CREATED, issue

    async def _describe(self, ticket: Ticket) -> str:
        lines = [
            f"**工单**：{ticket.key}",
            f"**单位**：{ticket.unit}",
            f"**提交人**：{ticket.submitter}",
            f"**提交时间**：{ticket.submitted_at}",
            "",
            "**问题描述**（客户原文）：",
            "",
            fenced(ticket.body),
        ]
        if ticket.attachments:
            lines += ["", "**附件**："]
            for attachment in ticket.attachments:
                content = await self.store.download(attachment.url)
                link = await self.gitlab.upload(
                    ticket.project, attachment.name, content
                )
                lines.append(f"- {link}")
        return "\n".join(lines)
