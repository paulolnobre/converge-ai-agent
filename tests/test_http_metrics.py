"""HTTP metrics keep a bounded set of labels: anyone on the internet can ask for
any path or send any method, and each new label value is a new time series
kept in the bot's memory forever."""
from fastapi.testclient import TestClient

from core.metrics import HTTP_REQUEST_SECONDS, HTTP_REQUESTS
from core.web.app import app


def _labels(metric, name: str) -> set[str]:
    return {sample.labels[name] for family in metric.collect() for sample in family.samples
            if name in sample.labels}


def test_unknown_paths_share_one_label():
    client = TestClient(app)
    for n in range(30):
        client.get(f"/probe-{n}-does-not-exist")
    paths = _labels(HTTP_REQUESTS, "path")
    assert not any(path.startswith("/probe-") for path in paths)
    assert "unmatched" in paths
    assert not any(path.startswith("/probe-") for path in _labels(HTTP_REQUEST_SECONDS, "path"))


def test_a_route_with_an_id_is_labelled_by_its_template():
    client = TestClient(app)
    for contact_id in (101, 202, 303):
        client.get(f"/painel/api/inbox/{contact_id}")  # 401 without a session: still that route
    paths = _labels(HTTP_REQUESTS, "path")
    assert "/painel/api/inbox/{contact_id}" in paths
    assert not any(path in paths for path in ("/painel/api/inbox/101", "/painel/api/inbox/202"))


def test_an_invented_method_does_not_become_a_label():
    client = TestClient(app)
    client.request("BREW", "/health")
    assert "BREW" not in _labels(HTTP_REQUESTS, "method")
    assert "OTHER" in _labels(HTTP_REQUESTS, "method")
