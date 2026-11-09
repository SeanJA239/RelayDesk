"""Thin Baserow REST client shared by the pilot scripts and tests."""

from pathlib import Path
from typing import Any

import httpx

ENV_FILE = Path(__file__).resolve().parents[2] / ".env.local"


def load_env() -> dict[str, str]:
    lines = ENV_FILE.read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def save_env(values: dict[str, Any]) -> None:
    env = load_env() if ENV_FILE.exists() else {}
    env.update({k: str(v) for k, v in values.items()})
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    ENV_FILE.chmod(0o600)


class BaserowError(RuntimeError):
    def __init__(self, response: httpx.Response) -> None:
        self.status = response.status_code
        self.body = response.text
        super().__init__(
            f"{response.request.method} {response.request.url} -> "
            f"{self.status}: {self.body}"
        )


class Api:
    """Calls `/api` on a Baserow instance, optionally as an authenticated admin."""

    def __init__(self, base_url: str, jwt: str | None = None) -> None:
        headers = {"Authorization": f"JWT {jwt}"} if jwt else {}
        self.http = httpx.Client(
            base_url=f"{base_url}/api", headers=headers, timeout=60
        )

    @classmethod
    def admin(cls, env: dict[str, str]) -> "Api":
        anon = cls(env["BASEROW_URL"])
        token = anon.call(
            "POST",
            "/user/token-auth/",
            {"email": env["ADMIN_EMAIL"], "password": env["ADMIN_PASSWORD"]},
        )["access_token"]
        return cls(env["BASEROW_URL"], token)

    def raw(self, method: str, path: str, body: Any = None) -> httpx.Response:
        return self.http.request(method, path, json=body)

    def call(self, method: str, path: str, body: Any = None) -> Any:
        response = self.raw(method, path, body)
        if response.is_error:
            raise BaserowError(response)
        return response.json() if response.content else None
