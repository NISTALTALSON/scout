from datetime import datetime

import pytest
from pydantic import ValidationError

from scout.budgets import Budget, BudgetExceeded
from scout.models import Candidate, Source, web_url


def test_unknowns_and_timezone_validation():
    with pytest.raises(ValidationError):
        Candidate(source_id="x", title="x", url="https://example.org", collected_at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        Source(id="xx", name="x", url="https://example.org", type="rss", categories=["ai_tools"],
               trust="official")


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://a:b@example.org", "https://x:22/", "https://a/\n"])
def test_url_syntax(url):
    with pytest.raises(ValueError):
        web_url(url)


def test_budget_stops_before_extra_request():
    budget = Budget(max_requests=1)
    budget.consume_http()
    with pytest.raises(BudgetExceeded, match="http_budget"):
        budget.consume_http()
    assert budget.requests == 1
    with pytest.raises(BudgetExceeded, match="ai_budget"):
        budget.consume_ai(1)
