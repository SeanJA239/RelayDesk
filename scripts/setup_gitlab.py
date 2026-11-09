"""Prepare the local GitLab: one project per unit and a least-privilege bridge token.

The admin token is minted through `gitlab-rails` inside the container and is only
used here; the bridge gets a group access token (Reporter, `api`) that can reach
nothing outside the customer-feedback group.
"""

import subprocess
from datetime import date, timedelta

import httpx

from feedback_pilot.client import Api, load_env, save_env

GROUP = "customer-feedback"
PROJECTS = {"示例单位甲": "unit-a", "示例单位乙": "unit-b"}
REPORTER = 20


def admin_token() -> str:
    script = (
        "t = User.find_by_username('root').personal_access_tokens.create!("
        "name: 'pilot-setup', scopes: [:api], expires_at: 30.days.from_now); "
        "puts t.token"
    )
    out = subprocess.run(
        ["docker", "exec", "feedback-gitlab", "gitlab-rails", "runner", script],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return out.strip().splitlines()[-1]


def main() -> None:
    env = load_env()
    token = env.get("GITLAB_ADMIN_TOKEN") or admin_token()
    save_env({"GITLAB_ADMIN_TOKEN": token})
    gl = httpx.Client(
        base_url=f"{env['GITLAB_URL']}/api/v4",
        headers={"PRIVATE-TOKEN": token},
        timeout=60,
    )

    def call(method: str, path: str, **kwargs):
        response = gl.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    groups = call("GET", "/groups", params={"search": GROUP})
    group = next((g for g in groups if g["path"] == GROUP), None) or call(
        "POST", "/groups", json={"name": "客户反馈", "path": GROUP}
    )
    for path in PROJECTS.values():
        full = f"{GROUP}/{path}"
        exists = gl.get(f"/projects/{full.replace('/', '%2F')}").is_success
        if not exists:
            call(
                "POST",
                "/projects",
                json={"name": path, "path": path, "namespace_id": group["id"]},
            )

    bridge = call(
        "POST",
        f"/groups/{group['id']}/access_tokens",
        json={
            "name": "feedback-bridge",
            "scopes": ["api"],
            "access_level": REPORTER,
            "expires_at": (date.today() + timedelta(days=360)).isoformat(),
        },
    )
    save_env({"GITLAB_BRIDGE_TOKEN": bridge["token"]})

    baserow = Api.admin(env)
    units = baserow.call(
        "GET", f"/database/rows/table/{env['TABLE_UNITS']}/?user_field_names=true"
    )["results"]
    baserow.call(
        "PATCH",
        f"/database/rows/table/{env['TABLE_UNITS']}/batch/?user_field_names=true",
        {
            "items": [
                {"id": u["id"], "GitLab项目": f"{GROUP}/{PROJECTS[u['名称']]}"}
                for u in units
            ]
        },
    )
    print(f"group {GROUP} ready; bridge token stored")


if __name__ == "__main__":
    main()
