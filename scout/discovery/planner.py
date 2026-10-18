"""Due-aware scheduling with fair category rotation and cautious backoff."""

from datetime import timedelta


def _urgent(source, opportunities, now):
    return any(getattr(item, "source_id", None) == source.id and
               getattr(item, "deadline_verified", False) and
               getattr(item, "verification_status", "") in {"official_verified", "corroborated"} and
               (deadline := getattr(item, "deadline_at", None)) is not None and
               now < deadline <= now + timedelta(hours=168) for item in opportunities or ())


def plan_sources(sources, now, category=None, urgent_only=False, opportunities=None):
    """Return due sources, interleaving categories rather than starving small categories."""
    buckets = {}
    for source in sources:
        if source.validation_status in {"rejected", "quarantined"}:
            continue
        if source.validation_status == "suspended" and (
            not source.suspended_until or now < source.suspended_until
        ):
            continue
        if not source.enabled and source.validation_status != "probation":
            continue
        if category and category not in source.categories:
            continue
        if urgent_only and not _urgent(source, opportunities, now):
            continue
        if source.suspended_until and now < source.suspended_until:
            continue
        urgent = _urgent(source, opportunities, now)
        interval = min(source.max_interval_hours,
                        max(source.min_interval_hours,
                            (source.min_interval_hours if urgent else source.scan_interval_hours)
                            * (2 ** min(source.failure_count, 8))))
        if source.last_attempt_at and now < source.last_attempt_at + timedelta(hours=interval):
            continue
        key = category or min(source.categories)
        lag = (now - source.last_attempt_at).total_seconds() if source.last_attempt_at else float("inf")
        priority = lag * (0.8 + 0.4 * source.usefulness) + (3600 if urgent else 0)
        buckets.setdefault(key, []).append((-priority, source.id, source))
    for queue in buckets.values():
        queue.sort(key=lambda row: (row[0], row[1]))
    output = []
    while any(buckets.values()):
        for key in sorted(buckets, key=lambda key: buckets[key][0][:2] if buckets[key] else (0, "")):
            if buckets[key]:
                output.append(buckets[key].pop(0)[2])
    return output
