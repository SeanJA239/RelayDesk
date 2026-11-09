"""Live fixtures: drive the published portal as customers, inspect as admin.

Needs `docker compose --env-file .env.local up -d` and `scripts/run_bridge.sh`
(with BRIDGE_RECONCILE_SECONDS=2 BRIDGE_BASE_BACKOFF_SECONDS=2 for fast retries).
Everything public is resolved by name from the published copy of the app.
"""

import time
from collections.abc import Callable, Iterator

import httpx
import pytest

from feedback_pilot.client import Api, load_env

pytestmark = pytest.mark.live

LOGINS = ("a1", "a2", "b1", "b2")
UNIT_OF = {
    "a1": "示例单位甲",
    "a2": "示例单位甲",
    "b1": "示例单位乙",
    "b2": "示例单位乙",
}
PROJECT_OF = {
    "示例单位甲": "customer-feedback/unit-a",
    "示例单位乙": "customer-feedback/unit-b",
}


def email(login: str) -> str:
    return f"{login}@customer.example"


class Portal:
    def __init__(self, env: dict[str, str]) -> None:
        self.env = env
        self.anon = Api(env["BASEROW_URL"])
        builder = self.anon.call(
            "GET", f"/builder/domains/published/by_name/{env['PORTAL_DOMAIN']}/"
        )
        (user_source,) = builder["user_sources"]
        self.user_source = user_source["id"]
        self.page = next(p["id"] for p in builder["pages"] if p["name"] == "提交工单")
        user = self.login("a1")
        elements = user.call("GET", self._published("elements"))
        self.title_input, self.body_input = (
            e["id"] for e in elements if e["type"] == "input_text"
        )
        actions = user.call("GET", self._published("workflow_actions"))
        self.create_action = next(a["id"] for a in actions if a["type"] == "create_row")
        self.account_ds = user.call("GET", self._published("data_sources"))[0]["id"]

    def _published(self, kind: str) -> str:
        return f"/builder/domains/published/page/{self.page}/{kind}/"

    def token_auth(self, login: str, password: str | None = None) -> httpx.Response:
        secret = self.env[f"PASSWORD_{login.upper()}"] if password is None else password
        return self.anon.raw(
            "POST",
            f"/user-source/{self.user_source}/token-auth",
            {"email": email(login), "password": secret},
        )

    def login(self, login: str) -> Api:
        response = self.token_auth(login)
        response.raise_for_status()
        return Api(self.env["BASEROW_URL"], response.json()["access_token"])

    def submit(self, client: Api, title: str, body: str = "描述", **metadata):
        form = {str(self.title_input): title, str(self.body_input): body}
        return client.raw(
            "POST",
            f"/builder/workflow_action/{self.create_action}/dispatch/",
            {"metadata": {"form_data": form, **metadata}},
        )

    def account(self, client: Api, **metadata) -> httpx.Response:
        return client.raw(
            "POST",
            f"/builder/domains/published/data-source/{self.account_ds}/dispatch/",
            {"metadata": metadata},
        )

    def published_config(self, client: Api) -> str:
        return "".join(
            client.raw("GET", self._published(kind)).text
            for kind in ("elements", "data_sources", "workflow_actions")
        )


@pytest.fixture(scope="session")
def env() -> dict[str, str]:
    return load_env()


@pytest.fixture(scope="session")
def admin(env) -> Api:
    return Api.admin(env)


@pytest.fixture(scope="session")
def portal(env) -> Portal:
    return Portal(env)


@pytest.fixture(scope="session")
def gitlab(env) -> httpx.Client:
    return httpx.Client(
        base_url=f"{env['GITLAB_URL']}/api/v4",
        headers={"PRIVATE-TOKEN": env["GITLAB_ADMIN_TOKEN"]},
        timeout=60,
    )


@pytest.fixture
def rows(admin, env) -> Callable[[str], list[dict]]:
    def read(table: str) -> list[dict]:
        return admin.call(
            "GET",
            f"/database/rows/table/{env[table]}/?user_field_names=true&size=200",
        )["results"]

    return read


def project_issues(gitlab: httpx.Client, project: str) -> list[dict]:
    response = gitlab.get(
        f"/projects/{project.replace('/', '%2F')}/issues", params={"per_page": 100}
    )
    response.raise_for_status()
    return response.json()


@pytest.fixture
def issues(gitlab) -> Callable[[str], list[dict]]:
    return lambda project: project_issues(gitlab, project)


@pytest.fixture(autouse=True)
def cleanup(admin, env, rows, gitlab) -> Iterator[None]:
    """Remove tickets, sync rows and issues a test creates; restore accounts."""
    before = {t: {r["id"] for r in rows(t)} for t in ("TABLE_TICKETS", "TABLE_SYNC")}
    issues_before = {
        p: {i["iid"] for i in project_issues(gitlab, p)} for p in PROJECT_OF.values()
    }
    accounts = rows("TABLE_ACCOUNTS")
    units = rows("TABLE_UNITS")
    yield
    # The bridge syncs asynchronously; let it settle so no issue appears after
    # the cleanup below has run.
    new_tickets = {
        r["id"] for r in rows("TABLE_TICKETS") if r["id"] not in before["TABLE_TICKETS"]
    }

    def settled() -> bool:
        done = {
            link["id"]
            for r in rows("TABLE_SYNC")
            if (r["状态"] or {}).get("value") in ("已同步", "失败")
            for link in r["工单"]
        }
        return new_tickets <= done

    wait_for(settled)
    for table in ("TABLE_SYNC", "TABLE_TICKETS"):
        new = [r["id"] for r in rows(table) if r["id"] not in before[table]]
        if new:
            admin.call(
                "POST",
                f"/database/rows/table/{env[table]}/batch-delete/",
                {"items": new},
            )
    for project, iids in issues_before.items():
        for issue in project_issues(gitlab, project):
            if issue["iid"] not in iids:
                gitlab.delete(
                    f"/projects/{project.replace('/', '%2F')}/issues/{issue['iid']}"
                ).raise_for_status()
    current = {r["id"] for r in rows("TABLE_ACCOUNTS")}
    missing = [a for a in accounts if a["id"] not in current]
    if missing:
        raise AssertionError(f"test deleted accounts it did not restore: {missing}")
    admin.call(
        "PATCH",
        f"/database/rows/table/{env['TABLE_ACCOUNTS']}/batch/?user_field_names=true",
        {"items": [{"id": a["id"], "角色": a["角色"]["value"]} for a in accounts]},
    )
    admin.call(
        "PATCH",
        f"/database/rows/table/{env['TABLE_UNITS']}/batch/?user_field_names=true",
        {"items": [{"id": u["id"], "GitLab项目": u["GitLab项目"]} for u in units]},
    )


def wait_for(predicate: Callable[[], object], timeout: float = 30) -> object:
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        time.sleep(0.5)
