"""Registry of the Spain job boards searched beside LinkedIn.

Added on 2026-10-01 when the markets narrowed to Valencia, Madrid and
Barcelona. Each board lives in its own module (see ``base`` for the
contract); this package only knows their names, so ``scraper`` and
``jobhunter_availability`` can look a board up by display name, by job id
prefix or by listing host without importing every module up front. A board
is only searched for a profile with a configured location in one of its
``Board.countries``.
"""

from __future__ import annotations

import importlib
import logging

from .base import Board  # noqa: F401  (re-exported for callers)

log = logging.getLogger("scraper")

# Collection order. Both boards are the owner's high-priority
# English-speaking boards and run before LinkedIn in each round of the
# round-robin, so a posting both show at the same page depth survives
# deduplication as the board's copy, with its language, visa and
# employer-link metadata. A copy LinkedIn surfaces on an earlier page, or one
# stored on a previous night, still wins.
BOARD_MODULES = (
    "jobhunter_sources.spain_devjobs",
    "jobhunter_sources.spainjobs_io",
)

_modules = {}


def module_for(board):
    """The imported module behind a registry entry."""
    if board.module not in _modules:
        _modules[board.module] = importlib.import_module(board.module)
    return _modules[board.module]


def boards():
    """Every registered board, in collection order.

    A board whose module will not import is left out with a logged error:
    one broken board must not take LinkedIn down with it.
    """
    found = []
    for name in BOARD_MODULES:
        if name not in _modules:
            try:
                _modules[name] = importlib.import_module(name)
            except Exception as error:  # noqa: BLE001 - a broken board is logged, never fatal
                _modules[name] = None
                log.error(f"{name}: board unavailable, skipped: {error!r}")
        if _modules[name] is not None:
            found.append(_modules[name].BOARD)
    return tuple(found)


def by_name(name):
    wanted = str(name or "").strip().casefold()
    if not wanted:
        return None
    for board in boards():
        if board.name.casefold() == wanted or board.key == wanted:
            return board
    return None


def by_key(key):
    wanted = str(key or "").strip().casefold()
    for board in boards():
        if board.key == wanted:
            return board
    return None


def by_host(host):
    wanted = str(host or "").strip().lower()
    for board in boards():
        if wanted in board.hosts:
            return board
    return None


def by_job_id(job_id):
    value = str(job_id or "")
    for board in boards():
        if value.startswith(board.prefix):
            return board
    return None
