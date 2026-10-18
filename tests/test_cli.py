import scout.cli as cli


def test_cloud_bootstrap_is_explicit(monkeypatch):
    calls = []

    class FakeRuntime:
        store = object()

        def __init__(self, *args, **kwargs):
            calls.append(kwargs)

        def scout(self, sender=None):
            class FakeScout:
                def close(self):
                    pass

            return FakeScout()

        def close(self):
            pass

    monkeypatch.setattr(cli, "Runtime", FakeRuntime)
    monkeypatch.setattr(cli, "_json", lambda _data: None)
    monkeypatch.setattr("scout.reporting.health.health_report", lambda _store, _now: {})

    normal = cli.build_parser().parse_args(["--cloud-state", "health"])
    normal.func(normal)
    assert calls[-1]["bootstrap"] is False

    first_deployment = cli.build_parser().parse_args(
        ["--cloud-state", "--bootstrap-cloud-state", "health"]
    )
    first_deployment.func(first_deployment)
    assert calls[-1]["bootstrap"] is True


def test_sources_discover_reports_onboarded_sources(monkeypatch):
    class FakeSource:
        def model_dump(self, mode=None):
            assert mode == "json"
            return {"id": "discovered"}

    class FakeScout:
        http = object()
        budget = object()
        now = object()

        def _discover(self, urls, origin, metrics):
            assert urls == ["https://example.org/feed.xml"]
            assert origin == "manual discovery"
            return [FakeSource()]

        def close(self):
            pass

    class FakeRuntime:
        store = object()

        def __init__(self, *args, **kwargs):
            pass

        def scout(self, sender=None):
            assert sender is None
            return FakeScout()

        def close(self):
            pass

    output = []
    monkeypatch.setattr(cli, "Runtime", FakeRuntime)
    monkeypatch.setattr(cli, "_json", output.append)
    monkeypatch.setattr("scout.discovery.query_engine.run_queries",
                        lambda http, budget, now: ["https://example.org/feed.xml"])

    args = cli.build_parser().parse_args(["sources", "discover"])
    args.func(args)

    assert output == [{"urls": ["https://example.org/feed.xml"],
                       "sources": [{"id": "discovered"}]}]


def test_sources_health_seeds_configured_sources(tmp_path, monkeypatch):
    output = []
    monkeypatch.setattr(cli, "_json", output.append)
    args = cli.build_parser().parse_args([
        "--state-dir", str(tmp_path), "--no-email", "sources", "health"
    ])

    assert args.func(args) == 0
    assert output[0]["sources_total"] == 21
    assert output[0]["enabled"] == 10
