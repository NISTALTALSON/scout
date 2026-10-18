"""Operational CLI. All commands are bounded and return useful non-zero errors."""

import argparse
import json
import sys

from scout.notifications.email_sender import SmtpSender
from scout.notifications.outbox import make_notification
from scout.orchestrator import opportunity_report
from scout.runtime import Runtime, now_utc


def _json(data):
    # Windows console encodings must not make a valid report fail on a publisher's
    # zero-width/non-ASCII character. Escaped JSON remains lossless and portable.
    print(json.dumps(data, indent=2, ensure_ascii=True, default=str))


def _runtime(args, bootstrap=False):
    return Runtime(args.state_dir, args.config_dir, args.cloud_state, bootstrap=bootstrap)


def _sender(args):
    if args.no_email:
        return None
    try:
        return SmtpSender.from_env()
    except ValueError:
        return None


def command_scan(args):
    runtime = _runtime(args, bootstrap=args.bootstrap_cloud_state)
    try:
        scout = runtime.scout(sender=_sender(args))
        try:
            metrics = scout.scan(category=args.category, urgent_only=args.urgent_only,
                                 notify_historical=args.notify_historical,
                                 discover=not args.no_discovery)
            delivery = scout.deliver(dry_run=args.dry_run or args.no_email)
            if args.cloud_state:
                runtime.checkpoint()
            _json({"metrics": metrics.model_dump(mode="json"), "delivery": delivery,
                   "checkpoint_writes": runtime._checkpoint_count})
            return 0 if not metrics.warnings else 2
        finally:
            scout.close()
    finally:
        runtime.close()


def command_verify(args):
    runtime = _runtime(args, bootstrap=args.bootstrap_cloud_state)
    try:
        scout = runtime.scout(sender=_sender(args))
        try:
            metrics = scout.scan(verify_only=True, notify_historical=args.notify_historical)
            delivery = scout.deliver(dry_run=args.dry_run or args.no_email)
            _json({"metrics": metrics.model_dump(mode="json"), "delivery": delivery})
            return 0 if not metrics.warnings else 2
        finally:
            scout.close()
    finally:
        runtime.close()


def command_digest(args):
    runtime = _runtime(args, bootstrap=args.bootstrap_cloud_state)
    try:
        scout = runtime.scout(sender=_sender(args))
        try:
            note = scout.digest(include_historical=args.notify_historical)
            result = scout.deliver(dry_run=args.dry_run or args.no_email)
            _json({"notification": note.model_dump(mode="json"), "delivery": result})
            return 0
        finally:
            scout.close()
    finally:
        runtime.close()


def command_sources(args):
    runtime = _runtime(args, bootstrap=args.bootstrap_cloud_state)
    try:
        scout = runtime.scout(sender=None)
        try:
            if args.sources_action == "health":
                from scout.reporting.health import health_report
                _json(health_report(runtime.store, now_utc()))
                return 0
            if args.sources_action == "discover":
                from scout.discovery.query_engine import run_queries
                urls = run_queries(scout.http, scout.budget, scout.now)
                metrics = {"urls": urls, "sources": []}
                found = scout._discover(urls, "manual discovery",
                                        type("Metrics", (), {"discovered_sources": 0})())
                metrics["sources"] = [source.model_dump(mode="json") for source in (found or [])]
                _json(metrics)
                return 0
            _json([source.model_dump(mode="json") for source in runtime.store.list_sources()])
            return 0
        finally:
            scout.close()
    finally:
        runtime.close()


def command_health(args):
    runtime = _runtime(args, bootstrap=args.bootstrap_cloud_state)
    try:
        scout = runtime.scout(sender=None)
        try:
            from scout.reporting.health import health_report
            _json(health_report(runtime.store, now_utc()))
            return 0
        finally:
            scout.close()
    finally:
        runtime.close()


def command_state(args):
    runtime = _runtime(args, bootstrap=args.bootstrap_cloud_state)
    try:
        if args.state_action == "export":
            runtime.export_local(args.path)
            print(f"Exported validated Scout state to {args.path}")
        else:
            runtime.import_local(args.path, replace=args.replace)
            if args.cloud_state:
                runtime.checkpoint()
            print(f"Imported validated Scout state from {args.path}")
        return 0
    finally:
        runtime.close()


def command_email(args):
    runtime = _runtime(args, bootstrap=args.bootstrap_cloud_state)
    try:
        sender = None if args.dry_run else SmtpSender.from_env()
        scout = runtime.scout(sender=sender)
        try:
            note = make_notification("test", [], scout.now, f"test:{scout.now.isoformat()}",
                                     scout.preferences.timezone,
                                     {"Result": "Dry run: no SMTP call" if args.dry_run else "SMTP send requested"})
            runtime.store.enqueue(note)
            _json(scout.deliver(dry_run=args.dry_run))
            return 0
        finally:
            scout.close()
    finally:
        runtime.close()


def command_feedback(args):
    runtime = _runtime(args, bootstrap=args.bootstrap_cloud_state)
    try:
        if args.action:
            item = runtime.store.feedback(args.id, args.action)
            _json(item.model_dump(mode="json"))
        else:
            _json([item.model_dump(mode="json") for item in runtime.store.list_opportunities()])
        return 0
    finally:
        runtime.close()


def command_report(args):
    runtime = _runtime(args, bootstrap=args.bootstrap_cloud_state)
    try:
        scout = runtime.scout(sender=None)
        try:
            _json(opportunity_report(runtime.store, now_utc(), args.limit))
            return 0
        finally:
            scout.close()
    finally:
        runtime.close()


def command_maintain(args):
    runtime = _runtime(args, bootstrap=args.bootstrap_cloud_state)
    try:
        scout = runtime.scout()
        try:
            _json(scout.maintain())
            return 0
        finally:
            scout.close()
    finally:
        runtime.close()


def build_parser():
    parser = argparse.ArgumentParser(prog="scout", description="Bounded opportunity intelligence agent")
    parser.add_argument("--state-dir", default="data")
    parser.add_argument("--config-dir", default="config")
    parser.add_argument("--cloud-state", action="store_true", help="load/save private GitHub state branch")
    parser.add_argument("--bootstrap-cloud-state", action="store_true",
                        help="explicitly create missing cloud state (first deployment only)")
    parser.add_argument("--no-email", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="collect, verify, score, persist, and queue alerts")
    scan.add_argument("--dry-run", action="store_true", help="do not send queued email")
    scan.add_argument("--urgent-only", action="store_true")
    scan.add_argument("--category")
    scan.add_argument("--notify-historical", action="store_true")
    scan.add_argument("--no-discovery", action="store_true")
    scan.set_defaults(func=command_scan)

    verify = sub.add_parser("verify", help="recheck stored opportunities")
    verify.add_argument("--dry-run", action="store_true")
    verify.add_argument("--notify-historical", action="store_true")
    verify.set_defaults(func=command_verify)

    digest = sub.add_parser("digest", help="queue and deliver one ranked digest")
    digest.add_argument("--dry-run", action="store_true")
    digest.add_argument("--notify-historical", action="store_true")
    digest.set_defaults(func=command_digest)

    sources = sub.add_parser("sources")
    sources.add_argument("sources_action", nargs="?", choices=["health", "discover"])
    sources.set_defaults(func=command_sources)
    sub.add_parser("health").set_defaults(func=command_health)

    state = sub.add_parser("state")
    state.add_argument("state_action", choices=["export", "import"])
    state.add_argument("path")
    state.add_argument("--replace", action="store_true")
    state.set_defaults(func=command_state)

    email = sub.add_parser("test-email")
    email.add_argument("--dry-run", action="store_true")
    email.set_defaults(func=command_email)

    feedback = sub.add_parser("feedback")
    feedback.add_argument("--id")
    feedback.add_argument("--action", choices=["save", "ignore", "applied", "irrelevant", "outdated"])
    feedback.set_defaults(func=command_feedback)

    report = sub.add_parser("report")
    report.add_argument("--limit", type=int, default=25)
    report.set_defaults(func=command_report)
    sub.add_parser("maintain").set_defaults(func=command_maintain)
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        if args.command == "feedback" and bool(args.id) != bool(args.action):
            raise ValueError("feedback requires both --id and --action, or neither")
        return args.func(args)
    except KeyboardInterrupt:
        print("Scout stopped safely; durable state remains intact.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"scout: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
