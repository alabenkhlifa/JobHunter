"""Record failure categories without exception messages or candidate content."""
from __future__ import annotations

import re


def error_category(error):
    # Telegram intentionally replaces request errors with a public-safe error.
    # The chained class still distinguishes a timeout from a rejected request.
    current = error
    seen = set()
    for _ in range(5):
        if id(current) in seen:
            break
        seen.add(id(current))
        name = type(current).__name__
        if name in {'Timeout', 'ReadTimeout', 'ConnectTimeout', 'TimeoutExpired'}:
            return 'timeout'
        cause = current.__cause__ or current.__context__
        if cause is None:
            break
        current = cause
    name = type(error).__name__
    return name if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}', name) else 'ExecutionError'
