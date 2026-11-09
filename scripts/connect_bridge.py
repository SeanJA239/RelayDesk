"""Give the bridge a least-privilege Baserow token and point a webhook at it.

The token may read the database but only create/update rows in GitLab同步. The
webhook carries a shared secret header the bridge checks on every call.
"""

import secrets

from feedback_pilot.client import Api, load_env, save_env

# Baserow runs in a container; the bridge runs on the host.
BRIDGE_URL = "http://host.docker.internal:8787/webhooks/baserow"


def main() -> None:
    env = load_env()
    api = Api.admin(env)
    database, sync = int(env["DATABASE_ID"]), int(env["TABLE_SYNC"])

    for token in api.call("GET", "/database/tokens/"):
        if token["name"] == "feedback-bridge":
            api.call("DELETE", f"/database/tokens/{token['id']}/")
    token = api.call(
        "POST",
        "/database/tokens/",
        {"name": "feedback-bridge", "workspace": int(env["WORKSPACE_ID"])},
    )
    api.call(
        "PATCH",
        f"/database/tokens/{token['id']}/",
        {
            "permissions": {
                "create": [["table", sync]],
                "read": [["database", database]],
                "update": [["table", sync]],
                "delete": False,
            }
        },
    )

    tickets = env["TABLE_TICKETS"]
    for hook in api.call("GET", f"/database/webhooks/table/{tickets}/"):
        api.call("DELETE", f"/database/webhooks/{hook['id']}/")
    secret = secrets.token_urlsafe(32)
    api.call(
        "POST",
        f"/database/webhooks/table/{tickets}/",
        {
            "name": "feedback-bridge",
            "url": BRIDGE_URL,
            "request_method": "POST",
            "include_all_events": False,
            "events": ["rows.created"],
            "use_user_field_names": True,
            "headers": {"X-Bridge-Secret": secret},
        },
    )
    save_env({"BRIDGE_BASEROW_TOKEN": token["key"], "BRIDGE_WEBHOOK_SECRET": secret})
    print("bridge token and webhook configured")


if __name__ == "__main__":
    main()
