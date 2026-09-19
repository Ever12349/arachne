"""Prometheus series dual-written with in-process Stats."""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

REQUESTS_TOTAL = Counter("arachne_requests_total", "Extract and suggest pipeline requests")
ERRORS_TOTAL = Counter("arachne_errors_total", "Extract and suggest pipeline errors", ["code"])
IN_FLIGHT = Gauge("arachne_in_flight", "In-flight extract and suggest requests")
CACHE_HITS_TOTAL = Counter("arachne_cache_hits_total", "Extract cache hits")
CACHE_MISSES_TOTAL = Counter("arachne_cache_misses_total", "Extract cache misses")
REQUEST_LATENCY_SECONDS = Histogram(
    "arachne_request_latency_seconds",
    "Extract and suggest pipeline latency in seconds",
)

METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST


def render_metrics() -> bytes:
    return generate_latest()
