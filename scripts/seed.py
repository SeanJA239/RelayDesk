"""Create the phase-1 schema and fictional sample data in a fresh local Baserow.

Tables:
  单位        customer organisation and its GitLab project
  客户账号    App Builder login table (one row per assigned account)
  工单        what customers submit; holds customer-entered fields only, because
              the publish config of a write action exposes this table's schema
  GitLab同步  internal sync state per ticket, never touched by the portal
"""

import secrets
import sys

from feedback_pilot.client import ENV_FILE, Api, load_env, save_env

BASEROW_URL = "http://localhost:8090"
ADMIN_EMAIL = "admin@example.com"

UNITS = {"示例单位甲": ("a1", "a2"), "示例单位乙": ("b1", "b2")}


def options(*names: str) -> list[dict]:
    colors = ["blue", "orange", "green", "red", "dark-gray"]
    return [{"value": n, "color": colors[i % len(colors)]} for i, n in enumerate(names)]


def main() -> None:
    if ENV_FILE.exists() and "ADMIN_PASSWORD" in load_env():
        sys.exit(f"{ENV_FILE} already holds a seeded instance.")

    password = secrets.token_urlsafe(18)
    jwt = Api(BASEROW_URL).call(
        "POST",
        "/user/",
        {
            "name": "Admin",
            "email": ADMIN_EMAIL,
            "password": password,
            "authenticate": True,
        },
    )["access_token"]
    api = Api(BASEROW_URL, jwt)

    def table(database_id: int, name: str, primary: str) -> int:
        # A header-only data payload yields a table with just this primary field.
        return api.call(
            "POST",
            f"/database/tables/database/{database_id}/",
            {"name": name, "data": [[primary]], "first_row_header": True},
        )["id"]

    def field(table_id: int, name: str, type_: str, **opts) -> None:
        api.call(
            "POST",
            f"/database/fields/table/{table_id}/",
            {"name": name, "type": type_, **opts},
        )

    def rows(table_id: int, items: list[dict]) -> list[dict]:
        return api.call(
            "POST",
            f"/database/rows/table/{table_id}/batch/?user_field_names=true",
            {"items": items},
        )["items"]

    workspace = api.call("POST", "/workspaces/", {"name": "客户反馈试点"})["id"]
    database = api.call(
        "POST",
        f"/applications/workspace/{workspace}/",
        {"type": "database", "name": "反馈数据"},
    )["id"]

    units = table(database, "单位", "名称")
    field(units, "GitLab项目", "text")

    accounts = table(database, "客户账号", "姓名")
    field(accounts, "邮箱", "email")
    field(accounts, "密码", "password")
    field(accounts, "角色", "single_select", select_options=options("客户", "停用"))
    field(accounts, "单位", "link_row", link_row_table_id=units, has_related_field=True)

    tickets = table(database, "工单", "标题")
    field(tickets, "编号", "autonumber")
    field(tickets, "问题描述", "long_text")
    field(tickets, "附件", "file")
    field(tickets, "单位", "link_row", link_row_table_id=units, has_related_field=True)
    field(
        tickets,
        "提交人",
        "link_row",
        link_row_table_id=accounts,
        has_related_field=True,
    )
    field(tickets, "提交时间", "created_on", date_include_time=True)

    sync = table(database, "GitLab同步", "说明")
    field(sync, "工单", "link_row", link_row_table_id=tickets, has_related_field=True)
    field(
        sync,
        "状态",
        "single_select",
        select_options=options("已同步", "失败", "处理中"),
    )
    field(sync, "Issue IID", "number")
    field(sync, "Issue 链接", "url")
    field(sync, "尝试次数", "number")
    field(sync, "最近错误", "long_text")
    field(sync, "更新时间", "last_modified", date_include_time=True)

    unit_rows = rows(units, [{"名称": name} for name in UNITS])
    passwords: dict[str, str] = {}
    account_items = []
    for unit, logins in zip(unit_rows, UNITS.values(), strict=True):
        for login in logins:
            passwords[login] = secrets.token_urlsafe(12)
            account_items.append(
                {
                    "姓名": f"{unit['名称']}-{login}",
                    "邮箱": f"{login}@customer.example",
                    "密码": passwords[login],
                    "角色": "客户",
                    "单位": [unit["id"]],
                }
            )
    rows(accounts, account_items)

    save_env(
        {
            "BASEROW_URL": BASEROW_URL,
            "ADMIN_EMAIL": ADMIN_EMAIL,
            "ADMIN_PASSWORD": password,
            "WORKSPACE_ID": workspace,
            "DATABASE_ID": database,
            "TABLE_UNITS": units,
            "TABLE_ACCOUNTS": accounts,
            "TABLE_TICKETS": tickets,
            "TABLE_SYNC": sync,
            **{f"PASSWORD_{login.upper()}": pw for login, pw in passwords.items()},
        }
    )
    print(f"seeded; credentials in {ENV_FILE}")


if __name__ == "__main__":
    main()
