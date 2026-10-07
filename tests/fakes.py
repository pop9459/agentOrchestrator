"""Test doubles."""

from collections.abc import Callable
from dataclasses import dataclass, field

from ao.backends.base import Health, Result, RunRequest, Usage
from ao.config import BackendConfig


@dataclass
class FakeBackend:
    """Returns canned results and remembers every request."""

    name: str = "fake"
    config: BackendConfig = field(default_factory=lambda: BackendConfig(type="claude_code"))
    results: list[Result] = field(default_factory=list)
    requests: list[RunRequest] = field(default_factory=list)
    local: bool = False
    # Optional per-request answer (e.g. scripted per agent); wins over `results`.
    responder: Callable[[RunRequest], Result] | None = None

    @property
    def is_local(self) -> bool:
        return self.local

    def run(self, request: RunRequest) -> Result:
        self.requests.append(request)
        if self.responder is not None:
            return self.responder(request)
        if self.results:
            return self.results.pop(0)
        return Result(
            outcome="ok",
            text=f"echo: {request.prompt}",
            usage=Usage(input_tokens=100, output_tokens=10, cache_read_tokens=5,
                        cache_write_tokens=20, cost_usd=0.001),
            model="fake-model",
            duration_ms=12,
        )  # fmt: skip

    def health(self) -> Health:
        return Health(ok=True, detail="fake")

    def preview(self, request: RunRequest) -> str:
        return f"fake-backend model={request.model}"
