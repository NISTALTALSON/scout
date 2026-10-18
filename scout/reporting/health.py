from collections import Counter
from datetime import timedelta


def health_report(store, now) -> dict:
    sources = store.list_sources()
    notifications = store.list_notifications()
    runs = store.list_runs(limit=10)
    last_success = next((r for r in runs if r.mode == "scan" and r.sources_ok > 0), None)
    last = last_success.started_at if last_success else None
    enabled = [s for s in sources if s.enabled]
    failed = [s for s in enabled if s.failure_count]
    overdue = [s for s in enabled if not s.last_success_at or
               s.last_success_at < now - timedelta(hours=s.max_interval_hours)]
    categories = Counter(category for s in enabled if s.last_success_at
                         for category in s.categories)
    return {
        "last_successful_scan": last.isoformat() if last else None,
        "missed_run_warning": bool(last and now - last > timedelta(hours=9)),
        "sources_total": len(sources), "enabled": len(enabled),
        "failing": [{"id": s.id, "reason": s.last_error, "failures": s.failure_count,
                     "retry_after": s.suspended_until.isoformat() if s.suspended_until else None}
                    for s in failed],
        "overdue_sources": [s.id for s in overdue],
        "coverage_by_category": dict(categories),
        "outbox": dict(Counter(n.status for n in notifications)),
        "recent_runs": [{"at": r.started_at.isoformat(), "http": r.http_requests,
                         "stop": r.stop_reason, "duration_seconds": r.duration_seconds}
                        for r in runs[:5]],
        "state": store.stats(),
        "limits": "Missed schedules are detectable only on the next execution; no independent uptime SLA.",
    }
