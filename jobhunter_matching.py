"""Validated search preferences and candidate-specific destination eligibility.

Generic profiles never inherit the owner's profession, stack, or legal status.
These helpers are pure; callers keep profile configuration and data ownership.
"""

from copy import deepcopy
import math
import re


DEFAULT_WEIGHTS = {"stack": 35, "role": 30, "seniority": 15, "employer": 12, "freshness": 8}
AUTHORIZATION_STATES = {"authorized", "sponsorship_required", "unknown"}
REFUSES_SPONSORSHIP = (
    "no visa sponsorship", "no sponsorship", "will not sponsor", "won't sponsor",
    "does not sponsor", "unable to sponsor", "not able to sponsor",
    "sponsorship is not available", "not offer sponsorship", "not offering visa",
    "sponsorship not available", "sponsorship is not provided", "sponsorship not provided",
    "do not provide sponsorship", "does not provide sponsorship", "cannot sponsor",
    "must hold a valid work permit", "existing work permit",
    "valid work permit for switzerland", "eu citizens only", "efta citizens only",
)
REFUSES_RELOCATION = ("no relocation", "relocation is not available", "relocation not provided")
REQUIRES_LOCAL_PRESENCE = (
    "local candidates only", "local hires only", "must be based in",
    "must be located in", "must be residing", "must currently reside",
    "candidates must be in", "candidates already in", "locally based",
    "residents only", "must already be based", "must already live",
)


def _strings(value, field):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(f"{field} must be a list of nonempty strings")
    if len(value) > 200 or any(len(v) > 200 for v in value):
        raise ValueError(f"{field} exceeds the supported size")
    return list(dict.fromkeys(v.strip() for v in value))


def _integer(value, field, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{field} must be an integer from {low} to {high}")
    return value


def validate_markets(markets):
    if not isinstance(markets, list) or len(markets) > 30:
        raise ValueError("markets must be a list with at most 30 destinations")
    result, names = [], set()
    for market in markets:
        if not isinstance(market, dict):
            raise ValueError("each market must be an object")
        unknown = set(market) - {"name", "locations", "work_authorization", "relocation_required", "salary_target"}
        if unknown:
            raise ValueError(f"unknown market fields: {', '.join(sorted(unknown))}")
        name = market.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("market.name must be a nonempty string up to 100 characters")
        if name.strip().lower() in names:
            raise ValueError("market names must be unique")
        names.add(name.strip().lower())
        locations = _strings(market.get("locations"), "market.locations")
        if not locations:
            raise ValueError("market.locations cannot be empty")
        authorization = market.get("work_authorization", "unknown")
        if authorization not in AUTHORIZATION_STATES:
            raise ValueError("invalid market.work_authorization")
        relocation = market.get("relocation_required")
        if type(relocation) is not bool:
            raise ValueError("market.relocation_required must be an explicit boolean")
        normalized = {"name": name.strip(), "locations": locations,
                      "work_authorization": authorization, "relocation_required": relocation}
        if "salary_target" in market:
            normalized["salary_target"] = validate_salary_target(market["salary_target"])
        result.append(normalized)
    return result


def validate_salary_target(value):
    if not isinstance(value, dict) or set(value) != {"amount", "currency", "period"}:
        raise ValueError("salary_target requires amount, currency and period")
    amount = value["amount"]
    try:
        finite = type(amount) in (int, float) and math.isfinite(amount)
    except OverflowError:
        finite = False
    if not finite or amount <= 0:
        raise ValueError("salary_target.amount must be a positive finite number")
    if not isinstance(value["currency"], str) or not re.fullmatch(r"[A-Z]{3}", value["currency"]):
        raise ValueError("salary_target.currency must contain three uppercase letters")
    if value["period"] not in ("month", "year"):
        raise ValueError("salary_target.period must be month or year")
    return dict(value)


def salary_target_for_job(job, markets):
    """Return the destination's unchanged target dict, or None; never convert."""
    policy = resolve_market(job.get("location"), markets)
    target = policy.get("salary_target") if policy else None
    return dict(target) if target else None


def salary_comparison(job, markets):
    """Conservative comparison of a source-extracted salary in the same units.

    Only an explicit currency code, period, and amount/range copied from the
    listing with a salary label qualify. Symbols, estimates, missing periods,
    FX conversion, and different gross/net bases remain unknown for AI review.
    """
    target = salary_target_for_job(job, markets)
    if target is None:
        return "no_target"
    raw = str(job.get("salary") or "").strip()
    description = str(job.get("description") or "")
    if not raw or raw.lower() not in description.lower():
        return "unknown"
    context = description[max(0, description.lower().find(raw.lower()) - 60):description.lower().find(raw.lower())] + raw
    if re.search(r"\b(?:gross|net)\b", context, re.I):
        # The configured target does not declare a gross/net basis.
        return "unknown"
    if not re.search(r"\b(?:salary|compensation|base pay)\b", context, re.I):
        return "unknown"
    amount = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?"
    pattern = (r"(?:(?:salary|compensation|base pay)\s*(?:range)?\s*[:=]?\s*)?"
               r"(?P<currency>[A-Z]{3})\s*(?P<low>" + amount + r")"
               r"(?:\s*(?:-|–|to)\s*(?:(?P<other_currency>[A-Z]{3})\s*)?(?P<high>" + amount + r"))?"
               r"\s*(?:per\s+|/\s*)(?P<period>month|year)")
    match = re.fullmatch(pattern, raw, re.I)
    if not match or match["currency"].upper() != target["currency"] or match["period"].lower() != target["period"]:
        return "unknown"
    if match["other_currency"] and match["other_currency"].upper() != target["currency"]:
        return "unknown"
    low = float(match["low"].replace(",", ""))
    high = float((match["high"] or match["low"]).replace(",", ""))
    if not math.isfinite(low) or not math.isfinite(high) or low <= 0 or high < low:
        return "unknown"
    if high < target["amount"]:
        return "below"
    return "met" if low >= target["amount"] else "unknown"


def validate_matching(matching):
    if not isinstance(matching, dict):
        raise ValueError("matching must be an object")
    allowed = {"preset", "preferred_roles", "excluded_roles", "preferred_technologies",
               "excluded_technologies", "seniority", "weights", "feedback_enabled"}
    if set(matching) - allowed:
        raise ValueError("unknown matching fields: " + ", ".join(sorted(set(matching) - allowed)))
    result = deepcopy(matching)
    preset = result.setdefault("preset", "generic")
    if preset not in {"generic", "legacy"}:
        raise ValueError("matching.preset must be generic or legacy")
    for key in ("preferred_roles", "excluded_roles", "preferred_technologies", "excluded_technologies"):
        result[key] = _strings(result.get(key, []), f"matching.{key}")
    seniority = result.setdefault("seniority", {})
    if not isinstance(seniority, dict) or set(seniority) - {
        "min_years", "max_years", "preferred_min_years", "preferred_max_years", "excluded_titles"
    }:
        raise ValueError("invalid matching.seniority")
    for key, default in (("min_years", 0), ("max_years", 30),
                         ("preferred_min_years", 0), ("preferred_max_years", 30)):
        seniority[key] = _integer(seniority.get(key, default), f"seniority.{key}", 0, 60)
    if seniority["min_years"] > seniority["max_years"] or seniority["preferred_min_years"] > seniority["preferred_max_years"]:
        raise ValueError("seniority minimum cannot exceed maximum")
    seniority["excluded_titles"] = _strings(seniority.get("excluded_titles", []), "seniority.excluded_titles")
    weights = result.setdefault("weights", dict(DEFAULT_WEIGHTS))
    if not isinstance(weights, dict) or set(weights) != set(DEFAULT_WEIGHTS):
        raise ValueError("matching.weights must name all five scoring dimensions")
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in weights.values()) or not math.isclose(sum(weights.values()), 100):
        raise ValueError("matching.weights must be finite nonnegative numbers summing to 100")
    if type(result.setdefault("feedback_enabled", False)) is not bool:
        raise ValueError("matching.feedback_enabled must be a boolean")
    return result


def validate_config(config):
    """Return normalized preferences; an incomplete generic draft stays inert.

    Missing ``matching`` preserves existing legacy configs. New account creators
    must explicitly set ``matching.preset=generic``. Empty generic markets and
    keywords are valid drafts, but cannot collect or authorize any matches.
    """
    if not isinstance(config, dict):
        raise ValueError("profile config must be an object")
    result = deepcopy(config)
    if "matching" in result:
        result["matching"] = validate_matching(result["matching"])
    generic = result.get("matching", {}).get("preset") == "generic"
    if "markets" in result or generic:
        result["markets"] = validate_markets(result.get("markets", []))
        result["regions"] = {m["name"]: m["locations"] for m in result["markets"]}
        result["allowed_locations"] = list(dict.fromkeys(v for m in result["markets"] for v in m["locations"]))
    if generic:
        matching = result["matching"]
        result["keywords"] = _strings(result.get("keywords", matching["preferred_roles"]), "keywords")
        result["exclude_terms"] = list(matching["excluded_roles"])
        result["local_presence_phrases"] = []
        result["tech_terms"] = list(dict.fromkeys(matching["preferred_technologies"] + matching["excluded_technologies"]))
        result["max_experience"] = matching["seniority"]["max_years"]
    for field, default, low, high in (
        ("score_threshold", 45, 0, 100), ("max_job_age_days", 7, 1, 365),
        ("min_matching_jobs", 25, 1, 200), ("max_pages", 10, 1, 100),
    ):
        result[field] = _integer(result.get(field, default), field, low, high)
    delivery = result.setdefault("delivery", {})
    if not isinstance(delivery, dict) or set(delivery) - {"per_market", "cap"}:
        raise ValueError("delivery only supports per_market and cap")
    for key, default in (("per_market", 3), ("cap", 12)):
        delivery[key] = _integer(delivery.get(key, default), f"delivery.{key}", 1, 12)
    return result


def contains_term(text, term):
    """Match literal phrases, including punctuation-bearing technologies."""
    return bool(re.search(r"(?<!\w)" + re.escape(term.lower()) + r"(?!\w)", str(text or "").lower()))


def resolve_market(location, markets):
    matched = [m for m in markets if any(contains_term(location, loc) for loc in m["locations"])]
    return matched[0] if len(matched) == 1 else None


def eligibility_reason(job, markets):
    """Hard collection/review constraints; unknown legal status stays reviewable."""
    market = resolve_market(job.get("location"), markets)
    if market is None:
        return "outside or ambiguous configured markets"
    description = str(job.get("description") or "").lower().replace("’", "'").replace("‘", "'")
    if market["work_authorization"] == "sponsorship_required":
        for phrase in REFUSES_SPONSORSHIP:
            if phrase in description:
                return f"requires sponsorship but posting excludes it: {phrase}"
    if market["relocation_required"]:
        for phrase in REFUSES_RELOCATION + REQUIRES_LOCAL_PRESENCE:
            if phrase in description:
                return f"requires relocation but posting requires local presence: {phrase}"
    return None


def review_sendable(job, markets):
    """Apply candidate authorization after AI review without inferring rights."""
    if job.get("location"):
        market = resolve_market(job["location"], markets)
        if eligibility_reason(job, markets):
            return False
    else:
        matched = [m for m in markets if m["name"].lower() == str(job.get("market") or "").lower()]
        market = matched[0] if len(matched) == 1 else None
    if market is None or market["work_authorization"] == "unknown":
        return False
    return market["work_authorization"] == "authorized" or job.get("ai_sponsorship") == "offered"


def excluded_reason(job, matching):
    title = str(job.get("title") or "")
    for term in matching["excluded_roles"] + matching["seniority"]["excluded_titles"]:
        if contains_term(title, term):
            return f"excluded title: {term}"
    text = " ".join(str(job.get(k) or "") for k in ("title", "description", "tech_required"))
    for term in matching["excluded_technologies"]:
        if contains_term(text, term):
            return f"excluded technology: {term}"
    years = job.get("min_experience", -1)
    if type(years) is int and years >= 0:
        seniority = matching["seniority"]
        if not seniority["min_years"] <= years <= seniority["max_years"]:
            return "outside configured experience range"
    return None
