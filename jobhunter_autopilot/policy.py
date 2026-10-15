"""The autopilot policy: what to apply to, how often, and whether to submit.

Stored as private JSON (0600, atomic rename) outside the repository so a
deploy never overwrites it. A missing file means the shipped defaults:
disabled, rehearsal mode.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 1

ROLE_FAMILIES = (
    "senior_software_engineer",
    "lead_software_engineer",
    "software_architect",
    "backend_engineer",
    "engineering_manager",
)
MODES = ("rehearsal", "live")
COUNTRIES = ("es", "uae", "ksa", "ch")
COUNTRY_LABELS = {"es": "Spain", "uae": "United Arab Emirates", "ksa": "Saudi Arabia", "ch": "Switzerland"}
# Statuses a policy may include. interested/skipped/rejected/unavailable are
# his decisions or dead listings and can never be opted in.
INCLUDABLE_STATUSES = ("new", "archived", "delivery_pending")
NEVER_STATUSES = ("interested", "skipped", "rejected", "unavailable", "submitted")
COVER_LETTER_MODES = ("when_required", "never")
UNKNOWN_AVAILABILITY = ("defer",)
VIA = ("telegram", "cli")

DEFAULTS = {
    "schema_version": SCHEMA_VERSION,
    "enabled": False,
    "mode": "rehearsal",
    "paused": False,
    "paused_until": None,
    "min_score": 75,
    "roles": ["senior_software_engineer", "lead_software_engineer"],
    "preferred_stack": ["java", "spring", "kotlin", "microservices"],
    "locations": {"countries": ["es"], "cities": []},
    "companies": {"allow": [], "deny": []},
    "max_per_day": 3,
    "max_handoffs_per_run": 3,
    "review_batch_size": 20,
    "include_statuses": ["new", "archived"],
    "unknown_availability": "defer",
    "allow_account_creation": True,
    "cover_letter": "when_required",
    "salary_ask": {"quantile": 0.75, "round_to": 1000},
    "timezone": "Africa/Tunis",
    "updated_at": None,
    "updated_via": None,
}


class PolicyError(ValueError):
    """An invalid policy file or update; the message is safe to show."""


def state_dir():
    return Path(os.getenv("JOBHUNTER_STATE_DIR") or Path.home() / ".jobhunter" / "state").expanduser()


def default_path():
    override = os.getenv("JOBHUNTER_AUTOPILOT_POLICY")
    return Path(override).expanduser() if override else state_dir() / "auto_apply.json"


DEFAULT_PATH = Path.home() / ".jobhunter" / "state" / "auto_apply.json"


def defaults():
    return copy.deepcopy(DEFAULTS)


def _merge(base, extra):
    merged = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load(path=None):
    """The stored policy merged over the defaults; a missing file is the defaults."""
    path = Path(path).expanduser() if path else default_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return defaults()
    except OSError as exc:
        raise PolicyError(f"policy file is unreadable ({type(exc).__name__})") from None
    try:
        stored = json.loads(raw)
    except ValueError:
        raise PolicyError("policy file is not valid JSON") from None
    if not isinstance(stored, dict):
        raise PolicyError("policy file must hold a JSON object")
    policy = _merge(DEFAULTS, {k: v for k, v in stored.items() if k in DEFAULTS})
    validate(policy)
    return policy


def save(policy, path=None, *, via=None, now=None):
    """Validate, stamp and atomically write the policy with mode 0600."""
    path = Path(path).expanduser() if path else default_path()
    policy = copy.deepcopy(policy)
    policy["schema_version"] = SCHEMA_VERSION
    policy["updated_at"] = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    if via is not None:
        if via not in VIA:
            raise PolicyError(f"updated_via must be one of {', '.join(VIA)}")
        policy["updated_via"] = via
    validate(policy)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".new", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(policy, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    os.chmod(path, 0o600)
    return policy


# ── Validation ───────────────────────────────────────────────────────────────

def _int(value, key, low, high):
    if type(value) is not int or not low <= value <= high:
        raise PolicyError(f"{key} must be a whole number from {low} to {high}")
    return value


def _str_list(value, key, *, allowed=None, lower=True, limit=50, item_limit=80):
    if not isinstance(value, list) or len(value) > limit:
        raise PolicyError(f"{key} must be a list of at most {limit} values")
    cleaned = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > item_limit:
            raise PolicyError(f"{key} values must be non-empty text of at most {item_limit} characters")
        item = " ".join(item.split())
        item = item.lower() if lower else item
        if allowed is not None and item not in allowed:
            raise PolicyError(f"{key} value {item!r} is not one of: {', '.join(allowed)}")
        if item not in cleaned:
            cleaned.append(item)
    return cleaned


def _parse_aware(value, key):
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise PolicyError(f"{key} must be an ISO date-time") from None
    if when.tzinfo is None:
        raise PolicyError(f"{key} must include a timezone")
    return when


def validate(policy):
    """Raise PolicyError unless every field holds an allowed value; normalises lists in place."""
    if not isinstance(policy, dict):
        raise PolicyError("policy must be an object")
    for flag in ("enabled", "paused", "allow_account_creation"):
        if type(policy.get(flag)) is not bool:
            raise PolicyError(f"{flag} must be true or false")
    if policy.get("mode") not in MODES:
        raise PolicyError("mode must be rehearsal or live")
    if policy.get("paused_until") is not None:
        _parse_aware(policy["paused_until"], "paused_until")
    _int(policy.get("min_score"), "min_score", 0, 100)
    _int(policy.get("max_per_day"), "max_per_day", 0, 10)
    _int(policy.get("max_handoffs_per_run"), "max_handoffs_per_run", 0, 10)
    _int(policy.get("review_batch_size"), "review_batch_size", 1, 40)
    policy["roles"] = _str_list(policy.get("roles"), "roles", allowed=ROLE_FAMILIES)
    if not policy["roles"]:
        raise PolicyError("roles needs at least one role family")
    policy["preferred_stack"] = _str_list(policy.get("preferred_stack"), "preferred_stack")
    locations = policy.get("locations")
    if not isinstance(locations, dict):
        raise PolicyError("locations must be an object")
    locations["countries"] = _str_list(locations.get("countries"), "locations.countries", allowed=COUNTRIES)
    if not locations["countries"]:
        raise PolicyError("locations.countries needs at least one country")
    locations["cities"] = _str_list(locations.get("cities"), "locations.cities")
    companies = policy.get("companies")
    if not isinstance(companies, dict):
        raise PolicyError("companies must be an object")
    companies["allow"] = _str_list(companies.get("allow"), "companies.allow", lower=False, limit=200, item_limit=120)
    companies["deny"] = _str_list(companies.get("deny"), "companies.deny", lower=False, limit=200, item_limit=120)
    policy["include_statuses"] = _str_list(policy.get("include_statuses"), "include_statuses",
                                           allowed=INCLUDABLE_STATUSES)
    if not policy["include_statuses"]:
        raise PolicyError("include_statuses needs at least one status")
    if policy.get("unknown_availability") not in UNKNOWN_AVAILABILITY:
        raise PolicyError("unknown_availability must be defer")
    if policy.get("cover_letter") not in COVER_LETTER_MODES:
        raise PolicyError("cover_letter must be when_required or never")
    ask = policy.get("salary_ask")
    if not isinstance(ask, dict):
        raise PolicyError("salary_ask must be an object")
    quantile = ask.get("quantile")
    if type(quantile) not in (int, float) or not 0.5 <= quantile <= 0.95:
        raise PolicyError("salary_ask.quantile must be from 0.5 to 0.95")
    _int(ask.get("round_to"), "salary_ask.round_to", 1, 10000)
    tz = policy.get("timezone")
    if not isinstance(tz, str):
        raise PolicyError("timezone must be an IANA name such as Africa/Tunis")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise PolicyError("timezone must be an IANA name such as Africa/Tunis") from None
    if policy.get("updated_via") not in (None, *VIA):
        raise PolicyError("updated_via must be telegram or cli")
    return policy


# ── Updates ──────────────────────────────────────────────────────────────────

_BOOLS = {"true": True, "yes": True, "on": True, "1": True,
          "false": False, "no": False, "off": False, "0": False}
_INT_KEYS = {"min_score": (0, 100), "max_per_day": (0, 10), "max_handoffs_per_run": (0, 10),
             "review_batch_size": (1, 40), "salary_ask.round_to": (1, 10000)}
_LIST_KEYS = {"roles", "preferred_stack", "locations.countries", "locations.cities",
              "companies.allow", "companies.deny", "include_statuses"}
_BOOL_KEYS = {"enabled", "paused", "allow_account_creation"}
_TEXT_KEYS = {"mode", "unknown_availability", "cover_letter", "timezone"}
KEYS = tuple(sorted(_INT_KEYS.keys() | _LIST_KEYS | _BOOL_KEYS | _TEXT_KEYS
                    | {"paused_until", "salary_ask.quantile"}))
_ROLE_ALIASES = {
    "senior": "senior_software_engineer", "senior software engineer": "senior_software_engineer",
    "lead": "lead_software_engineer", "lead software engineer": "lead_software_engineer",
    "tech lead": "lead_software_engineer", "architect": "software_architect",
    "software architect": "software_architect", "backend": "backend_engineer",
    "backend engineer": "backend_engineer", "engineering manager": "engineering_manager",
    "manager": "engineering_manager",
}
_COUNTRY_ALIASES = {"spain": "es", "españa": "es", "dubai": "uae", "emirates": "uae",
                    "united arab emirates": "uae", "saudi": "ksa", "saudi arabia": "ksa",
                    "switzerland": "ch"}


def _split(value):
    return [" ".join(part.split()) for part in value.split(",") if part.strip()]


def _normalise_items(key, items):
    if key == "roles":
        return [_ROLE_ALIASES.get(item.lower(), item.lower().replace(" ", "_").replace("-", "_")) for item in items]
    if key == "locations.countries":
        return [_COUNTRY_ALIASES.get(item.lower(), item.lower()) for item in items]
    return items


def _get(policy, dotted):
    node = policy
    for part in dotted.split(".")[:-1]:
        node = node[part]
    return node, dotted.split(".")[-1]


def apply_update(policy, assignment):
    """Return a new policy with one ``key=value`` (or list ``key+=v`` / ``key-=v``) applied."""
    if not isinstance(assignment, str):
        raise PolicyError("updates are written key=value")
    match = re.fullmatch(r"\s*([a-z_.]+)\s*([+-]?=)(.*)", assignment, re.S)
    if not match:
        raise PolicyError("updates are written key=value")
    key, operator, raw = match.group(1), match.group(2), match.group(3).strip()
    if key not in KEYS:
        raise PolicyError(f"unknown setting {key!r}; settable: {', '.join(KEYS)}")
    if operator != "=" and key not in _LIST_KEYS:
        raise PolicyError(f"{key} is not a list; use {key}=value")
    updated = copy.deepcopy(policy)
    node, leaf = _get(updated, key)
    if key in _INT_KEYS:
        low, high = _INT_KEYS[key]
        if not re.fullmatch(r"-?\d+", raw):
            raise PolicyError(f"{key} must be a whole number from {low} to {high}")
        node[leaf] = _int(int(raw), key, low, high)
    elif key in _BOOL_KEYS:
        if raw.lower() not in _BOOLS:
            raise PolicyError(f"{key} must be true or false")
        node[leaf] = _BOOLS[raw.lower()]
    elif key in _LIST_KEYS:
        items = _normalise_items(key, _split(raw))
        current = list(node.get(leaf) or [])
        if operator == "=":
            node[leaf] = items
        elif operator == "+=":
            node[leaf] = current + [item for item in items if item.casefold() not in {c.casefold() for c in current}]
        else:
            drop = {item.casefold() for item in items}
            node[leaf] = [item for item in current if item.casefold() not in drop]
    elif key == "salary_ask.quantile":
        try:
            node[leaf] = float(raw)
        except ValueError:
            raise PolicyError("salary_ask.quantile must be from 0.5 to 0.95") from None
    elif key == "paused_until":
        node[leaf] = None if raw.lower() in ("", "null", "none") else _parse_aware(raw, key).isoformat()
    elif key == "mode":
        node[leaf] = raw.lower()
    else:
        node[leaf] = raw
    validate(updated)
    return updated


def parse_until(value, *, now=None, tz="Africa/Tunis"):
    """``12h``, ``3d``, ``2w``, a date (end of that day locally) or an ISO date-time."""
    now = now or datetime.now(timezone.utc)
    value = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,3})\s*([hdw])", value.lower())
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        delta = {"h": timedelta(hours=amount), "d": timedelta(days=amount), "w": timedelta(weeks=amount)}[unit]
        return (now + delta).astimezone(timezone.utc).isoformat(timespec="seconds")
    try:
        day = date.fromisoformat(value)
    except ValueError:
        day = None
    if day is not None:
        return datetime.combine(day, time(23, 59, 59), ZoneInfo(tz)).isoformat()
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        when = None
    if when is None:
        raise PolicyError("pause --until takes 12h, 3d, 2w, a date or an ISO date-time")
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo(tz))
    return when.isoformat()


def is_paused(policy, now=None):
    """Paused without an end, or until a time not yet reached."""
    if not policy.get("paused"):
        return False
    until = policy.get("paused_until")
    if not until:
        return True
    now = now or datetime.now(timezone.utc)
    return now < _parse_aware(until, "paused_until")


def is_active(policy, now=None):
    return bool(policy.get("enabled")) and not is_paused(policy, now)


def zone(policy):
    return ZoneInfo(policy.get("timezone") or DEFAULTS["timezone"])


def _label(value):
    return value.replace("_", " ")


def summary_text(policy, now=None):
    """A few short plain-text lines for a Telegram confirmation."""
    if not policy.get("enabled"):
        state = "OFF"
    elif is_paused(policy, now):
        until = policy.get("paused_until")
        state = f"PAUSED{' until ' + until[:16].replace('T', ' ') if until else ''}"
    else:
        state = "ON"
    mode = "live: submits applications" if policy["mode"] == "live" else "rehearsal: fills forms, never submits"
    countries = ", ".join(COUNTRY_LABELS.get(c, c) for c in policy["locations"]["countries"])
    cities = ", ".join(c.title() for c in policy["locations"]["cities"]) or "all cities"
    allow = ", ".join(policy["companies"]["allow"]) or "any"
    deny = ", ".join(policy["companies"]["deny"]) or "none"
    return "\n".join([
        f"Auto apply: {state} ({mode})",
        f"Min score {policy['min_score']} · max {policy['max_per_day']}/day · "
        f"roles: {', '.join(_label(r) for r in policy['roles'])}",
        f"Locations: {countries} ({cities}) · stack boost: {', '.join(policy['preferred_stack']) or 'none'}",
        f"Companies: allow {allow} · deny {deny}",
    ])
