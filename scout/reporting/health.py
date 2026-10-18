from collections import Counter
from datetime import timedelta


def health_report(store, now) -> dict:
    sources = store.list_sources()
    notifications = store.list_notifications()
    runs = store.list_runs(limit=10)
    scan_runs = [run for run in runs if run.mode == "scan"]
    complete_runs = [run for run in scan_runs if run.stop_reason == "completed" and
                     run.sources_planned > 0 and
                     run.sources_ok + run.sources_failed >= run.sources_planned]
    last_complete = complete_runs[0] if complete_runs else None
    last = last_complete.started_at if last_complete else None
    latest_scan = scan_runs[0] if scan_runs else None
    if latest_scan is None:
        coverage = {"status": "no_scan", "planned": 0, "completed": 0, "ok": 0,
                    "failed": 0, "ratio": 0.0}
    else:
        completed = min(latest_scan.sources_planned,
                        latest_scan.sources_ok + latest_scan.sources_failed)
        planned = latest_scan.sources_planned
        coverage = {
            "status": "complete" if latest_scan.stop_reason == "completed" and
            completed >= planned else "partial",
            "planned": planned,
            "completed": completed,
            "ok": latest_scan.sources_ok,
            "failed": latest_scan.sources_failed,
            "ratio": round(completed / planned, 3) if planned else 0.0,
        }
    enabled = [s for s in sources if s.enabled]
    failed = [s for s in enabled if s.failure_count]
    overdue = [s for s in enabled if not s.last_success_at or
               s.last_success_at < now - timedelta(hours=s.max_interval_hours)]
    categories = Counter(category for s in enabled if s.last_success_at
                         for category in s.categories)
    return {
        "last_successful_scan": last.isoformat() if last else None,
        "last_scan": latest_scan.started_at.isoformat() if latest_scan else None,
        "coverage": coverage,
        "partial_scan_warning": coverage["status"] == "partial",
        "missed_run_warning": bool(not last or now - last > timedelta(hours=9)),
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
