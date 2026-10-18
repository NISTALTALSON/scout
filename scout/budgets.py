"""Hard per-cycle resource accounting shared by all deterministic agent tasks."""

import time
from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    max_seconds: float = 90
    max_requests: int = 80
    max_candidates: int = 120
    max_new_sources: int = 3
    max_ai_calls: int = 0
    max_ai_tokens: int = 0
    requests: int = 0
    candidates: int = 0
    new_sources: int = 0
    ai_calls: int = 0
    ai_tokens: int = 0
    stopped: bool = False
    started: float = field(default_factory=time.monotonic)

    @property
    def remaining_seconds(self) -> float:
        return max(0, self.max_seconds - (time.monotonic() - self.started))

    def check(self) -> None:
        if self.stopped:
            raise BudgetExceeded("shutdown_requested")
        if self.remaining_seconds <= 0:
            raise BudgetExceeded("duration_budget")

    def consume_http(self) -> None:
        self.check()
        if self.requests >= self.max_requests:
            raise BudgetExceeded("http_budget")
        self.requests += 1

    def consume_candidate(self) -> None:
        self.check()
        if self.candidates >= self.max_candidates:
            raise BudgetExceeded("candidate_budget")
        self.candidates += 1

    def consume_source(self) -> None:
        self.check()
        if self.new_sources >= self.max_new_sources:
            raise BudgetExceeded("source_discovery_budget")
        self.new_sources += 1

    def consume_ai(self, tokens: int) -> None:
        self.check()
        if self.ai_calls >= self.max_ai_calls or self.ai_tokens + tokens > self.max_ai_tokens:
            raise BudgetExceeded("ai_budget")
        self.ai_calls += 1
        self.ai_tokens += tokens
