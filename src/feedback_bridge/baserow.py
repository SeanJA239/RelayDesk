"""Baserow side of the bridge: read tickets, read and write their sync state."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx


@dataclass(frozen=True)
class Attachment:
    name: str
    url: str


@dataclass(frozen=True)
class Ticket:
    row_id: int
    number: int
    title: str
    body: str
    unit: str
    project: str
    submitter: str
    submitted_at: str
    attachments: tuple[Attachment, ...] = ()

    @property
    def key(self) -> str:
        """Customer-visible id, also the idempotency marker in the issue title."""
        return f"FB-{self.number}"


@dataclass
class SyncState:
    row_id: int | None = None
    status: str | None = None
    issue_iid: int | None = None
    attempts: int = 0
    updated_at: datetime | None = None

    @property
    def synced(self) -> bool:
        return self.status == "已同步" and self.issue_iid is not None


class TicketNotReady(Exception):
    """The ticket lacks data needed to sync, e.g. its unit has no GitLab project."""


class TicketGone(Exception):
    """The ticket row was deleted before it could be synced."""


class BaserowStore:
    def __init__(
        self, http: httpx.AsyncClient, table_tickets: int, table_sync: int
    ) -> None:
        self.http = http
        self.table_tickets = table_tickets
        self.table_sync = table_sync
        self._link_targets: dict[str, int] = {}

    async def _get(self, path: str, **params) -> Any:
        response = await self.http.get(
            path, params={"user_field_names": "true", **params}
        )
        response.raise_for_status()
        return response.json()

    async def _row(self, table_id: int, row_id: int) -> dict:
        return await self._get(f"/api/database/rows/table/{table_id}/{row_id}/")

    async def ticket(self, row_id: int) -> Ticket:
        try:
            row = await self._row(self.table_tickets, row_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == httpx.codes.NOT_FOUND:
                raise TicketGone(row_id) from exc
            raise
        if len(row["单位"]) != 1 or len(row["提交人"]) != 1:
            raise TicketNotReady("工单必须恰好关联一个单位和一个提交人")
        unit_link, submitter_link = row["单位"][0], row["提交人"][0]
        unit = await self._linked_row("单位", unit_link["id"])
        submitter = await self._linked_row("提交人", submitter_link["id"])
        if not unit.get("GitLab项目"):
            raise TicketNotReady(f"单位「{unit['名称']}」未配置 GitLab 项目")
        return Ticket(
            row_id=row["id"],
            number=int(row["编号"]),
            title=row["标题"],
            body=row["问题描述"] or "",
            unit=unit["名称"],
            project=unit["GitLab项目"],
            submitter=f"{submitter['姓名']} <{submitter['邮箱']}>",
            submitted_at=row["提交时间"],
            attachments=tuple(
                Attachment(f["visible_name"], f["url"]) for f in row["附件"] or []
            ),
        )

    async def _linked_row(self, link_field: str, row_id: int) -> dict:
        table_id = await self._link_target(link_field)
        return await self._row(table_id, row_id)

    async def _link_target(self, link_field: str) -> int:
        if not self._link_targets:
            fields = await self._get(
                f"/api/database/fields/table/{self.table_tickets}/"
            )
            self._link_targets = {
                f["name"]: f["link_row_table_id"]
                for f in fields
                if f["type"] == "link_row"
            }
        return self._link_targets[link_field]

    async def sync_state(self, ticket_id: int) -> SyncState:
        rows = (
            await self._get(
                f"/api/database/rows/table/{self.table_sync}/",
                **{"filter__工单__link_row_has": ticket_id},
            )
        )["results"]
        if len(rows) > 1:
            raise RuntimeError(f"ticket {ticket_id} has {len(rows)} sync rows")
        if not rows:
            return SyncState()
        row = rows[0]
        return SyncState(
            row_id=row["id"],
            status=(row["状态"] or {}).get("value"),
            issue_iid=int(row["Issue IID"]) if row["Issue IID"] else None,
            attempts=int(row["尝试次数"] or 0),
            updated_at=datetime.fromisoformat(row["更新时间"]),
        )

    async def save_sync(self, ticket_id: int, state: SyncState, **values) -> None:
        payload = {"工单": [ticket_id], **values}
        path = f"/api/database/rows/table/{self.table_sync}/"
        if state.row_id is None:
            response = await self.http.post(
                path, params={"user_field_names": "true"}, json=payload
            )
        else:
            response = await self.http.patch(
                f"{path}{state.row_id}/",
                params={"user_field_names": "true"},
                json=payload,
            )
        response.raise_for_status()
        state.row_id = response.json()["id"]

    async def _all_rows(self, table_id: int) -> list[dict]:
        rows, page = [], 1
        while True:
            data = await self._get(
                f"/api/database/rows/table/{table_id}/", page=page, size=200
            )
            rows += data["results"]
            if not data["next"]:
                return rows
            page += 1

    async def unsynced_ticket_ids(self) -> list[int]:
        """Tickets without an 已同步 sync row, oldest first."""
        synced = {
            link["id"]
            for row in await self._all_rows(self.table_sync)
            if (row["状态"] or {}).get("value") == "已同步"
            for link in row["工单"]
        }
        tickets = await self._all_rows(self.table_tickets)
        return sorted(row["id"] for row in tickets if row["id"] not in synced)

    async def download(self, url: str) -> bytes:
        response = await self.http.get(url)
        response.raise_for_status()
        return response.content
