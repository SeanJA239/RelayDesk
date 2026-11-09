"""Build and publish the phase-1 customer portal: log in, submit a ticket.

Pages:
  /login   email/password form backed by the 客户账号 table
  /        submit a ticket (logged-in customers only)

Isolation rules this config relies on (tests/live attacks each one):
  * 单位 and 提交人 of a new ticket come from the 当前账号 get-row data source,
    keyed by the server-side session user id, never from anything the browser
    sends;
  * that data source also requires 角色 = 客户. Page role restrictions only drive
    navigation (workflow-action dispatch checks element visibility, not the
    page's), so this filter is what makes a 停用 account fail on its next
    request, for reads and writes alike.
"""

import time

from feedback_pilot.client import Api, load_env, save_env

DOMAIN = "portal.localhost"
CUSTOMER_ROLE = "客户"


def main() -> None:
    env = load_env()
    api = Api.admin(env)

    def fields(table: str) -> dict[str, int]:
        return {
            f["name"]: f["id"]
            for f in api.call("GET", f"/database/fields/table/{env[table]}/")
        }

    accounts, tickets = fields("TABLE_ACCOUNTS"), fields("TABLE_TICKETS")
    customer_option = next(
        o["id"]
        for o in api.call("GET", f"/database/fields/{accounts['角色']}/")[
            "select_options"
        ]
        if o["value"] == CUSTOMER_ROLE
    )

    # Rebuild from scratch so the config always matches this script. A trashed
    # app keeps its domain reserved, so domains are deleted first.
    for app in api.call("GET", f"/applications/workspace/{env['WORKSPACE_ID']}/"):
        if app["type"] == "builder":
            for domain in api.call("GET", f"/builder/{app['id']}/domains/"):
                api.call("DELETE", f"/builder/domains/{domain['id']}/")
            api.call("DELETE", f"/applications/{app['id']}/")

    builder = api.call(
        "POST",
        f"/applications/workspace/{env['WORKSPACE_ID']}/",
        {"type": "builder", "name": "客户工单入口"},
    )["id"]
    integration = api.call(
        "POST",
        f"/application/{builder}/integrations/",
        {"type": "local_baserow", "name": "本地数据"},
    )["id"]
    user_source = api.call(
        "POST",
        f"/application/{builder}/user-sources/",
        {
            "type": "local_baserow",
            "name": "客户账号",
            "integration_id": integration,
            "table_id": int(env["TABLE_ACCOUNTS"]),
            "email_field_id": accounts["邮箱"],
            "name_field_id": accounts["姓名"],
            "role_field_id": accounts["角色"],
            "auth_providers": [
                {
                    "type": "local_baserow_password",
                    "password_field_id": accounts["密码"],
                }
            ],
        },
    )["id"]

    def page(name: str, path: str) -> int:
        return api.call(
            "POST",
            f"/builder/{builder}/pages/",
            {"name": name, "path": path, "path_params": []},
        )["id"]

    def data_source(page_id: int, name: str, type_: str, table: str, **config):
        # Formula-bearing settings are validated against the page, which only
        # exists in the update path.
        ds = api.call(
            "POST",
            f"/builder/page/{page_id}/data-sources/",
            {
                "type": type_,
                "name": name,
                "integration_id": integration,
                "table_id": int(env[table]),
            },
        )
        return api.call("PATCH", f"/builder/data-source/{ds['id']}/", config)["id"]

    def element(
        page_id: int, type_: str, place: tuple[int, str] | None = None, **config
    ) -> int:
        """`place` is (reference element id, "child" | "south" | "north")."""
        placement = (
            {"reference_element_id": place[0], "position": place[1]} if place else {}
        )
        el = api.call(
            "POST",
            f"/builder/page/{page_id}/elements/",
            {"type": type_, **placement},
        )
        if config:
            api.call("PATCH", f"/builder/element/{el['id']}/", config)
        return el["id"]

    def action(page_id: int, element_id: int, event: str, type_: str, **config):
        created = api.call(
            "POST",
            f"/builder/page/{page_id}/workflow_actions/",
            {"type": type_, "element_id": element_id, "event": event},
        )
        if config:
            api.call("PATCH", f"/builder/workflow_action/{created['id']}/", config)
        return created["id"]

    # --- pages -------------------------------------------------------------
    login_page = page("登录", "/login")
    submit_page = page("提交工单", "/")
    api.call(
        "PATCH",
        f"/builder/pages/{submit_page}/",
        {
            "visibility": "logged-in",
            "role_type": "disallow_all_except",
            "roles": [CUSTOMER_ROLE],
        },
    )
    api.call("PATCH", f"/applications/{builder}/", {"login_page_id": login_page})

    login_form = element(
        login_page, "auth_form", user_source_id=user_source, login_button_label="'登录'"
    )
    action(
        login_page,
        login_form,
        "after_login",
        "open_page",
        navigation_type="page",
        navigate_to_page_id=submit_page,
    )

    # The session user id is resolved server-side from the user-source JWT.
    ds_account = data_source(
        submit_page,
        "当前账号",
        "local_baserow_get_row",
        "TABLE_ACCOUNTS",
        row_id="get('user.id')",
        filters=[
            {
                "field": accounts["角色"],
                "type": "single_select_equal",
                "value": f"'{customer_option}'",
            }
        ],
    )

    def account(path: str) -> str:
        return f"get('data_source.{ds_account}.{path}')"

    element(
        submit_page,
        "heading",
        value=f"concat('提交工单｜', {account(f'field_{accounts["单位"]}.0.value')})",
    )
    element(
        submit_page,
        "text",
        value=f"concat('当前账号：', {account(f'field_{accounts["姓名"]}')})",
    )

    form = element(submit_page, "form_container", submit_button_label="'提交'")
    title = element(
        submit_page,
        "input_text",
        place=(form, "child"),
        label="'标题'",
        required=True,
    )
    body = element(
        submit_page,
        "input_text",
        place=(title, "south"),
        label="'问题描述'",
        required=True,
        is_multiline=True,
        rows=6,
    )
    # No attachment input: `input_file` needs an Advanced/Enterprise license.

    create_ticket = action(
        submit_page,
        form,
        "submit",
        "create_row",
        service={
            "integration_id": integration,
            "table_id": int(env["TABLE_TICKETS"]),
            "field_mappings": [
                {"field_id": f, "value": v, "enabled": True}
                for f, v in {
                    tickets["标题"]: f"get('form_data.{title}')",
                    tickets["问题描述"]: f"get('form_data.{body}')",
                    # A get-row raises when the session user has no row, which
                    # aborts the write instead of creating an orphan ticket.
                    tickets["单位"]: account(f"field_{accounts['单位']}.0.id"),
                    tickets["提交人"]: account("id"),
                }.items()
            ],
        },
    )
    action(
        submit_page,
        form,
        "submit",
        "notification",
        title="'已提交'",
        description=(
            f"concat('工单编号：FB-', "
            f"get('previous_action.{create_ticket}.field_{tickets['编号']}'))"
        ),
    )
    logout = element(submit_page, "button", value="'退出登录'")
    action(submit_page, logout, "click", "logout")

    # --- publish -----------------------------------------------------------
    domain = api.call(
        "POST",
        f"/builder/{builder}/domains/",
        {"type": "custom", "domain_name": DOMAIN},
    )["id"]
    job = api.call("POST", f"/builder/domains/{domain}/publish/async/")
    while (job := api.call("GET", f"/jobs/{job['id']}/"))["state"] not in (
        "finished",
        "failed",
    ):
        time.sleep(1)
    if job["state"] != "finished":
        raise RuntimeError(f"publish failed: {job}")

    save_env({"PORTAL_DOMAIN": DOMAIN})
    print(f"published builder {builder} at http://{DOMAIN}:8090")


if __name__ == "__main__":
    main()
