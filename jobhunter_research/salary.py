"""Salary evidence: published pay, company-specific and market figures, benchmarks.

Every source keeps its evidence text in ``snippet``; ranges are always
recomputed from those snippets, so a cached result and a fresh one render the
same numbers. Market and company figures come from Brave result snippets
(Glassdoor/Indeed only reach us this way) and from two direct pages that
answer from the Pi: Levels.fyi Spain and es.talent.com.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable
from urllib.parse import quote_plus, urlparse

from jobhunter_research import roles

BENCHMARKS_PATH = Path(__file__).resolve().parent / "salary_benchmarks.json"
TRUSTED_SALARY_SOURCES = ("Spain Dev Jobs", "SpainJobs.io")
LEVELS_BASE_URL = "https://www.levels.fyi/t/"
TALENT_URL = "https://es.talent.com/salary?job={job}&location={location}"
MARKET_SITES = (
    ("glassdoor.", "Glassdoor"),
    ("levels.fyi", "Levels.fyi"),
    ("indeed.", "Indeed"),
    ("payscale.", "PayScale"),
    ("talent.com", "talent.com"),
    ("salaryexpert.", "SalaryExpert"),
    ("gulftalent.", "GulfTalent"),
    ("jobted.", "Jobted"),
)
CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}
# Plausible gross pay per period; outside this a figure is a typo, an hourly
# rate or a funding round.
PLAUSIBLE = {"year": (12000, 600000), "month": (1000, 200000)}

_CUR = r"(?:€|(?:EUR|euros?|AED|SAR|CHF|USD|GBP)(?![A-Za-z])|US\$|\$|£)"
_NUM = (
    r"(?:\d{1,3}(?:[.,]\d{3})+(?:,\d{1,2})?(?![\d])"
    r"|\d+(?:[.,]\d+)?\s?[kKmM](?![A-Za-z])"
    r"|\d{4,}(?:[.,]\d{1,2})?(?![\d]))"
)
_SHORT = r"\d{1,3}(?:[.,]\d)?"
_SEP = r"\s*(?:-|–|—|to|a|y|hasta|and)\s*"
_RANGE = re.compile(
    rf"(?:(?P<c1>{_CUR})\s?)?(?P<a>{_NUM}|{_SHORT})(?:\s?(?P<c2>{_CUR}))?\+?{_SEP}"
    rf"(?:(?P<c3>{_CUR})\s?)?(?P<b>{_NUM})(?:\s?(?P<c4>{_CUR}))?",
    re.IGNORECASE,
)
_SINGLE = re.compile(rf"(?P<c1>{_CUR})\s?(?P<a>{_NUM})|(?P<b>{_NUM})\s?(?P<c2>{_CUR})", re.IGNORECASE)
_YEAR_AFTER = re.compile(
    r"^\s*\+?\s*(?:/\s*(?:year|yr|annum|a(?:ñ|n)o)\b|per\s+(?:year|annum)|a\s+year|yearly|annual(?:ly)?|p\.?\s?a\.?(?![a-z])"
    r"|(?:al|por)\s+a(?:ñ|n)o|anual(?:es)?|brut[oa]s?\s+anual(?:es)?|gross\s*/?\s*(?:per\s+)?(?:year|annual))",
    re.IGNORECASE,
)
_MONTH_AFTER = re.compile(
    r"^\s*\+?\s*(?:/\s*(?:month|mo|mes)\b|per\s+month|a\s+month|monthly|p\.?\s?m\.?(?![a-z])|(?:al|por)\s+mes"
    r"|mensual(?:es)?|brut[oa]s?\s+mensual(?:es)?|gross\s*/?\s*(?:per\s+)?month)",
    re.IGNORECASE,
)
_HOUR_AFTER = re.compile(r"^\s*(?:/\s*(?:hour|hr|h)\b|per\s+hour|an\s+hour|hourly|(?:por|la)\s+hora)", re.IGNORECASE)
_MONTH_BEFORE = re.compile(r"\b(?:monthly|per\s+month|mensual|al\s+mes)\b[^.]{0,40}$", re.IGNORECASE)
_YEAR_BEFORE = re.compile(r"\b(?:annual|yearly|anual|al\s+a(?:ñ|n)o|per\s+year)\b[^.]{0,40}$", re.IGNORECASE)
_LABELS = (
    ("low", re.compile(
        r"\b(?:lowest|minimum|min|low|starts?\s+at|starting|from|desde|bajo|entry[- ]level|inicial|empiezan|25th|p25)\b",
        re.IGNORECASE)),
    ("high", re.compile(
        r"\b(?:highest|maximum|max|high|up\s+to|top|hasta|alto|experienced|experimentados|75th|p75|goes\s+up)\b",
        re.IGNORECASE)),
    ("mid", re.compile(
        r"\b(?:median|average|avg|typical|mean|media|mediana|promedio|p50|estimated|totals?)\b", re.IGNORECASE)),
)
_PAYMENTS = re.compile(r"\b(1[2-6])\s+pagas\b|\b(1[2-6])\s+(?:payments|instal?lments)\b", re.IGNORECASE)


def _flow():
    import jobhunter_interest_flow as flow

    return flow


# --------------------------------------------------------------------------- parsing


def currency_code(token: str | None) -> str | None:
    value = str(token or "").strip().lower()
    if not value:
        return None
    if value in {"€", "eur", "euro", "euros"}:
        return "EUR"
    if value in {"$", "usd", "us$"}:
        return "USD"
    if value in {"£", "gbp"}:
        return "GBP"
    return value.upper()


def parse_number(token: str) -> int | None:
    """'45.000' / '45,000' / '45.000,00' / '58.6K' / '1.04M' / '88000' -> int."""
    value = re.sub(r"\s", "", str(token or ""))
    if not value:
        return None
    multiplier = 1
    if value[-1] in "kK":
        multiplier, value = 1000, value[:-1]
    elif value[-1] in "mM":
        multiplier, value = 1_000_000, value[:-1]
    try:
        if multiplier > 1:
            return round(float(value.replace(",", ".")) * multiplier)
        grouped = re.fullmatch(r"(\d{1,3}(?:[.,]\d{3})+)(?:[.,]\d{1,2})?", value)
        if grouped:
            return int(re.sub(r"[.,]", "", grouped.group(1)))
        if re.fullmatch(r"\d+[.,]\d{1,2}", value):
            return round(float(value.replace(",", ".")))
        return int(value)
    except ValueError:
        return None


@dataclass
class Amount:
    low: int
    high: int
    currency: str | None
    period: str | None
    label: str | None
    start: int
    end: int

    @property
    def is_range(self) -> bool:
        return self.low != self.high


def _period_near(text: str, start: int, end: int) -> str | None:
    after = text[end:end + 30]
    if _HOUR_AFTER.search(after):
        return "hour"
    if _MONTH_AFTER.search(after):
        return "month"
    if _YEAR_AFTER.search(after):
        return "year"
    before = text[max(0, start - 60):start]
    if _MONTH_BEFORE.search(before):
        return "month"
    if _YEAR_BEFORE.search(before):
        return "year"
    return None


def _label_near(text: str, start: int) -> str | None:
    window = text[max(0, start - 90):start]
    window = re.split(r"[.?!](?:\s|$)|[|·;]", window)[-1]
    best, best_end = None, -1
    for label, pattern in _LABELS:
        for match in pattern.finditer(window):
            if match.end() > best_end:
                best, best_end = label, match.end()
    return best


def iter_amounts(text: str) -> list[Amount]:
    """Every currency amount or range in ``text``, raw (no period conversion)."""
    value = str(text or "")
    found: list[Amount] = []
    taken: list[tuple[int, int]] = []
    for match in _RANGE.finditer(value):
        currency = next((match.group(name) for name in ("c1", "c2", "c3", "c4") if match.group(name)), None)
        if not currency:
            continue
        a_text, b_text = match.group("a"), match.group("b")
        high = parse_number(b_text)
        low = parse_number(a_text)
        if low is None or high is None:
            continue
        if re.fullmatch(_SHORT, a_text) and b_text.strip()[-1:] in "kK":
            low = round(float(a_text.replace(",", ".")) * 1000)
        elif re.fullmatch(_SHORT, a_text):
            continue
        if low > high:
            low, high = high, low
        found.append(Amount(low, high, currency_code(currency), _period_near(value, match.start(), match.end()),
                            _label_near(value, match.start()), match.start(), match.end()))
        taken.append((match.start(), match.end()))
    for match in _SINGLE.finditer(value):
        if any(start <= match.start() < end for start, end in taken):
            continue
        number = parse_number(match.group("a") or match.group("b"))
        if number is None:
            continue
        currency = match.group("c1") or match.group("c2")
        found.append(Amount(number, number, currency_code(currency), _period_near(value, match.start(), match.end()),
                            _label_near(value, match.start()), match.start(), match.end()))
    return sorted(found, key=lambda amount: amount.start)


def payments_per_year(text: str) -> int | None:
    match = _PAYMENTS.search(str(text or ""))
    return int(match.group(1) or match.group(2)) if match else None


def parse_salary_text(text: str) -> dict[str, Any] | None:
    """The first salary in ``text`` as {low, high, currency, period, payments}."""
    amounts = [amount for amount in iter_amounts(text) if amount.period != "hour"]
    if not amounts:
        return None
    first = next((amount for amount in amounts if amount.is_range), amounts[0])
    period = first.period or next((amount.period for amount in amounts if amount.period), None)
    if period is None:
        lowered = str(text).lower()
        if re.search(r"\b(?:month|monthly|mes|mensual(?:es)?)\b", lowered):
            period = "month"
        elif re.search(r"\b(?:year|annual|annually|a(?:ñ|n)o|anual(?:es)?|annum)\b", lowered):
            period = "year"
    return {"low": first.low, "high": first.high, "currency": first.currency, "period": period,
            "payments": payments_per_year(text)}


def to_period(value: int, period: str | None, target: str, *, payments: int | None = None) -> int | None:
    """Convert a figure to the target period; None when implausible or hourly."""
    if period == "hour":
        return None
    if period is None:
        if target == "year":
            period = "year" if value >= 15000 else None
        else:
            period = "month" if value <= 100000 else "year"
    if period is None:
        return None
    if period != target:
        value = value * (payments or 12) if target == "year" else round(value / (payments or 12))
    low, high = PLAUSIBLE.get(target, (0, 10 ** 9))
    return value if low <= value <= high else None


def figures(text: str, *, currency: str | None, period: str) -> list[tuple[int, int, str | None]]:
    """(low, high, label) in ``currency``/``period``; monthly echoes of yearly figures are dropped."""
    amounts = [
        amount for amount in iter_amounts(text)
        if amount.period != "hour" and (currency is None or amount.currency == currency)
    ]
    if any(amount.period == "year" for amount in amounts) and period == "year":
        amounts = [amount for amount in amounts if amount.period != "month"]
    rows = []
    for amount in amounts:
        low = to_period(amount.low, amount.period, period)
        high = to_period(amount.high, amount.period, period)
        if low is not None and high is not None:
            rows.append((low, high, amount.label))
    return rows


def summarize_figures(rows: list[tuple[int, int, str | None]]) -> tuple[int | None, int | None] | None:
    """Collapse one site's figures to (low, high), requiring a range or center.

    A lone ``highest`` or ``lowest`` observation is not a salary range. Keep it
    in the source snippet, but do not promote it to an estimated distribution.
    """
    if not rows:
        return None
    ranges = [(low, high) for low, high, _ in rows if low != high]
    lows = [low for low, high, label in rows if low == high and label == "low"] + [low for low, _ in ranges]
    highs = [low for low, high, label in rows if low == high and label == "high"] + [high for _, high in ranges]
    mids = [low for low, high, label in rows if low == high and label not in {"low", "high"}]
    if not ranges and not mids and (not lows or not highs):
        return None
    low = median(lows) if lows else (min(mids) if mids else None)
    high = median(highs) if highs else (max(mids) if mids else None)
    if low is not None and high is not None and low > high:
        low, high = high, low
    return (round(low) if low is not None else None, round(high) if high is not None else None)


def aggregate(pairs: list[tuple[int | None, int | None]]) -> tuple[int, int, int] | None:
    """(low, high, agreeing sources) across sites; far outliers are ignored."""
    values = [value for pair in pairs for value in pair if value]
    if not values:
        return None
    center = median(values)

    def keep(value: int | None) -> bool:
        return bool(value) and center / 2.2 <= value <= center * 2.2

    lows = [low for low, _ in pairs if keep(low)]
    highs = [high for _, high in pairs if keep(high)]
    if not lows and not highs:
        return None
    low = median(lows) if lows else min(highs)
    high = median(highs) if highs else max(lows)
    if low > high:
        low, high = high, low
    agreeing = 0
    for pair_low, pair_high in pairs:
        kept = [value for value in (pair_low, pair_high) if keep(value)]
        if kept and low * 0.75 <= sum(kept) / len(kept) <= high * 1.25:
            agreeing += 1
    return round(low), round(high), agreeing


def site_name(url: str) -> str:
    host = urlparse(str(url or "")).netloc.lower().removeprefix("www.")
    return next((name for marker, name in MARKET_SITES if marker in host), "")


_EVIDENCE_LEVEL = re.compile(r"\b(staff|principal|lead|senior|sr\.?|s[ée]nior)\b", re.I)


def source_evidence(source: dict[str, Any]) -> dict[str, str]:
    """Return normalized evidence descriptors, inferring them for old sources."""
    provided = source.get("evidence")
    evidence = dict(provided) if isinstance(provided, dict) else {}
    text = " ".join(str(source.get(key) or "") for key in ("name", "snippet", "url"))
    plain = _plain(text)
    if not evidence.get("level"):
        match = _EVIDENCE_LEVEL.search(plain)
        evidence["level"] = ("staff" if match and match.group(1) in {"staff", "principal"} else
                              "lead" if match and match.group(1) == "lead" else
                              "senior" if match else "unknown")
    if not evidence.get("compensation_basis"):
        if re.search(r"total\s+comp|total\s+compensation|total\s+pay", plain):
            evidence["compensation_basis"] = "total_comp"
        elif re.search(r"base\s+(?:salary|pay)|gross\s+annual\s+base", plain):
            evidence["compensation_basis"] = "base"
        else:
            evidence["compensation_basis"] = "unspecified"
    if not evidence.get("statistic"):
        if re.search(r"\b(?:25th|75th|p25|p75|percentile|range)\b", plain):
            evidence["statistic"] = "percentile_range" if re.search(r"percentile|25th|75th|p25|p75", plain) else "range"
        elif re.search(r"median|mean|average|promedio|mediana", plain):
            evidence["statistic"] = "median"
        elif re.search(r"\bhighest\b|\blowest\b|\bmaximum\b|\bminimum\b", plain):
            evidence["statistic"] = "single_point"
        else:
            evidence["statistic"] = "unknown"
    if not evidence.get("scope"):
        kind = source.get("kind")
        level_marked = evidence["level"] != "unknown"
        if kind == "company" and (not level_marked or re.search(r"\ball\s+software\s+engineers?\b", plain)):
            evidence["scope"] = "company_wide"
        else:
            evidence["scope"] = "role_specific" if level_marked else "unknown"
    if not evidence.get("geography"):
        if re.search(r"\bspain|espa(?:n|ñ)a\b", plain):
            evidence["geography"] = "Spain"
        else:
            evidence["geography"] = "unknown"
    return evidence


def summarize_sources(sources: Iterable[dict[str, Any]], *, kind: str, currency: str | None,
                      period: str) -> dict[str, Any] | None:
    """Range for one evidence kind: {low, high, agreeing, names}."""
    selected = [source for source in sources if source.get("kind") == kind]
    if not selected:
        return None
    if currency is None:
        codes = [amount.currency for source in selected for amount in iter_amounts(source.get("snippet", ""))]
        currency = max(set(codes), key=codes.count) if codes else None
    groups: dict[str, list[dict[str, Any]]] = {}
    for source in selected:
        key = site_name(source.get("url", "")) or str(source.get("name") or source.get("url"))
        groups.setdefault(key, []).append(source)
    cohorts: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for group in groups.values():
        for source in group:
            evidence = source_evidence(source)
            # Company-wide and role-specific numbers, and base and total-comp
            # figures, must never be blended into a synthetic range.
            cohort = (evidence["scope"], evidence["level"], evidence["compensation_basis"],
                      evidence.get("proxy_for_level", ""))
            cohorts.setdefault(cohort, []).append(source)
    candidates = []
    for cohort, cohort_sources in cohorts.items():
        cohort_groups: dict[str, list[dict[str, Any]]] = {}
        for source in cohort_sources:
            key = site_name(source.get("url", "")) or str(source.get("name") or source.get("url"))
            cohort_groups.setdefault(key, []).append(source)
        pairs, names = [], []
        for group in cohort_groups.values():
            rows = [row for source in group for row in figures(source.get("snippet", ""), currency=currency, period=period)]
            pair = summarize_figures(rows)
            if pair and any(pair):
                pairs.append(pair)
                names.extend(str(source.get("name") or "") for source in group)
        result = aggregate(pairs)
        if result is not None:
            candidates.append((cohort, result, names))
    if not candidates:
        return None
    # Prefer specifically leveled role evidence. Broad company aggregates remain
    # queryable when they are the only evidence, but callers can label their scope.
    cohort, result, names = max(candidates, key=lambda item: (
        item[0][0] == "role_specific", item[0][1] != "unknown", bool(item[0][3]), item[1][2], len(item[2])))
    low, high, agreeing = result
    return {"low": low, "high": high, "agreeing": agreeing, "currency": currency, "period": period,
            "names": list(dict.fromkeys(name for name in names if name)), "scope": cohort[0],
            "level": cohort[1], "compensation_basis": cohort[2],
            "proxy_for_level": cohort[3] or None}


def ask_from_range(low: int | None, high: int | None, quantile: float = 0.75, round_to: int = 1000) -> int | None:
    """The ``quantile`` point of a range, rounded to ``round_to``."""
    if low is None and high is None:
        return None
    low = high if low is None else low
    high = low if high is None else high
    if low > high:
        low, high = high, low
    value = low + (high - low) * float(quantile)
    step = max(int(round_to or 1), 1)
    return int(round(value / step) * step)


# --------------------------------------------------------------------------- formatting


def format_amount(value: int, currency: str | None, *, compact: bool = True) -> str:
    symbol = CURRENCY_SYMBOLS.get(str(currency or ""))
    if compact and value >= 10000:
        number = f"{int(value / 1000 + 0.5)}k"
    else:
        number = f"{value:,}"
    if symbol:
        return f"{symbol}{number}"
    return f"{currency} {number}" if currency else number


def format_range(low: int | None, high: int | None, currency: str | None, period: str | None) -> str:
    if low is None and high is None:
        return ""
    low = high if low is None else low
    high = low if high is None else high
    suffix = f"/{period}" if period else ""
    first = format_amount(low, currency)
    if low == high or first == format_amount(high, currency):
        return f"{first}{suffix}"
    second = format_amount(high, currency)
    symbol = CURRENCY_SYMBOLS.get(str(currency or ""))
    if not symbol and currency and second.startswith(f"{currency} "):
        second = second[len(currency) + 1:]
    return f"{first}–{second}{suffix}"


# --------------------------------------------------------------------------- benchmarks

_BENCHMARK_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_COUNTRY_CODES = {"es": "es", "spain": "es", "españa": "es", "espana": "es"}


def load_benchmarks(path: Path | str | None = None) -> dict[str, Any]:
    """The benchmark file, or an empty table when it is missing or broken."""
    target = Path(path or BENCHMARKS_PATH)
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return {"entries": []}
    cached = _BENCHMARK_CACHE.get(str(target))
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(target.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        data = {"entries": []}
    _BENCHMARK_CACHE[str(target)] = (mtime, data)
    return data


def benchmark_entry(country: str | None, city: str | None, family: str | None, level: str | None,
                    benchmarks: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Exact city else '*'; exact level else the nearest one."""
    if not country or not family:
        return None
    table = benchmarks if benchmarks is not None else load_benchmarks()
    code = _COUNTRY_CODES.get(str(country).lower(), str(country).lower())
    entries = [
        entry for entry in table.get("entries", [])
        if isinstance(entry, dict)
        and _COUNTRY_CODES.get(str(entry.get("country", "")).lower(), str(entry.get("country", "")).lower()) == code
        and entry.get("role_family") == family
        and all(isinstance(entry.get(key), (int, float)) for key in ("p25", "p75"))
    ]
    if not entries:
        return None
    city_key = str(city or "").strip().lower().replace("valència", "valencia")
    exact = [entry for entry in entries if str(entry.get("city", "")).lower() == city_key]
    pool = exact or [entry for entry in entries if entry.get("city") == "*"] or entries
    order = {name: index for index, name in enumerate(roles.LEVELS)}
    wanted = order.get(str(level or "senior"), 1)
    return min(pool, key=lambda entry: abs(order.get(str(entry.get("level")), 1) - wanted))


# --------------------------------------------------------------------------- direct pages


def parse_levels_page(html_text: str) -> dict[str, Any] | None:
    """Percentiles from a Levels.fyi job-family page, converted to the page currency."""
    text = html_text or ""
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.DOTALL)
    if match:
        try:
            props = json.loads(match.group(1))["props"]["pageProps"]
            percentiles = props["jobFamilyLocationPercentiles"]
            rate = float(props.get("locationExchangeRate") or 1)
            values = {key: round(float(percentiles[key]) * rate) for key in ("p25", "p50", "p75", "p90")
                      if percentiles.get(key) is not None}
            if {"p25", "p75"} <= set(values):
                return {**values, "count": percentiles.get("count"),
                        "currency": props.get("locationCurrency") or "USD",
                        "role": " ".join(filter(None, [props.get("level"), props.get("jobFamily")])),
                        "location": str(props.get("location") or "").strip()}
        except (KeyError, TypeError, ValueError):
            pass
    plain = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())
    median_match = re.search(r"€\s?([\d.,]+[KkMm]?)\s+Median Total Comp", plain)
    p25 = re.search(r"€\s?([\d.,]+[KkMm]?)\s+25th%", plain)
    p75 = re.search(r"€\s?([\d.,]+[KkMm]?)\s+75th%", plain)
    if p25 and p75:
        return {"p25": parse_number(p25.group(1)), "p50": parse_number(median_match.group(1)) if median_match else None,
                "p75": parse_number(p75.group(1)), "count": None, "currency": "EUR", "role": "", "location": ""}
    return None


def _identity_text(html_text: str, tag: str) -> str:
    match = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}\s*>", str(html_text or ""), re.I | re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", match.group(1))).split()) if match else ""


def _normalized_identity(value: str) -> str:
    return " ".join(_plain(value).replace("-", " ").split())


def _role_identity_matches(actual: str, ctx, *, allow_senior_proxy: bool = False) -> bool:
    value = _normalized_identity(actual)
    actual_family = roles.role_family(actual)
    swe_families = {"senior_software_engineer", "lead_software_engineer", "backend_engineer"}
    if ctx.family in swe_families:
        family_matches = actual_family in swe_families
    else:
        family_matches = actual_family == ctx.family
    if not family_matches:
        return False
    expected_level = "senior" if allow_senior_proxy and ctx.level in {"lead", "staff"} else ctx.level
    actual_level = roles.role_level(actual)
    return actual_level == expected_level


def _talent_identity(html_text: str) -> tuple[str, str]:
    plain = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", str(html_text or ""))).split())
    question = re.search(r"¿?Cuánto gana un\s+(.+?)\s+en\s+([^?]+)\?", plain, re.I)
    if question:
        return question.group(1).strip(), question.group(2).strip()
    heading = _identity_text(html_text, "h1")
    match = re.search(r"^(.+?):\s*salario promedio en\s+([^,]+)", heading, re.I)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    average = re.search(r"La media de\s+(.+?)\s+en\s+(.+?)\s+es de\b", plain, re.I)
    if average:
        return average.group(1).strip(), average.group(2).strip()
    return "", ""


def _levels_identity(html_text: str, data: dict[str, Any]) -> tuple[str, str]:
    role, location = str(data.get("role") or "").strip(), str(data.get("location") or "").strip()
    if role and location:
        return role, location
    heading = _identity_text(html_text, "h1")
    match = re.search(r"^(.+?)\s+salary in\s+(.+?)\s*$", heading, re.I)
    if match:
        role = role or match.group(1).strip()
        location = location or match.group(2).strip()
    return role, location


def parse_talent_page(html_text: str) -> dict[str, Any] | None:
    plain = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", html_text or "")).split())
    found = {}
    for key, word in (("median", "Mediana"), ("low", "Bajo"), ("high", "Alto")):
        match = re.search(rf"\b{word}\s*€\s?([\d.,]+)", plain)
        if match:
            found[key] = parse_number(match.group(1))
    if "low" not in found or "high" not in found:
        return None
    count = re.search(r"Basado en ([\d.,]+) salarios", plain)
    role, location = _talent_identity(html_text)
    return {**found, "count": parse_number(count.group(1)) if count else None, "role": role, "location": location}


# --------------------------------------------------------------------------- research steps


def _source(kind: str, name: str, url: str, snippet: str, now_iso: str, *,
            evidence: dict[str, str] | None = None) -> dict[str, Any]:
    source = {"kind": kind, "name": name, "url": url, "snippet": " ".join(str(snippet or "").split())[:400],
              "retrieved_at": now_iso}
    source["evidence"] = {**source_evidence(source), **(evidence or {})}
    return source


def _fetch_text(fetch: Callable, url: str, notes: list[str], label: str) -> str | None:
    try:
        response = fetch(url)
    except Exception as exc:  # noqa: BLE001 - research is best-effort
        notes.append(f"{label} fetch failed: {exc.__class__.__name__}.")
        return None
    if response is None:
        notes.append(f"{label} fetch unavailable.")
        return None
    status = getattr(response, "status_code", 200)
    if status != 200:
        notes.append(f"{label} returned HTTP {status}.")
        return None
    return str(getattr(response, "text", "") or "")


def fetch_levels(ctx, fetch: Callable, now_iso: str, notes: list[str]) -> dict[str, str] | None:
    if ctx.market != "spain":
        return None
    path = roles.levels_fyi_path(ctx.family, ctx.level)
    if not path:
        return None
    url = LEVELS_BASE_URL + path
    text = _fetch_text(fetch, url, notes, "Levels.fyi")
    if text is None:
        return None
    data = parse_levels_page(text)
    if not data or data.get("currency") != "EUR":
        notes.append("Levels.fyi page had no EUR percentiles.")
        return None
    page_role, page_location = _levels_identity(text, data)
    if (not page_role or not page_location or not _role_identity_matches(page_role, ctx, allow_senior_proxy=True)
            or _normalized_identity(page_location) != "spain"):
        notes.append("Levels.fyi page identity did not match the requested Spain role and level.")
        return None
    upper = ctx.level in {"lead", "staff"} and data.get("p90") and ctx.family != "engineering_manager"
    low, high = (data.get("p50"), data["p90"]) if upper else (data["p25"], data["p75"])
    span = "median–90th" if upper else "25th–75th"
    count = f", {data['count']} submissions" if data.get("count") else ""
    role = data.get("role") or ctx.search_role
    snippet = (f"{role}, Spain ({span} percentile total comp{count}): "
               f"low {format_amount(low, 'EUR', compact=False)} · "
               + (f"median {format_amount(data['p50'], 'EUR', compact=False)} · " if data.get("p50") and not upper else "")
               + f"high {format_amount(high, 'EUR', compact=False)} per year")
    levels_evidence = {
        # The Spain page is the senior-engineer distribution even when we use
        # its upper quantiles as a proxy for lead/staff roles.
        "level": "senior" if ctx.level in {"lead", "staff"} else ctx.level,
        "role_family": ctx.family or "unknown", "geography": "Spain", "scope": "role_specific",
        "compensation_basis": "total_comp", "statistic": "percentile_range",
    }
    if ctx.level in {"lead", "staff"}:
        levels_evidence["proxy_for_level"] = ctx.level
    return _source("market", "Levels.fyi Spain", url, snippet, now_iso, evidence=levels_evidence)


def fetch_talent(ctx, fetch: Callable, now_iso: str, notes: list[str]) -> dict[str, str] | None:
    if ctx.market != "spain":
        return None
    place = (ctx.city or "España").lower()
    url = TALENT_URL.format(job=quote_plus(ctx.search_role.lower()), location=quote_plus(place))
    text = _fetch_text(fetch, url, notes, "talent.com")
    if text is None:
        return None
    data = parse_talent_page(text)
    if not data:
        notes.append("talent.com page had no salary figures.")
        return None
    page_role, page_city = data.get("role", ""), data.get("location", "")
    expected_city = ctx.city or "España"
    if (not page_role or not page_city or not _role_identity_matches(page_role, ctx)
            or _normalized_identity(page_city) != _normalized_identity(expected_city)):
        notes.append("talent.com page identity did not match the requested role and location.")
        return None
    count = f", {data['count']} salaries" if data.get("count") else ""
    snippet = (f"{page_role}, {page_city} (job-ad salaries{count}): "
               f"low {format_amount(data['low'], 'EUR', compact=False)} · "
               + (f"median {format_amount(data['median'], 'EUR', compact=False)} · " if data.get("median") else "")
               + f"high {format_amount(data['high'], 'EUR', compact=False)} per year")
    return _source("market", f"talent.com {page_city}", url, snippet, now_iso, evidence={
        "level": ctx.level or "unknown", "role_family": ctx.family or "unknown",
        "geography": page_city, "scope": "role_specific",
        "compensation_basis": "unspecified", "statistic": "range"})


def market_query(ctx) -> str:
    place = " ".join(part for part in (ctx.city, ctx.country_label) if part) or ctx.location
    currency = f" {ctx.currency}" if ctx.currency and ctx.market not in {None, "spain"} else ""
    return " ".join(f"{ctx.search_role} salary {place}{currency}".split())


def company_query(ctx) -> str | None:
    name = _flow().company_search_name(ctx.employer)
    if not name:
        return None
    return " ".join(f'"{name}" {ctx.search_role} salary {ctx.city or ctx.country_label or ""}'.split())


_LEVEL_WORDS = {
    "senior": r"\b(?:senior|sr|s[ée]nior)\b",
    "lead": r"\b(?:lead|principal|staff)\b",
    "staff": r"\b(?:staff|principal|lead)\b",
}


def _prefer_level(ctx, results: list[dict[str, str]]) -> list[dict[str, str]]:
    for level in ([ctx.level, "senior"] if ctx.level in {"lead", "staff"} else [ctx.level]):
        pattern = _LEVEL_WORDS.get(level)
        if not pattern:
            return results
        chosen = [result for result in results if re.search(pattern, result.get("title", ""), re.IGNORECASE)]
        if chosen:
            return chosen
    return results


# Salary sites file backend roles under the software-engineer title.
_ROLE_ALIASES = {"Backend Engineer": ("Software Engineer",)}


def _role_matches(ctx, result: dict[str, str]) -> bool:
    text = re.sub(r"[^a-z0-9]", "", f"{result.get('title', '')} {result.get('url', '')} {result.get('snippet', '')}".lower())
    for role in (ctx.base_role, *_ROLE_ALIASES.get(ctx.base_role, ())):
        token = re.sub(r"[^a-z0-9]", "", role.lower())
        if token and token in text:
            return True
    return False


def _evidence_text(result: dict[str, str]) -> str:
    title, snippet = str(result.get("title") or "").strip(), str(result.get("snippet") or "").strip()
    return f"{title}: {snippet}" if title and snippet else title or snippet


def _has_figures(ctx, text: str) -> bool:
    return bool(figures(text, currency=ctx.currency, period=ctx.period or "year"))


def _plain(value: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", str(value or "")) if not unicodedata.combining(ch)).lower()


def _display_name(site: str, ctx, text: str) -> str:
    plain = _plain(text)
    if ctx.city and re.search(rf"\b{re.escape(_plain(ctx.city))}\b", plain):
        return f"{site} {ctx.city}"
    if ctx.country_label and re.search(rf"\b{re.escape(_plain(ctx.country_label))}\b", plain):
        return f"{site} {ctx.country_label}"
    return site


def market_sources_from_results(ctx, results: list[dict[str, str]], now_iso: str) -> list[dict[str, str]]:
    flow = _flow()
    candidates = []
    for result in results:
        site = site_name(result.get("url", ""))
        if not site or not _role_matches(ctx, result):
            continue
        if ctx.location and not flow._salary_source_matches_job_location(result, ctx.location):
            continue
        if not _has_figures(ctx, _evidence_text(result)):
            continue
        candidates.append(result)
    sources, seen = [], set()
    candidates = _prefer_level(ctx, candidates)
    if ctx.level == "senior":
        # A query result for the family alone is not evidence for a senior role.
        candidates = [result for result in candidates
                      if source_evidence({"kind": "market", "snippet": _evidence_text(result)})["level"] == "senior"]
    for result in candidates:
        if result["url"] in seen:
            continue
        seen.add(result["url"])
        text = _evidence_text(result)
        inferred = source_evidence({"kind": "market", "snippet": text})
        inferred.update(role_family=ctx.family or "unknown", geography=ctx.city or ctx.country_label or "unknown")
        sources.append(_source("market", _display_name(site_name(result["url"]), ctx, text), result["url"], text,
                               now_iso, evidence=inferred))
    return sources[:6]


def company_sources_from_results(ctx, results: list[dict[str, str]], now_iso: str) -> list[dict[str, str]]:
    flow = _flow()
    candidates = []
    for result in results:
        if not flow._result_matches_company(ctx.employer, result) or not _role_matches(ctx, result):
            continue
        if ctx.location and not flow._salary_source_matches_job_location(result, ctx.location):
            continue
        if not _has_figures(ctx, _evidence_text(result)):
            continue
        candidates.append(result)
    sources, seen = [], set()
    for result in _prefer_level(ctx, candidates):
        if result["url"] in seen:
            continue
        seen.add(result["url"])
        site = site_name(result["url"]) or urlparse(result["url"]).netloc.removeprefix("www.")
        text = _evidence_text(result)
        inferred = source_evidence({"kind": "company", "snippet": text})
        inferred.update(role_family=ctx.family or "unknown", geography=ctx.city or ctx.country_label or "unknown")
        sources.append(_source("company", f"{site}: {flow.company_search_name(ctx.employer)}", result["url"],
                               text, now_iso, evidence=inferred))
    return sources[:4]


def published_salary(ctx, now_iso: str) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """The employer-published pay, validated against the posting or a trusted board."""
    text = _flow().validated_job_salary(ctx.job)
    if not text:
        return None, None
    parsed = parse_salary_text(text)
    payments = payments_per_year(ctx.description) if parsed else None
    source_name = ctx.source if ctx.source in TRUSTED_SALARY_SOURCES else "Job description"
    source = _source("published", source_name, str(ctx.job.get("url") or ""), text, now_iso,
                     evidence={"level": ctx.level or "unknown", "role_family": ctx.family or "unknown",
                               "geography": ctx.city or ctx.country_label or "unknown", "scope": "role_specific",
                               "compensation_basis": "base" if re.search(r"base salary|gross annual base", text, re.I) else "unspecified",
                               "statistic": "range" if parsed and parsed["low"] != parsed["high"] else "unknown"})
    if not parsed:
        return {"text": text}, source
    currency = parsed["currency"]
    target = ctx.period if ctx.currency and currency == ctx.currency and ctx.period else (
        parsed["period"] or ctx.period or "year")
    low = to_period(parsed["low"], parsed["period"], target, payments=parsed["payments"] or payments)
    high = to_period(parsed["high"], parsed["period"], target, payments=parsed["payments"] or payments)
    if low is None or high is None:
        return {"text": text}, source
    return {"text": text, "low": low, "high": high, "currency": currency, "period": target}, source


def estimate_source(ctx, now_iso: str, benchmarks: dict[str, Any] | None = None) -> dict[str, str] | None:
    """Benchmark fallback; for target markets without benchmarks, the configured band."""
    entry = benchmark_entry("es" if ctx.market == "spain" else ctx.market, ctx.city, ctx.family, ctx.level, benchmarks)
    if entry:
        names = [str(item.get("name")) for item in entry.get("sources", []) if isinstance(item, dict) and item.get("name")]
        url = next((str(item.get("url")) for item in entry.get("sources", []) if isinstance(item, dict) and item.get("url")), "")
        where = "Spain-wide" if entry.get("city") == "*" else str(entry.get("city")).title()
        family = str(entry.get("role_family", "")).replace("_", " ")
        snippet = (f"low €{int(entry['p25']):,} · "
                   + (f"median €{int(entry['p50']):,} · " if isinstance(entry.get("p50"), (int, float)) else "")
                   + f"high €{int(entry['p75']):,} per year ({entry.get('level')} {family}, {where}; gross annual base)")
        label = ", ".join(dict.fromkeys(names[:3])) or "benchmark table"
        return _source("benchmark", f"Benchmark ({label})", url, snippet, now_iso)
    flow = _flow()
    target = flow.salary_target(ctx.job)
    if target is None:
        return None
    band = flow.format_salary_band(flow.legacy_salary_band_name(ctx.job), ctx.job)
    match = re.match(r"(\w+) (\d+)k–(\d+)k/(\w+)", band)
    if not match:
        return None
    currency, low, high, period = match.group(1), int(match.group(2)) * 1000, int(match.group(3)) * 1000, match.group(4)
    snippet = f"low {currency} {low:,} · high {currency} {high:,} per {period} (band derived from the configured target)"
    return _source("benchmark", "Configured target band", "", snippet, now_iso)


def decide(ctx, published: dict[str, Any] | None, sources: list[dict[str, str]], *,
           quantile: float = 0.75, round_to: int = 1000) -> dict[str, Any]:
    """Choose the salary basis by precedence and compute the ask."""
    salary: dict[str, Any] = {
        "low": None, "high": None, "currency": ctx.currency, "period": ctx.period, "basis": "none",
        "ask_amount": None, "published_text": (published or {}).get("text") or "", "confidence": "low",
    }
    period = ctx.period or "year"
    configured_ask = False
    if published and published.get("low") is not None:
        salary.update(low=published["low"], high=published["high"], currency=published["currency"],
                      period=published["period"], basis="published", confidence="high")
    else:
        for kind in ("company", "market", "benchmark"):
            summary = summarize_sources(sources, kind=kind, currency=ctx.currency, period=period)
            if not summary:
                continue
            wanted_level = getattr(ctx, "level", None)
            if kind == "company" and (summary.get("scope") != "role_specific" or
                                       wanted_level is not None and summary.get("level") not in {wanted_level, "unknown"}):
                continue
            if kind == "market" and wanted_level is not None:
                summary_sources = [source for source in sources if source.get("kind") == kind]
                acceptable_levels = {wanted_level, "unknown"}
                if summary.get("proxy_for_level") == wanted_level:
                    acceptable_levels.add("senior")
                if summary.get("level") not in acceptable_levels:
                    continue
            basis = "estimate" if kind == "benchmark" else kind
            if kind == "benchmark":
                confidence = "low"
            else:
                confidence = "high" if summary["agreeing"] >= 2 else "medium"
            salary.update(low=summary["low"], high=summary["high"], currency=summary["currency"],
                          period=period, basis=basis, confidence=confidence,
                          compensation_basis=summary.get("compensation_basis", "unspecified"),
                          ask_basis=basis if kind != "benchmark" else "estimate",
                          proxy_for_level=summary.get("proxy_for_level"))
            break
    target = _flow().salary_target(ctx.job)
    if target and salary["basis"] != "none" and target["currency"] == salary["currency"]:
        if target["period"] == salary["period"]:
            salary["ask_amount"] = int(target["amount"])
            configured_ask = True
        else:
            salary["ask_amount"] = ask_from_range(salary["low"], salary["high"], quantile, round_to)
    elif target and salary["basis"] == "none":
        salary.update(currency=target["currency"], period=target["period"], ask_amount=int(target["amount"]))
    else:
        salary["ask_amount"] = ask_from_range(salary["low"], salary["high"], quantile, round_to)
        if salary["ask_amount"] is not None:
            salary["ask_basis"] = salary.get("basis")
    if salary["basis"] == "published":
        salary["compensation_basis"] = source_evidence({"kind": "published", "snippet": (published or {}).get("text", "")}).get("compensation_basis")
        salary["ask_basis"] = "published"
    elif salary["basis"] == "none" and target:
        salary["ask_basis"] = "configured"
        salary.setdefault("compensation_basis", "unspecified")
    elif salary["basis"] == "none":
        salary["ask_basis"] = "none"
        salary.setdefault("compensation_basis", "unspecified")
    elif configured_ask:
        salary["ask_basis"] = "configured"
    return salary
