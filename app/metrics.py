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
QPS_WAITS_TOTAL = Counter(
    "arachne_qps_waits_total",
    "Extract and suggest requests that waited for a QPS slot",
)
# Histogram so the exposition includes arachne_qps_wait_seconds_sum (seconds).
# /stats keeps the same total in milliseconds as qps_wait_ms_sum.
QPS_WAIT_SECONDS = Histogram(
    "arachne_qps_wait_seconds",
    "Time spent waiting for a QPS slot, in seconds",
)
AUTO_RENDERS_TOTAL = Counter(
    "arachne_auto_renders_total",
    "Successful Playwright fallbacks after challenge_detected when render was false",
)

METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST


def render_metrics() -> bytes:
    return generate_latest()
