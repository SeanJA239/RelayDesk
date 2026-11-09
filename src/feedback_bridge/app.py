"""HTTP entry point: Baserow webhooks trigger syncs, a loop reconciles the rest.

Run with: uvicorn feedback_bridge.app:from_env --factory --port 8787
The webhook is only a fast path. The reconcile loop is what guarantees every
ticket is eventually synced, including those whose webhook was lost.
"""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from feedback_bridge.baserow import BaserowStore
from feedback_bridge.gitlab import GitLab
from feedback_bridge.settings import Settings
from feedback_bridge.sync import Syncer

log = logging.getLogger(__name__)


class WebhookItem(BaseModel):
    id: int


class WebhookPayload(BaseModel):
    table_id: int
    event_type: str
    items: list[WebhookItem] = []


async def reconcile_forever(syncer: Syncer, store: BaserowStore, every: float) -> None:
    while True:
        try:
            for ticket_id in await store.unsynced_ticket_ids():
                await syncer.sync(ticket_id, respect_backoff=True)
        except Exception:
            # A down Baserow must not kill the loop; the next pass retries.
            log.exception("reconcile pass failed")
        await asyncio.sleep(every)


def create_app(settings: Settings) -> FastAPI:
    pending: set[asyncio.Task] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with (
            httpx.AsyncClient(
                base_url=settings.baserow_url,
                headers={"Authorization": f"Token {settings.baserow_token}"},
                timeout=30,
            ) as baserow,
            httpx.AsyncClient(
                base_url=settings.gitlab_url,
                headers={"PRIVATE-TOKEN": settings.gitlab_token},
                timeout=30,
            ) as gitlab,
        ):
            store = BaserowStore(baserow, settings.table_tickets, settings.table_sync)
            app.state.syncer = Syncer(
                store,
                GitLab(gitlab),
                base_backoff=timedelta(seconds=settings.base_backoff_seconds),
            )
            loop = asyncio.create_task(
                reconcile_forever(app.state.syncer, store, settings.reconcile_seconds)
            )
            yield
            loop.cancel()
            for task in pending:
                task.cancel()

    app = FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.post("/webhooks/baserow", status_code=status.HTTP_202_ACCEPTED)
    async def baserow_webhook(
        payload: WebhookPayload,
        x_bridge_secret: str = Header(default=""),
    ) -> dict:
        if not hmac.compare_digest(x_bridge_secret, settings.webhook_secret):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED)
        if payload.table_id != settings.table_tickets or payload.event_type != (
            "rows.created"
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "unexpected event"
            )
        # Only the ids are trusted; the ticket itself is re-read from Baserow.
        for item in payload.items:
            task = asyncio.create_task(app.state.syncer.sync(item.id))
            pending.add(task)
            task.add_done_callback(pending.discard)
        return {"queued": [item.id for item in payload.items]}

    return app


def from_env() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return create_app(Settings.from_env())
