"""Submit → GitLab issue, exactly once, through duplicates, outages and lost events."""

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from conftest import PROJECT_OF, wait_for

BRIDGE = "http://localhost:8787"


def submit(portal, rows, login: str, title: str) -> dict:
    response = portal.submit(portal.login(login), title)
    assert response.is_success, response.text
    return next(r for r in rows("TABLE_TICKETS") if r["标题"] == title)


def sync_row(rows, ticket: dict) -> dict | None:
    matches = [
        r for r in rows("TABLE_SYNC") if [t["id"] for t in r["工单"]] == [ticket["id"]]
    ]
    assert len(matches) <= 1, matches
    return matches[0] if matches else None


def status(rows, ticket: dict) -> str | None:
    row = sync_row(rows, ticket)
    return row and (row["状态"] or {}).get("value")


def issues_for(issues, ticket: dict, project: str) -> list[dict]:
    prefix = f"[FB-{ticket['编号']}]"
    return [i for i in issues(project) if i["title"].startswith(prefix)]


def wait_synced(rows, ticket: dict, timeout: float = 30) -> dict:
    wait_for(lambda: status(rows, ticket) == "已同步", timeout)
    return sync_row(rows, ticket)


def webhook(env, ticket_id: int, secret: str | None = None, table: str | None = None):
    return httpx.post(
        f"{BRIDGE}/webhooks/baserow",
        headers={
            "X-Bridge-Secret": env["BRIDGE_WEBHOOK_SECRET"]
            if secret is None
            else secret
        },
        json={
            "table_id": int(table or env["TABLE_TICKETS"]),
            "event_type": "rows.created",
            "items": [{"id": ticket_id}],
        },
    )


def test_submit_creates_exactly_one_issue(portal, rows, issues):
    ticket = submit(portal, rows, "b2", "同步：正常提交")
    row = wait_synced(rows, ticket)
    project = PROJECT_OF["示例单位乙"]
    (issue,) = issues_for(issues, ticket, project)
    assert int(row["Issue IID"]) == issue["iid"]
    assert row["Issue 链接"] == issue["web_url"]
    assert issue["title"] == f"[FB-{ticket['编号']}] 同步：正常提交"
    assert "示例单位乙-b2 <b2@customer.example>" in issue["description"]
    assert issues_for(issues, ticket, PROJECT_OF["示例单位甲"]) == []


def test_replayed_and_concurrent_webhooks_do_not_duplicate(portal, env, rows, issues):
    ticket = submit(portal, rows, "a1", "同步：重复事件")
    with ThreadPoolExecutor(8) as pool:
        responses = list(pool.map(lambda _: webhook(env, ticket["id"]), range(8)))
    assert all(r.status_code == 202 for r in responses)
    wait_synced(rows, ticket)
    for _ in range(3):
        assert webhook(env, ticket["id"]).status_code == 202
    # Replays are handled in background tasks; give a duplicate time to appear.
    time.sleep(3)
    assert len(issues_for(issues, ticket, PROJECT_OF["示例单位甲"])) == 1
    assert int(sync_row(rows, ticket)["尝试次数"]) == 1


@pytest.mark.parametrize("secret", ["", "wrong"], ids=["missing", "wrong"])
def test_webhook_requires_shared_secret(env, secret):
    assert webhook(env, 1, secret=secret).status_code == 401


def test_webhook_rejects_other_tables(env):
    assert webhook(env, 1, table=env["TABLE_ACCOUNTS"]).status_code == 422


def test_bad_project_mapping_fails_visibly_then_recovers(
    portal, admin, env, rows, issues
):
    unit = next(u for u in rows("TABLE_UNITS") if u["名称"] == "示例单位甲")
    patch = (
        f"/database/rows/table/{env['TABLE_UNITS']}/{unit['id']}/?user_field_names=true"
    )
    admin.call("PATCH", patch, {"GitLab项目": "customer-feedback/does-not-exist"})

    ticket = submit(portal, rows, "a2", "同步：项目映射错误")
    wait_for(lambda: status(rows, ticket) == "失败")
    assert "404" in sync_row(rows, ticket)["最近错误"]

    admin.call("PATCH", patch, {"GitLab项目": unit["GitLab项目"]})
    row = wait_synced(rows, ticket)
    assert row["最近错误"] == ""
    assert len(issues_for(issues, ticket, unit["GitLab项目"])) == 1


def test_gitlab_outage_fails_visibly_then_recovers(portal, rows, issues):
    subprocess.run(["docker", "pause", "feedback-gitlab"], check=True)
    try:
        ticket = submit(portal, rows, "b1", "同步：GitLab 中断")
        wait_for(lambda: status(rows, ticket) == "失败", timeout=60)
        assert "Timeout" in sync_row(rows, ticket)["最近错误"]
    finally:
        subprocess.run(["docker", "unpause", "feedback-gitlab"], check=True)
    wait_synced(rows, ticket, timeout=60)
    assert len(issues_for(issues, ticket, PROJECT_OF["示例单位乙"])) == 1


def test_lost_webhook_is_caught_by_reconcile(portal, admin, env, rows, issues):
    hooks = admin.call("GET", f"/database/webhooks/table/{env['TABLE_TICKETS']}/")
    (hook,) = hooks
    admin.call("PATCH", f"/database/webhooks/{hook['id']}/", {"active": False})
    try:
        ticket = submit(portal, rows, "a1", "同步：Webhook 丢失")
    finally:
        admin.call("PATCH", f"/database/webhooks/{hook['id']}/", {"active": True})
    wait_synced(rows, ticket)
    assert len(issues_for(issues, ticket, PROJECT_OF["示例单位甲"])) == 1
