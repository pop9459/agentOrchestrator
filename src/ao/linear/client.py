"""Minimal Linear GraphQL client (read side). Plain Python: no LLM ever sees the API key.

Write mutations live in `ao.linear.changes` and are only reachable through an applied
change set (KAP-95).
"""

from collections.abc import Iterator
from typing import Any

import httpx

from ao import secrets
from ao.config import LoadedConfig

API_URL = "https://api.linear.app/graphql"
PAGE_SIZE = 50  # Linear rejects "too complex" queries; nested lists also carry explicit limits

ISSUE_FIELDS = """
  id identifier title description priority url updatedAt
  state { name type }
  labels(first: 10) { nodes { name } }
  project { id name }
  projectMilestone { name }
  assignee { name }
  parent { identifier }
"""

# Metadata is fetched in small separate queries (one combined query is "too complex").
TEAM_QUERY = """
query Team($key: String!) {
  teams(filter: { key: { eq: $key } }) {
    nodes { id key name states { nodes { id name type position } } }
  }
}
"""

PROJECTS_QUERY = """
query Projects($key: String!) {
  teams(filter: { key: { eq: $key } }) {
    nodes { projects(first: 50) { nodes { id name status { name type } } } }
  }
}
"""

LABELS_QUERY = """
query Labels {
  issueLabels(first: 250) { nodes { id name description team { key } parent { name } } }
}
"""

MILESTONES_QUERY = """
query Milestones {
  projectMilestones(first: 250) { nodes { id name project { id } } }
}
"""

ISSUES_QUERY = f"""
query Issues($filter: IssueFilter, $after: String) {{
  issues(first: {PAGE_SIZE}, after: $after, filter: $filter) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ {ISSUE_FIELDS} }}
  }}
}}
"""

ISSUE_QUERY = f"""
query Issue($id: String!) {{
  issue(id: $id) {{ {ISSUE_FIELDS} }}
}}
"""


class LinearError(Exception):
    pass


class LinearClient:
    def __init__(self, api_key: str, http: httpx.Client | None = None, timeout: float = 30):
        self._http = http or httpx.Client(timeout=timeout)
        self._headers = {"Authorization": api_key, "Content-Type": "application/json"}

    def query(self, document: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = self._http.post(
                API_URL,
                json={"query": document, "variables": variables or {}},
                headers=self._headers,
            )
        except httpx.HTTPError as exc:
            raise LinearError(f"Linear request failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise LinearError("Linear rejected the API key (check AO_SECRET_LINEAR)")
        if response.status_code == 429:
            raise LinearError("Linear rate limit hit; try again later")
        try:
            body = response.json()
        except ValueError as exc:
            raise LinearError(f"Linear returned HTTP {response.status_code}") from exc
        if body.get("errors"):
            messages = "; ".join(e.get("message", "?") for e in body["errors"])
            raise LinearError(f"Linear API error: {messages}")
        if response.status_code >= 400 or "data" not in body:
            raise LinearError(f"Linear returned HTTP {response.status_code}")
        return body["data"]

    def team(self, key: str) -> dict[str, Any]:
        """Team with states, projects (status + milestones) and team/workspace labels."""
        teams = self.query(TEAM_QUERY, {"key": key})["teams"]["nodes"]
        if not teams:
            raise LinearError(f"no Linear team with key {key!r}")
        team = teams[0]
        projects = self.query(PROJECTS_QUERY, {"key": key})["teams"]["nodes"][0]["projects"]
        milestones: dict[str, list[dict[str, str]]] = {}
        for node in self.query(MILESTONES_QUERY)["projectMilestones"]["nodes"]:
            if node.get("project"):
                milestones.setdefault(node["project"]["id"], []).append(
                    {"id": node["id"], "name": node["name"]}
                )
        team["projects"] = [
            {
                "id": p["id"],
                "name": p["name"],
                "state": (p.get("status") or {}).get("type"),
                "milestones": milestones.get(p["id"], []),
            }
            for p in projects["nodes"]
        ]
        team["labels"] = [
            label
            for label in self.query(LABELS_QUERY)["issueLabels"]["nodes"]
            if label.get("team") is None or label["team"].get("key") == key
        ]
        return team

    def issues(self, team_id: str, updated_after: str | None = None) -> Iterator[dict[str, Any]]:
        issue_filter: dict[str, Any] = {"team": {"id": {"eq": team_id}}}
        if updated_after:
            issue_filter["updatedAt"] = {"gt": updated_after}
        after = None
        while True:
            page = self.query(ISSUES_QUERY, {"filter": issue_filter, "after": after})["issues"]
            yield from page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                return
            after = page["pageInfo"]["endCursor"]

    def issue(self, identifier: str) -> dict[str, Any] | None:
        return self.query(ISSUE_QUERY, {"id": identifier}).get("issue")


def client_from_config(loaded: LoadedConfig, http: httpx.Client | None = None) -> LinearClient:
    name = loaded.config.linear.api_key_secret
    try:
        key = secrets.get_secret(name)
    except secrets.SecretNotFound as exc:
        raise LinearError(
            f"no Linear API key: create a personal API key in Linear (Settings → Security & "
            f"access) and put it in .env as {secrets.env_var_name(name)}=lin_api_…"
        ) from exc
    return LinearClient(key, http)
