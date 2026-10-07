"""A tiny fake of Linear's GraphQL API for tests (httpx.MockTransport)."""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from ao.linear.client import LinearClient

TEAM = {
    "id": "team-1",
    "key": "KAP",
    "name": "KAPSLOK",
    "states": {
        "nodes": [
            {"id": "st-backlog", "name": "Backlog", "type": "backlog", "position": 0},
            {"id": "st-todo", "name": "Todo", "type": "unstarted", "position": 1},
            {"id": "st-doing", "name": "In Progress", "type": "started", "position": 2},
            {"id": "st-done", "name": "Done", "type": "completed", "position": 3},
        ]
    },
}
PROJECTS = [
    {
        "id": "p-roadmap",
        "name": "Create paperclip clone project",
        "status": {"name": "In Progress", "type": "started"},
    },
    {"id": "p-sandbox", "name": "ao sandbox", "status": {"name": "In Progress", "type": "started"}},
]
MILESTONES = [{"id": "m-m4", "name": "M4 Linear integration", "project": {"id": "p-roadmap"}}]
LABELS = [
    {"id": "l-feature", "name": "Feature", "description": None, "team": None, "parent": None},
    {
        "id": "l-upload",
        "name": "Upload",
        "description": "Blackboard hand-in; use the file-naming rule",
        "team": {"key": "KAP"},
        "parent": None,
    },
    {
        "id": "l-other",
        "name": "OtherTeam",
        "description": None,
        "team": {"key": "XYZ"},
        "parent": None,
    },
]


def issue_node(
    n: int,
    *,
    project: str | None = "p-roadmap",
    state: str = "Todo",
    updated: str = "2026-10-01T00:00:00.000Z",
    labels=("Feature",),
) -> dict[str, Any]:
    proj = next((p for p in PROJECTS if p["id"] == project), None)
    st = next(s for s in TEAM["states"]["nodes"] if s["name"] == state)
    return {
        "id": f"uuid-{n}",
        "identifier": f"KAP-{n}",
        "title": f"Issue {n}",
        "description": f"Body {n}",
        "priority": 3,
        "url": f"https://linear.app/x/KAP-{n}",
        "updatedAt": updated,
        "state": {"name": st["name"], "type": st["type"]},
        "labels": {"nodes": [{"name": name} for name in labels]},
        "project": {"id": proj["id"], "name": proj["name"]} if proj else None,
        "projectMilestone": None,
        "assignee": {"name": "Peter"},
        "parent": None,
    }


@dataclass
class FakeLinear:
    issues: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    page_size: int = 2
    status: int = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        query, variables = body["query"], body.get("variables") or {}
        self.calls.append(
            {"query": query, "variables": variables, "auth": request.headers.get("Authorization")}
        )
        if self.status != 200:
            return httpx.Response(self.status, json={})
        if "mutation" in query:
            return httpx.Response(200, json={"data": self.mutate(query, variables)})
        if "query Team(" in query:
            teams = [TEAM] if variables["key"] == "KAP" else []
            return httpx.Response(200, json={"data": {"teams": {"nodes": teams}}})
        if "query Projects(" in query:
            return httpx.Response(
                200, json={"data": {"teams": {"nodes": [{"projects": {"nodes": PROJECTS}}]}}}
            )
        if "query Labels" in query:
            return httpx.Response(200, json={"data": {"issueLabels": {"nodes": LABELS}}})
        if "query Milestones" in query:
            return httpx.Response(200, json={"data": {"projectMilestones": {"nodes": MILESTONES}}})
        if "issues(" in query:
            filt = variables["filter"]
            nodes = [
                i
                for i in self.issues
                if "updatedAt" not in filt or i["updatedAt"] > filt["updatedAt"]["gt"]
            ]
            start = int(variables.get("after") or 0)
            page = nodes[start : start + self.page_size]
            more = start + self.page_size < len(nodes)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "issues": {
                            "nodes": page,
                            "pageInfo": {
                                "hasNextPage": more,
                                "endCursor": str(start + self.page_size),
                            },
                        }
                    }
                },
            )
        if "issue(" in query:
            found = next((i for i in self.issues if i["identifier"] == variables["id"]), None)
            return httpx.Response(200, json={"data": {"issue": found}})
        return httpx.Response(200, json={"errors": [{"message": "unknown query"}]})

    def mutate(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        if "issueCreate" in query:
            data = variables["input"]
            n = 100 + len([i for i in self.issues if i["identifier"].startswith("KAP-1")])
            node = issue_node(n, project=data["projectId"], labels=())
            node["title"] = data["title"]
            node["description"] = data.get("description")
            self.issues.append(node)
            return {"issueCreate": {"success": True, "issue": node}}
        if "issueUpdate" in query:
            node = next(i for i in self.issues if i["id"] == variables["id"])
            for key in ("title", "description", "priority"):
                if key in variables["input"]:
                    node[key] = variables["input"][key]
            return {"issueUpdate": {"success": True, "issue": node}}
        if "commentCreate" in query:
            return {
                "commentCreate": {
                    "success": True,
                    "comment": {"id": "c-1", "url": "https://linear.app/c-1"},
                }
            }
        raise AssertionError(f"unexpected mutation {query}")

    def client(self) -> LinearClient:
        return LinearClient(
            "lin_test_key", httpx.Client(transport=httpx.MockTransport(self.handler))
        )

    def mutations(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if "mutation" in c["query"]]
