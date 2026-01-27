"""
Metrics and timing utilities - now using PostgreSQL instead of SQLite
"""
from db import (
    increment_metric,
    get_metric,
    list_metrics,
    record_timing,
    get_timing,
    list_timings,
)

__all__ = [
    "increment_metric",
    "get_metric",
    "list_metrics",
    "record_timing",
    "get_timing",
    "list_timings",
]

