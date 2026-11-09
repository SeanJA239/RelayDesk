"""Login and submit: every ticket lands in the submitter's own unit, nothing leaks."""

import pytest
from conftest import LOGINS, UNIT_OF, email

from feedback_pilot.client import Api


def ticket_by_title(rows, title: str) -> dict:
    (ticket,) = [r for r in rows("TABLE_TICKETS") if r["标题"] == title]
    return ticket


@pytest.mark.parametrize("login", LOGINS)
def test_ticket_is_bound_to_session_users_unit(portal, rows, login):
    title = f"隔离：{login} 的工单"
    response = portal.submit(portal.login(login), title)
    assert response.is_success, response.text
    assert set(response.json()) == {"编号"}
    ticket = ticket_by_title(rows, title)
    assert [u["value"] for u in ticket["单位"]] == [UNIT_OF[login]]
    (submitter,) = ticket["提交人"]
    account = next(r for r in rows("TABLE_ACCOUNTS") if r["id"] == submitter["id"])
    assert account["邮箱"] == email(login)
    assert response.json()["编号"] == ticket["编号"]


@pytest.mark.parametrize(
    "forged",
    [
        pytest.param({"user": {"id": 3, "email": "b1@customer.example"}}, id="user"),
        pytest.param({"data_source": {"element": 1, "id": 3}}, id="data_source"),
        pytest.param({"current_record": {"id": 3}}, id="current_record"),
        pytest.param({"page_parameter": {"id": 3}}, id="page_parameter"),
    ],
)
def test_client_metadata_cannot_redirect_ticket(portal, rows, forged):
    title = f"伪造：{next(iter(forged))}"
    response = portal.submit(portal.login("a1"), title, **forged)
    if response.is_success:
        ticket = ticket_by_title(rows, title)
        assert [u["value"] for u in ticket["单位"]] == ["示例单位甲"]
        assert [s["value"] for s in ticket["提交人"]] == ["示例单位甲-a1"]


def test_extra_form_values_are_not_written(portal, rows):
    a1 = portal.login("a1")
    response = a1.raw(
        "POST",
        f"/builder/workflow_action/{portal.create_action}/dispatch/",
        {
            "metadata": {
                "form_data": {
                    str(portal.title_input): "多余字段",
                    str(portal.body_input): "x",
                    "999999": "示例单位乙",
                }
            }
        },
    )
    if response.is_success:
        assert "示例单位乙" not in str(ticket_by_title(rows, "多余字段"))


@pytest.mark.parametrize(
    "token",
    [None, "garbage", "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjozfQ.c2ln"],
    ids=["anonymous", "garbage", "unsigned"],
)
def test_unauthenticated_cannot_submit_or_read(portal, env, rows, token):
    client = Api(env["BASEROW_URL"], token)
    before = len(rows("TABLE_TICKETS"))
    response = portal.submit(client, "匿名提交")
    assert response.is_error
    assert len(rows("TABLE_TICKETS")) == before
    account = portal.account(client)
    assert account.is_error or account.json() == {}


@pytest.mark.parametrize("password", ["wrong-password", ""])
def test_bad_credentials_are_rejected(portal, password):
    assert portal.token_auth("a1", password).status_code in (400, 401)


def test_unknown_account_is_rejected(portal):
    assert portal.token_auth("nobody", "whatever").status_code in (400, 401)


def test_session_sees_only_own_account(portal, rows):
    for login in LOGINS:
        client = portal.login(login)
        for forged in ({}, {"user": {"id": 1}}, {"page_parameter": {"id": 1}}):
            response = portal.account(client, **forged)
            assert response.is_success
            body = response.json()
            assert body["姓名"].endswith(login)
            assert set(body) <= {"姓名", "单位"}
            others = [a for a in rows("TABLE_ACCOUNTS") if a["邮箱"] != email(login)]
            for other in others:
                assert other["姓名"] not in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/database/rows/table/{TABLE_TICKETS}/",
        "/database/rows/table/{TABLE_ACCOUNTS}/",
        "/database/rows/table/{TABLE_SYNC}/",
        "/database/fields/table/{TABLE_TICKETS}/",
    ],
)
def test_customer_session_cannot_use_database_api(portal, env, path):
    response = portal.login("a1").raw("GET", path.format(**env))
    assert response.status_code == 401


def test_disabled_account_loses_access_immediately(portal, admin, env, rows):
    a2 = portal.login("a2")
    assert portal.account(a2).json()["姓名"].endswith("a2")
    account = next(r for r in rows("TABLE_ACCOUNTS") if r["邮箱"] == email("a2"))
    admin.call(
        "PATCH",
        f"/database/rows/table/{env['TABLE_ACCOUNTS']}/{account['id']}/"
        "?user_field_names=true",
        {"角色": "停用"},
    )
    before = len(rows("TABLE_TICKETS"))
    assert portal.submit(a2, "停用后提交").is_error
    assert len(rows("TABLE_TICKETS")) == before
    account_view = portal.account(a2)
    assert account_view.is_error or account_view.json() == {}


def test_published_config_holds_no_secrets_or_sync_schema(portal, env):
    for client in (portal.anon, portal.login("a1")):
        config = portal.published_config(client)
        for secret_key in (
            "GITLAB_BRIDGE_TOKEN",
            "GITLAB_ADMIN_TOKEN",
            "BRIDGE_BASEROW_TOKEN",
            "BRIDGE_WEBHOOK_SECRET",
        ):
            assert env[secret_key] not in config
        for internal in ("Issue IID", "最近错误", "GitLab项目", "PASSWORD"):
            assert internal not in config, internal


def test_anonymous_sees_no_fields_or_actions(portal, admin, env):
    """The data source shell (table id, `get('user.id')`) is visible; no field is."""
    config = portal.published_config(portal.anon)
    for table in ("TABLE_ACCOUNTS", "TABLE_TICKETS"):
        for f in admin.call("GET", f"/database/fields/table/{env[table]}/"):
            assert f'"{f["name"]}"' not in config, f["name"]
    assert "create_row" not in config
