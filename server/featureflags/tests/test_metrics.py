"""
With Prometheus metrics on, every request goes through the instrumentator;
the test configs leave metrics off, so this path needs its own test.
"""

import httpx

from featureflags import metrics
from featureflags.http.app import create_app


async def test_requests_are_served_with_metrics_enabled(monkeypatch):
    # the metrics endpoint server itself is not under test
    monkeypatch.setattr(metrics, "start_http_server", lambda port: None)
    app = create_app()
    metrics.configure_metrics(port=9100, app=app)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.get("/~health")

    assert response.status_code == 200
