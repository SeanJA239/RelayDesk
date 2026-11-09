"""Bridge configuration, validated once at startup from the environment."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    baserow_url: str
    baserow_token: str
    table_tickets: int
    table_sync: int
    gitlab_url: str
    gitlab_token: str
    webhook_secret: str
    reconcile_seconds: float = 60
    base_backoff_seconds: float = 60

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        missing = [
            k
            for k in (
                "BASEROW_URL",
                "BRIDGE_BASEROW_TOKEN",
                "TABLE_TICKETS",
                "TABLE_SYNC",
                "GITLAB_URL",
                "GITLAB_BRIDGE_TOKEN",
                "BRIDGE_WEBHOOK_SECRET",
            )
            if not env.get(k)
        ]
        if missing:
            raise RuntimeError(f"missing bridge settings: {', '.join(missing)}")
        return cls(
            baserow_url=env["BASEROW_URL"],
            baserow_token=env["BRIDGE_BASEROW_TOKEN"],
            table_tickets=int(env["TABLE_TICKETS"]),
            table_sync=int(env["TABLE_SYNC"]),
            gitlab_url=env["GITLAB_URL"],
            gitlab_token=env["GITLAB_BRIDGE_TOKEN"],
            webhook_secret=env["BRIDGE_WEBHOOK_SECRET"],
            reconcile_seconds=float(env.get("BRIDGE_RECONCILE_SECONDS", 60)),
            base_backoff_seconds=float(env.get("BRIDGE_BASE_BACKOFF_SECONDS", 60)),
        )
