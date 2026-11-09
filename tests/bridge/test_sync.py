"""Sync guarantees: one issue per ticket under duplicates, crashes and outages."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from feedback_bridge.baserow import (
    Attachment,
    SyncState,
    Ticket,
    TicketGone,
    TicketNotReady,
)
from feedback_bridge.gitlab import Issue
from feedback_bridge.sync import Outcome, Syncer, fenced

T0 = datetime(2026, 11, 9, tzinfo=UTC)

TICKET = Ticket(
    row_id=7,
    number=12,
    title="导出卡死",
    body="点击导出无响应",
    unit="示例单位甲",
    project="customer-feedback/unit-a",
    submitter="a1 <a1@customer.example>",
    submitted_at="2026-11-09T10:00:00Z",
)


class FakeStore:
    def __init__(self, ticket: Ticket | Exception = TICKET) -> None:
        self._ticket = ticket
        self.rows: dict[int, dict] = {}
        self.fail_next_save = False
        self.files = {"https://files/log.txt": b"log"}

    async def ticket(self, row_id: int) -> Ticket:
        await asyncio.sleep(0)
        if isinstance(self._ticket, Exception):
            raise self._ticket
        return self._ticket

    async def sync_state(self, ticket_id: int) -> SyncState:
        await asyncio.sleep(0)
        row = self.rows.get(ticket_id)
        if row is None:
            return SyncState()
        return SyncState(
            row_id=ticket_id,
            status=row["状态"],
            issue_iid=row.get("Issue IID"),
            attempts=row["尝试次数"],
            updated_at=row["更新时间"],
        )

    async def save_sync(self, ticket_id: int, state: SyncState, **values) -> None:
        await asyncio.sleep(0)
        if self.fail_next_save:
            self.fail_next_save = False
            raise ConnectionError("baserow unreachable")
        self.rows[ticket_id] = {**self.rows.get(ticket_id, {}), **values}
        self.rows[ticket_id]["更新时间"] = self.now
        state.row_id = ticket_id

    async def download(self, url: str) -> bytes:
        return self.files[url]

    now = T0


class FakeGitLab:
    def __init__(self) -> None:
        self.issues: dict[str, list[dict]] = {}
        self.down = False
        self.uploads: list[str] = []

    def _check(self) -> None:
        if self.down:
            raise ConnectionError("gitlab unreachable")

    async def find_by_title_prefix(self, project: str, prefix: str) -> Issue | None:
        await asyncio.sleep(0)
        self._check()
        for issue in self.issues.get(project, []):
            if issue["title"].startswith(prefix):
                return Issue(issue["iid"], issue["url"])
        return None

    async def create_issue(self, project: str, title: str, description: str) -> Issue:
        await asyncio.sleep(0)
        self._check()
        issues = self.issues.setdefault(project, [])
        iid = len(issues) + 1
        issues.append(
            {"iid": iid, "title": title, "description": description, "url": f"u/{iid}"}
        )
        return Issue(iid, f"u/{iid}")

    async def upload(self, project: str, name: str, content: bytes) -> str:
        self._check()
        self.uploads.append(name)
        return f"[{name}](/uploads/{name})"


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def gitlab() -> FakeGitLab:
    return FakeGitLab()


@pytest.fixture
def syncer(store, gitlab) -> Syncer:
    return Syncer(store, gitlab, now=lambda: store.now)


def issues(gitlab: FakeGitLab) -> list[dict]:
    return gitlab.issues.get(TICKET.project, [])


async def test_creates_issue_and_records_it(syncer, store, gitlab):
    assert await syncer.sync(TICKET.row_id) is Outcome.CREATED
    (issue,) = issues(gitlab)
    assert issue["title"] == "[FB-12] 导出卡死"
    assert "示例单位甲" in issue["description"]
    assert store.rows[TICKET.row_id] | {"更新时间": None} == {
        "状态": "已同步",
        "尝试次数": 1,
        "最近错误": "",
        "Issue IID": 1,
        "Issue 链接": "u/1",
        "更新时间": None,
    }


async def test_concurrent_duplicate_triggers_create_one_issue(syncer, gitlab):
    outcomes = await asyncio.gather(*(syncer.sync(TICKET.row_id) for _ in range(5)))
    assert len(issues(gitlab)) == 1
    assert sorted(outcomes) == sorted([Outcome.CREATED] + [Outcome.ALREADY_SYNCED] * 4)


async def test_replayed_trigger_after_sync_is_a_no_op(syncer, gitlab):
    await syncer.sync(TICKET.row_id)
    assert await syncer.sync(TICKET.row_id) is Outcome.ALREADY_SYNCED
    assert len(issues(gitlab)) == 1


async def test_crash_between_create_and_record_does_not_duplicate(
    syncer, store, gitlab
):
    store.fail_next_save = True
    with pytest.raises(ConnectionError):
        await syncer.sync(TICKET.row_id)
    assert len(issues(gitlab)) == 1
    assert TICKET.row_id not in store.rows

    assert await syncer.sync(TICKET.row_id) is Outcome.ADOPTED
    assert len(issues(gitlab)) == 1
    assert store.rows[TICKET.row_id]["Issue IID"] == 1


async def test_gitlab_outage_is_recorded_then_recovered(syncer, store, gitlab):
    gitlab.down = True
    assert await syncer.sync(TICKET.row_id) is Outcome.FAILED
    row = store.rows[TICKET.row_id]
    assert row["状态"] == "失败"
    assert row["尝试次数"] == 1
    assert "gitlab unreachable" in row["最近错误"]

    gitlab.down = False
    assert await syncer.sync(TICKET.row_id) is Outcome.CREATED
    assert store.rows[TICKET.row_id]["尝试次数"] == 2
    assert len(issues(gitlab)) == 1


async def test_reconcile_backs_off_exponentially(syncer, store, gitlab):
    gitlab.down = True
    await syncer.sync(TICKET.row_id)  # attempt 1 at T0
    store.now = T0 + timedelta(seconds=59)
    assert await syncer.sync(TICKET.row_id, respect_backoff=True) is Outcome.DEFERRED
    store.now = T0 + timedelta(minutes=1)
    assert await syncer.sync(TICKET.row_id, respect_backoff=True) is Outcome.FAILED
    store.now += timedelta(minutes=1, seconds=59)
    assert await syncer.sync(TICKET.row_id, respect_backoff=True) is Outcome.DEFERRED
    store.now += timedelta(seconds=1)
    gitlab.down = False
    assert await syncer.sync(TICKET.row_id, respect_backoff=True) is Outcome.CREATED


async def test_webhook_path_ignores_backoff(syncer, store, gitlab):
    gitlab.down = True
    await syncer.sync(TICKET.row_id)
    gitlab.down = False
    assert await syncer.sync(TICKET.row_id) is Outcome.CREATED


async def test_unit_without_project_is_reported_not_skipped(gitlab):
    store = FakeStore(TicketNotReady("单位「示例单位乙」未配置 GitLab 项目"))
    syncer = Syncer(store, gitlab, now=lambda: store.now)
    assert await syncer.sync(TICKET.row_id) is Outcome.FAILED
    assert "未配置 GitLab 项目" in store.rows[TICKET.row_id]["最近错误"]
    assert gitlab.issues == {}


async def test_attachments_are_uploaded_and_linked(gitlab):
    ticket = replace(
        TICKET, attachments=(Attachment("log.txt", "https://files/log.txt"),)
    )
    store = FakeStore(ticket)
    await Syncer(store, gitlab, now=lambda: store.now).sync(ticket.row_id)
    assert gitlab.uploads == ["log.txt"]
    assert "[log.txt](/uploads/log.txt)" in issues(gitlab)[0]["description"]


@pytest.mark.parametrize(
    "text",
    ["@all 请看 #1 和 !2", "```\n@root\n```", "````破坏 fence", ""],
)
def test_customer_text_is_quoted_verbatim(text):
    block = fenced(text)
    fence = block.splitlines()[0].removesuffix("text")
    inner = block.removeprefix(fence + "text\n").removesuffix("\n" + fence)
    assert inner == text
    assert fence not in text


async def test_deleted_ticket_leaves_no_trace(gitlab):
    store = FakeStore(TicketGone(TICKET.row_id))
    syncer = Syncer(store, gitlab, now=lambda: store.now)
    assert await syncer.sync(TICKET.row_id) is Outcome.GONE
    assert store.rows == {}
    assert gitlab.issues == {}
