"""Explicit local-time schedules; a unique local slot prevents DST duplicates."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_schedule(value):
    if not isinstance(value, dict) or set(value) - {'timezone', 'time', 'weekdays', 'enabled'}:
        raise ValueError('Invalid schedule fields.')
    result = {'timezone': 'UTC', 'time': '09:00', 'weekdays': list(range(7)), 'enabled': False, **value}
    try:
        ZoneInfo(result['timezone'])
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError('Choose an IANA timezone, for example Africa/Tunis.') from None
    try:
        parsed = datetime.strptime(result['time'], '%H:%M')
        if parsed.strftime('%H:%M') != result['time']:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError('Schedule time must be HH:MM.') from None
    days = result['weekdays']
    if not isinstance(days, list) or not days or any(type(x) is not int or x not in range(7) for x in days):
        raise ValueError('Choose weekdays from 0 (Monday) through 6 (Sunday).')
    if type(result['enabled']) is not bool:
        raise ValueError('Schedule enabled must be a boolean.')
    result['weekdays'] = sorted(set(days))
    return result


def next_run(schedule, now=None):
    settings = validate_schedule(schedule)
    if not settings['enabled']:
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('A timezone-aware time is required.')
    zone = ZoneInfo(settings['timezone'])
    local = now.astimezone(zone)
    hour, minute = map(int, settings['time'].split(':'))
    for n in range(9):
        day = local.date() + timedelta(days=n)
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
        # Spring DST gaps are skipped; the first occurrence wins in fall.
        if candidate.astimezone(timezone.utc).astimezone(zone).replace(fold=0) != candidate:
            continue
        if day.weekday() in settings['weekdays'] and candidate.astimezone(timezone.utc) > now:
            return candidate
    raise ValueError('No valid schedule occurrence found.')


def due_slot(schedule, now=None):
    settings = validate_schedule(schedule)
    if not settings['enabled']:
        return None
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo(settings['timezone']))
    if local.weekday() in settings['weekdays'] and local.strftime('%H:%M') == settings['time']:
        return local.strftime('%Y-%m-%dT%H:%M') + '@' + settings['timezone']
    return None
