"""Low-cardinality Prometheus metrics for the web, worker and agent paths.

Every label here has a bounded set of values. A chat id, a project id or a file
name as a label would make Prometheus grow one time series per user, which is
how a metrics endpoint becomes the most expensive part of a small deployment.
"""
from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "reception_http_requests_total",
    "HTTP requests handled by the application.",
    ("method", "path", "status"),
)
HTTP_REQUEST_SECONDS = Histogram(
    "reception_http_request_duration_seconds",
    "HTTP request latency.",
    ("method", "path"),
)

QUEUE_EVENTS = Counter(
    "reception_queue_events_total",
    "Durable queue lifecycle events.",
    ("event",),
)
QUEUE_DEPTH = Gauge(
    "reception_queue_depth",
    "Number of Telegram updates in each Redis queue.",
    ("queue",),
)
UPDATE_SECONDS = Histogram(
    "reception_update_processing_duration_seconds",
    "Time spent processing one Telegram update.",
    ("result",),
)

GRAPH_RUNS = Counter(
    "reception_graph_runs_total",
    "Completed LangGraph runs.",
    ("task", "result"),
)
GRAPH_SECONDS = Histogram(
    "reception_graph_run_duration_seconds",
    "End-to-end LangGraph execution latency.",
    ("task",),
)

CACHE_REQUESTS = Counter(
    "reception_cache_requests_total",
    "Cache lookup outcomes.",
    ("namespace", "result"),
)

LLM_CALLS = Counter(
    "reception_llm_calls_total",
    "LLM call outcomes.",
    ("provider", "model", "result"),
)
LLM_SECONDS = Histogram(
    "reception_llm_call_duration_seconds",
    "LLM provider latency.",
    ("provider", "model"),
)
LLM_TOKENS = Counter(
    "reception_llm_tokens_total",
    "Tokens reported by LLM providers.",
    ("provider", "model", "direction"),
)

WORKSPACE_CALLS = Counter(
    "reception_workspace_calls_total",
    "Google Workspace / Microsoft 365 call outcomes.",
    ("provider", "capability", "result"),
)
WORKSPACE_SECONDS = Histogram(
    "reception_workspace_call_duration_seconds",
    "Google Workspace / Microsoft 365 latency.",
    ("provider", "capability"),
)

EXTERNAL_CALLS = Counter(
    "reception_external_calls_total",
    "External dependency call outcomes (transcription, weather, tax lookups).",
    ("dependency", "result"),
)
EXTERNAL_SECONDS = Histogram(
    "reception_external_call_duration_seconds",
    "External dependency latency.",
    ("dependency",),
)

DEPENDENCY_READY = Gauge(
    "reception_dependency_ready",
    "Whether a required dependency passed its latest readiness check.",
    ("dependency",),
)

MEDIA_CLEANUP_RUNS = Counter(
    "media_cleanup_runs_total",
    "Passes of the media cleanup (ok, partial: some item failed, error).",
    ("result",),
)
MEDIA_CLEANUP_LAST_SUCCESS = Gauge(
    "media_cleanup_last_success",
    "Unix time of the last media cleanup pass in which every item went through.",
)

LOG_ERRORS = Counter(
    "reception_log_errors_total",
    "ERROR records in the bot's log, by logger (core/services/error_alerts.py).",
    ("logger",),
)

CALENDAR_SYNC_RUNS = Counter(
    "calendar_sync_runs_total",
    "Passes of the agenda mirror (ok, partial: some calendar failed, error, skipped).",
    ("result",),
)
CALENDAR_SYNC_LAST_SUCCESS = Gauge(
    "calendar_sync_last_success",
    "Unix time of the last agenda mirror pass in which every calendar was read.",
)


def observe_llm(
    provider: str,
    model: str,
    result: str,
    seconds: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Record one provider call without logging prompts or user identifiers."""
    LLM_CALLS.labels(provider, model, result).inc()
    LLM_SECONDS.labels(provider, model).observe(seconds)
    if input_tokens:
        LLM_TOKENS.labels(provider, model, "input").inc(input_tokens)
    if output_tokens:
        LLM_TOKENS.labels(provider, model, "output").inc(output_tokens)


def observe_workspace(provider: str, capability: str, result: str, seconds: float) -> None:
    WORKSPACE_CALLS.labels(provider, capability, result).inc()
    WORKSPACE_SECONDS.labels(provider, capability).observe(seconds)


def observe_external(dependency: str, result: str, seconds: float) -> None:
    EXTERNAL_CALLS.labels(dependency, result).inc()
    EXTERNAL_SECONDS.labels(dependency).observe(seconds)
