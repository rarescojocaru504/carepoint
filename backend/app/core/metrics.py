"""Prometheus metrics (ADR-029), served at GET /metrics.

HTTP metrics are counted by the request middleware and labelled with the
route *template* (`/api/v1/reservations/{reservation_id}`), never the raw path,
so the number of series stays fixed. Business gauges are filled in from the
database when the metrics are read (app/services/ops_metrics.py).
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter(
    "carepoint_http_requests_total",
    "HTTP requests handled, by method, route template and status code.",
    ["method", "route", "status"],
    registry=REGISTRY,
)
HTTP_DURATION = Histogram(
    "carepoint_http_request_duration_seconds",
    "Time to handle an HTTP request (for event streams: how long the stream was open).",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 60, 900),
    registry=REGISTRY,
)
MACHINES = Gauge(
    "carepoint_machines",
    "Machines by state: online, offline, maintenance (decommissioned ones excluded).",
    ["state"],
    registry=REGISTRY,
)
RESERVATIONS = Gauge(
    "carepoint_reservations",
    "Reservations by effective status (a lapsed hold counts as expired).",
    ["status"],
    registry=REGISTRY,
)
UNITS_AVAILABLE = Gauge(
    "carepoint_units_available",
    "Units customers can reserve right now, across active machines.",
    registry=REGISTRY,
)
