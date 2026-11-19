from opentelemetry import trace

from baranda.telemetry import configure


def test_no_endpoint_means_no_export(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    assert configure("test") is False


def test_endpoint_configures_an_sdk_provider(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    assert configure("baranda-test") is True
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    assert provider.resource.attributes["service.name"] == "baranda-test"
    provider.shutdown()  # do not try to export the rest of the suite's spans to a dead endpoint
