"""GitLab side of the bridge: find, create and attach to issues."""

from dataclasses import dataclass
from urllib.parse import quote

import httpx


@dataclass(frozen=True)
class Issue:
    iid: int
    url: str


class GitLab:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    @staticmethod
    def _project(path: str) -> str:
        return f"/api/v4/projects/{quote(path, safe='')}"

    async def find_by_title_prefix(self, project: str, prefix: str) -> Issue | None:
        """Exact title-prefix match; GitLab search is fuzzy, so hits are rechecked."""
        response = await self.http.get(
            f"{self._project(project)}/issues",
            params={
                "search": prefix.strip("[] "),
                "in": "title",
                "scope": "all",
                "per_page": 100,
            },
        )
        response.raise_for_status()
        matches = [i for i in response.json() if i["title"].startswith(prefix)]
        if len(matches) > 1:
            iids = sorted(i["iid"] for i in matches)
            raise RuntimeError(f"{project} has several issues titled {prefix}: {iids}")
        return Issue(matches[0]["iid"], matches[0]["web_url"]) if matches else None

    async def create_issue(self, project: str, title: str, description: str) -> Issue:
        response = await self.http.post(
            f"{self._project(project)}/issues",
            json={"title": title, "description": description},
        )
        response.raise_for_status()
        body = response.json()
        return Issue(body["iid"], body["web_url"])

    async def upload(self, project: str, name: str, content: bytes) -> str:
        """Upload a file to the project and return its Markdown link."""
        response = await self.http.post(
            f"{self._project(project)}/uploads", files={"file": (name, content)}
        )
        response.raise_for_status()
        return response.json()["markdown"]
