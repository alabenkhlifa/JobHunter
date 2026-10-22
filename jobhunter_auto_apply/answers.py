"""Form answers and per-field classification for autopilot (policy) runs.

`build_answers` turns the owner profile, the job research and the jobs mailbox
into the only values an unattended run may type. `classify_field` maps one
rendered input to an answer key or a hard stop (EN + ES labels), and
`plan_fields` combines both into a fill plan for the agent.

Hard-stop codes are the shared literals of the autopilot contract.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


CAPTCHA = "captcha"
PHONE_OR_IDENTITY = "phone_or_identity"
NO_AI_ANSWER = "no_ai_answer"
SALARY_PERIOD_AMBIGUOUS = "salary_period_ambiguous"
LINKEDIN_EASY_APPLY = "linkedin_easy_apply"
COVER_LETTER_REQUIRED = "cover_letter_required"


def unanswerable(label: str) -> str:
    return f"unanswerable:{_clean_label(label)}"


ANSWER_KEYS = (
    "first_name", "last_name", "full_name", "email", "phone", "linkedin", "website",
    "location", "city", "country", "salary_expectation", "salary_currency", "notice_period",
    "work_authorization", "requires_visa_sponsorship", "citizenship",
    "allow_contact_for_other_vacancies", "languages", "english_level", "years_experience",
    "gender", "pronouns", "preferred_language", "data_sharing_scope",
    "willing_to_relocate", "current_company", "current_title", "referral_source",
    "consent", "decline_to_answer",
)


# --------------------------------------------------------------------------- text helpers

def _norm(value: Any) -> str:
    """Casefold, strip accents and collapse whitespace (``Teléfono`` -> ``telefono``)."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("’", "'").replace("¿", " ").replace("¡", " ")
    return " ".join(text.casefold().split())


def _split_identifier(value: Any) -> str:
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(value or ""))
    return re.sub(r"[_\-\[\]\.]+", " ", text)


def _clean_label(label: Any) -> str:
    text = " ".join(str(label or "field").split())
    text = text.rstrip(" *:").strip() or "field"
    return text[:80]


def _label(field: dict[str, Any]) -> str:
    return _clean_label(field.get("question") or field.get("label") or field.get("name")
                        or field.get("id") or field.get("tag") or "field")


def _field_text(field: dict[str, Any]) -> str:
    parts = [field.get("question"), field.get("label"), field.get("placeholder"),
             field.get("autocomplete"), _split_identifier(field.get("name")),
             _split_identifier(field.get("id"))]
    return _norm(" ".join(str(part) for part in parts if part))


def _options(field: dict[str, Any]) -> list[str]:
    return [str(option) for option in (field.get("options") or []) if str(option).strip()]


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I)


# --------------------------------------------------------------------------- patterns (normalized text)

_CAPTCHA_RX = _rx(r"captcha|i'?m not a robot|no soy un robot|human verification|verify (that )?you are (a )?human"
                  r"|verifica(r)? que (eres|es) (un )?humano")
_PAGE_CAPTCHA_RX = _rx(r"(solve|complete|enter|fill in|resuelve|completa|introduce)\b.{0,40}captcha|i'?m not a robot"
                       r"|no soy un robot|verify (that )?you are (a )?human|verifica(r)? que (eres|es) (un )?humano")
_VERIFY_RX = _rx(r"(sms|text message)\s+(verification|code|codigo)|(phone|mobile|telefono|movil)\s+verification"
                 r"|verification code sent to your (phone|mobile)|codigo (enviado )?(por sms|al (movil|telefono))"
                 r"|verify your (phone|mobile|identity)|identity verification|verificacion de (identidad|telefono)"
                 r"|verifica tu (identidad|telefono|movil)|phone verification")
_SENSITIVE_RX = _rx(r"criminal|convict|felon|offen[cs]e|background (check|screening|verification)"
                    r"|antecedentes|delito|medical|medic[oa]|health condition|enfermedad|drug (test|screen)"
                    r"|\bsalud\b")
_EEO_RX = _rx(r"\b(gender|sex|sexual orientation|pronouns?|ethnic\w*|race|racial|veteran|disabilit\w*|disabled"
              r"|lgbt\w*|transgender|genero|genre|sexo|etnia|origen etnico|raza|discapacidad|minusvalia"
              r"|orientacion sexual|diversity|diversidad)\b")
_GENDER_RX = _rx(r"\b(gender|genero|genre)\b")
_PRONOUNS_RX = _rx(r"\bpronouns?\b")
_OTHER_EEO_RX = _rx(r"\b(sex|sexual|pronouns?|ethnic\w*|race|racial|veteran|disabilit\w*|disabled"
                     r"|lgbt\w*|transgender|sexo|etnia|raza|discapacidad|minusvalia|diversity|diversidad)\b")
_DATA_SHARING_RX = _rx(r"\b(shar(?:e|ing)\b.{0,40}\b(data|profile|information)"
                       r"|(data|profile|information)\b.{0,40}\b(sharing|shared)"
                       r"|compartir\b.{0,40}\b(datos|perfil)|partag\w*\b.{0,40}\b(donnees|profil))\b")
_DECLINE_RX = _rx(r"prefer not|decline|do not wish|don'?t wish|rather not|not (to )?(say|disclose|answer)"
                  r"|prefiero no|no deseo|no quiero (responder|decir|contestar)|sin especificar|no especificar")
_NO_AI_RX = _rx(r"without (the )?(use of |using |help of )?(ai|a\.i\.|artificial intelligence|chat ?gpt|llms?|generative ai)\b"
                r"|(do not|don'?t|please don'?t|must not) use (any )?(ai|chat ?gpt|generative ai|llms?|artificial intelligence)\b"
                r"|not (be )?(ai|chat ?gpt)[- ](generated|written)|no (ai|chat ?gpt)[- ]generated"
                r"|sin (usar |utilizar |el uso de |ayuda de )?(la )?(ia|inteligencia artificial|chat ?gpt)\b"
                r"|no (uses|utilices|utilizar|usar|debes usar) (la )?(ia|inteligencia artificial|chat ?gpt)\b")
_COVER_RX = _rx(r"cover ?letter|motivation(al)? letter|letter of motivation|carta de (presentacion|motivacion)")
_RESUME_RX = _rx(r"resume|curriculum|\bcv\b|hoja de vida")
_CURRENCY_RX = _rx(r"\b(currency|moneda|divisa)\b")
_CURRENT_SALARY_RX = _rx(r"current (salary|compensation|pay|package|ctc|base)|salario actual|sueldo actual"
                         r"|retribucion actual|remuneracion actual")
_SALARY_RX = _rx(r"salar|compensation|pay expectation|expected pay|desired pay|expected package|remuneracion"
                 r"|retribucion|sueldo|pretension\w* (salarial|economica)|expectativa\w* (salarial|economica)"
                 r"|banda salarial|rango salarial|\bctc\b|bruto anual|brutos anuales")
_NON_EUR_RX = _rx(r"\b(usd|gbp|chf|aed|sar|cad|aud|dollars?|dolares|pounds? sterling|libras)\b|[$£]")
_MONTHLY_RX = _rx(r"per month|monthly|a month|/ ?month|\bmonth\b|mensual|al mes|por mes|/ ?mes\b|\bmes\b")
_ANNUAL_RX = _rx(r"per (year|annum)|annual|yearly|a year|/ ?year|\bp\.?a\.?\b|anual|al ano|por ano|/ ?ano\b|\byear\b")
_INSTALMENTS_RX = _rx(r"\b(1[2-6])\s*(pagas|payments|instal?ments|salaries|monthly payments)\b")
_THOUSANDS_RX = _rx(r"in thousands|en miles|miles de (euros|€)|\(\s*k\s*(€|eur)?\s*\)|\bk ?(€|eur)\b|\b(€|eur) ?k\b")
_CONSENT_RX = _rx(r"privacy|data protection|gdpr|rgpd|lopd|terms|condiciones|politica de privacidad"
                  r"|proteccion de datos|consent|consiento|acepto|i agree|i accept|i have read|he leido"
                  r"|certify|true and correct|accurate|veraz|declaro|confirm that")
_MARKETING_RX = _rx(r"newsletter|marketing|promotional|comunicaciones comerciales|publicidad|job alerts?")
_COMPANYISH_RX = _rx(r"company|empresa|employer|referr|referen|recomend|school|universi|manager|contact person")
_FREE_TEXT_TYPES = {"text", "textarea", ""}
_WITHOUT_SPONSORSHIP_RX = _rx(r"without (the need (for|of) |requiring |needing )?(a |any )?(visa |employer )?sponsorship"
                              r"|sin (necesidad de |necesitar |requerir )?(un |ningun )?(visado|patrocinio)")
_YES_NO_KEYS = {"allow_contact_for_other_vacancies", "requires_visa_sponsorship", "work_authorization",
                "willing_to_relocate"}

# Ordered: earlier patterns win. Keys refer to `build_answers` output.
_KEY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("preferred_language", _rx(
        r"preferred (communication |contact |correspondence )?language|language preference"
        r"|language (for|of) (communication|correspondence)|idioma preferido|lengua preferida"
        r"|langue preferee|langue de (communication|correspondance)")),
    ("allow_contact_for_other_vacancies", _rx(
        r"(other|future|similar) (vacanc|position|opportunit|role|opening|job)|talent (pool|community|network)"
        r"|otras (ofertas|vacantes|posiciones|oportunidades)|futur[ao]s? (procesos|ofertas|vacantes|oportunidades)"
        r"|base de datos de candidatos|keep (my|your) (data|details|cv|resume)|conservar (mis|tus) datos")),
    ("requires_visa_sponsorship", _rx(r"sponsor|\bvisa\b|visado|patrocinio")),
    ("work_authorization", _rx(
        r"authori[sz]ed to work|right to work|work (authori[sz]ation|permit)|eligible to work"
        r"|legally (able|allowed|entitled|authori[sz]ed) to work|permiso de (trabajo|residencia)"
        r"|autorizacion (de|para) trabajar|autorizad[oa] para trabajar|derecho a trabajar|permiso para trabajar")),
    ("citizenship", _rx(r"citizenship|nationality|nacionalidad|ciudadania")),
    ("linkedin", _rx(r"linked ?in")),
    ("website", _rx(r"website|portfolio|personal (site|url|page)|github|web ?site|sitio web|pagina web|\bblog\b")),
    ("email", _rx(r"e-?mail|correo")),
    ("phone", _rx(r"phone|mobile|telephone|\bcell\b|telefono|movil|celular|\btel\b")),
    ("notice_period", _rx(
        r"notice period|preaviso|earliest start|start date|when can you start|available to start"
        r"|availability (to start|date)|fecha de incorporacion|incorporacion inmediata"
        r"|disponibilidad (de|para) (incorporaci|empezar|comenzar)|cuando (podrias|puedes) (empezar|incorporarte)")),
    ("years_experience", _rx(r"years of (professional |relevant |work |total )?experience|anos de experiencia"
                             r"|how many years|cuantos anos")),
    ("english_level", _rx(r"(english|ingles)\b.*\b(level|proficiency|nivel)|(level|nivel) (of|de) (english|ingles)"
                          r"|english proficiency")),
    ("languages", _rx(r"\b(languages?|idiomas?|lenguas?)\b")),
    ("willing_to_relocate", _rx(r"relocat|reubica|traslad|mudarte|mudarse")),
    ("current_title", _rx(r"current (job )?(title|role|position)|puesto actual|cargo actual")),
    ("current_company", _rx(r"current (company|employer)|empresa actual|current organi[sz]ation")),
    ("referral_source", _rx(r"how did you (hear|find|learn)|where did you (hear|find|see)"
                            r"|como (nos )?(conociste|encontraste|te enteraste|has conocido)|donde (viste|encontraste)")),
    ("full_name", _rx(r"full name|your name|legal name|candidate name|nombre (y apellidos?|completo)")),
    ("last_name", _rx(r"last ?name|surname|family name|apellido")),
    ("first_name", _rx(r"first ?name|given name|forename|\bnombre\b")),
    ("city", _rx(r"\b(city|ciudad|localidad|municipio)\b")),
    ("country", _rx(r"\b(country|pais)\b")),
    ("location", _rx(r"location|ubicacion|where are you (based|located)|currently (based|located)"
                     r"|place of residence|lugar de residencia|residencia|\bprovincia\b")),
)

_KEY_KINDS = {
    "email": "email", "phone": "tel", "linkedin": "url", "website": "url",
    "salary_expectation": "number", "years_experience": "number",
    "work_authorization": "yes_no", "requires_visa_sponsorship": "yes_no",
    "allow_contact_for_other_vacancies": "yes_no", "willing_to_relocate": "yes_no",
    "consent": "checkbox", "decline_to_answer": "choice", "salary_currency": "choice",
    "gender": "choice", "pronouns": "choice", "preferred_language": "choice", "data_sharing_scope": "choice",
}


# --------------------------------------------------------------------------- answers

_PLACEHOLDER_RX = _rx(r"^(ask[_ ]?user|private|yes/no|n/?a|tbd|todo|unknown|example\b.*|.*\bif confirmed\b.*"
                      r"|.*\bif the user chooses\b.*|city, country|country or citizenship value.*|mr/ms.*)$")


def _real(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        text = " ".join(value.split())
        if not text or _PLACEHOLDER_RX.fullmatch(_norm(text)):
            return None
        return text
    return value


def _yes_no(value: Any) -> str | None:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    text = _norm(_real(value) or "")
    if text in {"yes", "y", "true", "si", "1"}:
        return "Yes"
    if text in {"no", "n", "false", "0"}:
        return "No"
    return None


_MONTHS = {name: index for index, names in enumerate((
    ("january", "jan", "enero"), ("february", "feb", "febrero"), ("march", "mar", "marzo"),
    ("april", "apr", "abril"), ("may", "mayo"), ("june", "jun", "junio"), ("july", "jul", "julio"),
    ("august", "aug", "agosto"), ("september", "sep", "sept", "septiembre"),
    ("october", "oct", "octubre"), ("november", "nov", "noviembre"), ("december", "dec", "diciembre"),
), start=1) for name in names}


def _start_month(dates: str) -> tuple[int, int] | None:
    first = re.split(r"\s+[-–—]\s+|\s+to\s+|\s+a\s+", str(dates or ""), maxsplit=1)[0]
    norm = _norm(first)
    year = re.search(r"\b(19|20)\d{2}\b", norm)
    if not year:
        return None
    month = 1
    numeric = re.search(r"\b(\d{1,2})[/.](19|20)\d{2}\b", norm)
    if numeric and 1 <= int(numeric.group(1)) <= 12:
        month = int(numeric.group(1))
    else:
        for word in re.findall(r"[a-z]+", norm):
            if word in _MONTHS:
                month = _MONTHS[word]
                break
    return int(year.group(0)), month


def _years_experience(profile: dict[str, Any], today: dt.date) -> int | None:
    starts = [_start_month(item.get("dates", "")) for item in profile.get("experience") or []
              if isinstance(item, dict)]
    starts = [item for item in starts if item]
    if not starts:
        return None
    year, month = min(starts)
    months = (today.year - year) * 12 + today.month - month
    return max(0, months // 12) if months >= 0 else None


def _current_role(profile: dict[str, Any]) -> dict[str, Any] | None:
    for item in profile.get("experience") or []:
        if not isinstance(item, dict):
            continue
        if re.search(r"present|actualidad|current|today|hoy", _norm(item.get("dates"))) and \
                not re.search(r"alongside|side|freelance", _norm(item.get("title"))):
            return item
    return None


def _languages_text(profile: dict[str, Any]) -> str | None:
    value = (profile.get("additional") or {}).get("languages") or profile.get("languages")
    if isinstance(value, list):
        parts = [item.get("name") if isinstance(item, dict) else str(item) for item in value]
        value = ", ".join(part for part in parts if part)
    if not isinstance(value, str) or not value.strip():
        return None
    return ", ".join(part.strip() for part in re.split(r"\s*[|;]\s*", value) if part.strip())


def build_answers(profile: dict[str, Any] | None, *, research: dict[str, Any] | None,
                  jobs_mailbox: str | None, cached: dict[str, Any] | None = None,
                  today: dt.date | None = None) -> dict[str, dict[str, Any]]:
    """Return ``{key: {"value", "source", "kind"}}``; unknown answers have ``value None``, ``source "missing"``.

    ``cached`` holds previously built answers (same shape or bare values); they
    only fill keys the profile/research cannot answer now.
    """

    profile = profile if isinstance(profile, dict) else {}
    defaults = profile.get("application_defaults") if isinstance(profile.get("application_defaults"), dict) else {}
    today = today or dt.date.today()
    answers: dict[str, dict[str, Any]] = {}

    def put(key: str, value: Any, source: str, kind: str | None = None) -> None:
        value = _real(value)
        kind = kind or _KEY_KINDS.get(key, "text")
        if value is None:
            answers[key] = {"value": None, "source": "missing", "kind": kind}
        else:
            answers[key] = {"value": value, "source": source, "kind": kind}

    name = _real(profile.get("name"))
    parts = name.split() if isinstance(name, str) else []
    put("full_name", name, "profile")
    put("first_name", parts[0] if parts else None, "profile")
    put("last_name", " ".join(parts[1:]) if len(parts) > 1 else None, "profile")
    put("email", jobs_mailbox, "jobs_mailbox")
    put("phone", profile.get("phone"), "profile")
    linkedin = _real(profile.get("linkedin"))
    if isinstance(linkedin, str) and not linkedin.startswith("http"):
        linkedin = "https://" + linkedin.lstrip("/")
    put("linkedin", linkedin, "profile")
    put("website", profile.get("website"), "profile")

    location = _real(defaults.get("current_work_location"))
    location_source = "application_defaults"
    if location is None:
        raw = _real(profile.get("location"))
        location = re.split(r"\s+[·|•]\s+", raw)[0].strip() if isinstance(raw, str) else None
        location_source = "profile"
    pieces = [piece.strip() for piece in str(location or "").split(",") if piece.strip()]
    put("location", location, location_source)
    put("city", pieces[0] if pieces else None, location_source)
    put("country", pieces[-1] if len(pieces) > 1 else None, location_source)

    salary = (research or {}).get("salary") if isinstance(research, dict) else None
    ask = salary.get("ask_amount") if isinstance(salary, dict) else None
    currency = str((salary or {}).get("currency") or "EUR").upper() if isinstance(salary, dict) else "EUR"
    period = str((salary or {}).get("period") or "year").casefold() if isinstance(salary, dict) else "year"
    try:
        ask_value = int(round(float(ask))) if ask is not None and currency == "EUR" and period == "year" else None
    except (TypeError, ValueError):
        ask_value = None
    put("salary_expectation", str(ask_value) if ask_value and ask_value > 0 else None,
        f"research:{(salary or {}).get('basis') or 'unknown'}")
    put("salary_currency", "EUR", "policy")

    put("notice_period", defaults.get("notice_period"), "application_defaults")
    sponsorship = _yes_no(defaults.get("requires_visa_sponsorship"))
    put("requires_visa_sponsorship", sponsorship, "application_defaults")
    authorization = _yes_no(defaults.get("work_authorization"))
    if authorization is None and sponsorship is not None:
        authorization = "No" if sponsorship == "Yes" else "Yes"
    put("work_authorization", authorization, "application_defaults")
    put("citizenship", defaults.get("citizenship"), "application_defaults")
    put("allow_contact_for_other_vacancies", _yes_no(defaults.get("allow_contact_for_other_vacancies")),
        "application_defaults")
    # Explicit private preferences only: never derive these from a name, CV,
    # spoken-language proficiency, or consent to contact about future jobs.
    for key in ("gender", "pronouns", "preferred_language", "data_sharing_scope"):
        put(key, defaults.get(key), "application_defaults")

    languages = _languages_text(profile)
    put("languages", languages, "profile")
    english = re.search(r"english\s*\(([^)]+)\)", languages or "", re.I)
    put("english_level", english.group(1).strip() if english else None, "profile")
    years = _years_experience(profile, today)
    put("years_experience", str(years) if years is not None else None, "profile")

    relocate = _yes_no(defaults.get("willing_to_relocate"))
    if relocate is None and re.search(r"relocat|reubica", _norm(profile.get("location"))):
        relocate = "Yes"
    put("willing_to_relocate", relocate, "profile")
    role = _current_role(profile)
    put("current_title", (role or {}).get("title"), "profile")
    put("current_company", (role or {}).get("company"), "profile")
    put("referral_source", "Job board", "default", "choice")
    put("consent", "Yes", "policy")
    put("decline_to_answer", "Prefer not to say", "policy")

    for key, value in (cached or {}).items():
        if key in answers and answers[key]["value"] is not None:
            continue
        entry = value if isinstance(value, dict) and "value" in value else {"value": value}
        if _real(entry.get("value")) is None:
            continue
        answers[key] = {"value": entry["value"], "source": entry.get("source") or "cached",
                        "kind": entry.get("kind") or _KEY_KINDS.get(key, "text")}
    return answers


def missing_answers(answers: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(key for key, entry in answers.items() if entry.get("value") is None)


# --------------------------------------------------------------------------- classification

def _result(kind: str, required: bool, answer_key: str | None = None, hard_stop: str | None = None,
            transform: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {"kind": kind, "answer_key": answer_key, "hard_stop": hard_stop, "required": required}
    if transform:
        result["transform"] = transform
    return result


def _is_free_text(field: dict[str, Any]) -> bool:
    tag = str(field.get("tag") or "").casefold()
    kind = str(field.get("type") or "").casefold()
    return tag == "textarea" or (tag in {"input", ""} and kind in _FREE_TEXT_TYPES)


def _salary_transform(text: str, page_text: str) -> tuple[dict[str, Any] | None, str | None]:
    transform: dict[str, Any] = {}
    if _THOUSANDS_RX.search(text):
        transform["thousands"] = True
    monthly = bool(_MONTHLY_RX.search(text))
    annual = bool(_ANNUAL_RX.search(text))
    if monthly and not annual:
        instalments = _INSTALMENTS_RX.search(text) or _INSTALMENTS_RX.search(_norm(page_text))
        if not instalments:
            return None, SALARY_PERIOD_AMBIGUOUS
        transform["divide"] = int(instalments.group(1))
    return transform or None, None


def classify_field(field: dict[str, Any], page_text: str = "") -> dict[str, Any]:
    """Classify one inspected input: ``{"kind","answer_key","hard_stop","required"}``.

    ``kind`` is semantic (``personal``, ``salary``, ``eeo``, ``consent``,
    ``file``, ``free_text``…). ``hard_stop`` is a contract code or ``None``.
    """

    tag = str(field.get("tag") or "").casefold()
    ftype = str(field.get("type") or "").casefold()
    required = bool(field.get("required"))
    label = _label(field)
    text = _field_text(field)
    options = _options(field)
    option_text = _norm(" ".join(options))
    page = _norm(page_text)

    if ftype in {"hidden", "submit", "button", "reset", "image", "search"}:
        return _result("ignored", required)
    if _CAPTCHA_RX.search(text):
        return _result("captcha", required, hard_stop=CAPTCHA)
    if _VERIFY_RX.search(text):
        return _result("verification", required, hard_stop=PHONE_OR_IDENTITY)
    if ftype == "password":
        return _result("password", required, "password")
    if _SENSITIVE_RX.search(text):
        return _result("sensitive", required, hard_stop=unanswerable(label))
    if _EEO_RX.search(text):
        if not required:
            return _result("eeo", required)
        if any(_DECLINE_RX.search(_norm(option)) for option in options):
            return _result("eeo", required, "decline_to_answer")
        return _result("eeo", required, hard_stop=unanswerable(label))
    if ftype == "file":
        if _COVER_RX.search(text):
            return _result("file", required, "cover_letter", COVER_LETTER_REQUIRED if required else None)
        if _RESUME_RX.search(text) or not text:
            return _result("file", required, "resume")
        return _result("file", required, hard_stop=unanswerable(label) if required else None)
    free_text = _is_free_text(field)
    if free_text and _NO_AI_RX.search(f"{text} {page}") and (tag == "textarea" or _COVER_RX.search(text)):
        return _result("free_text", required, hard_stop=NO_AI_ANSWER if required else None)
    if _COVER_RX.search(text):
        return _result("cover_letter", required, "cover_letter", COVER_LETTER_REQUIRED if required else None)
    if _DATA_SHARING_RX.search(text):
        # A yes/no checkbox does not describe the scope of data sharing.
        # Do not turn an opening-only preference into broader consent.
        if ftype == "checkbox" and len(options) < 2:
            return _result("privacy", required, hard_stop=unanswerable(label) if required else None)
        if not options and tag != "select" and ftype != "radio":
            return _result("privacy", required, hard_stop=unanswerable(label) if required else None)
        return _result("privacy", required, "data_sharing_scope")
    if _CURRENCY_RX.search(text) and (not _SALARY_RX.search(text)
                                      or (options and not any(_numbers(option) for option in options))):
        if tag == "select" or options:
            if any(re.search(r"\beur\b|€|euro", _norm(option)) for option in options):
                return _result("currency", required, "salary_currency")
            return _result("currency", required, hard_stop=unanswerable(label))
        return _result("currency", required, "salary_currency")
    if _SALARY_RX.search(text):
        if _CURRENT_SALARY_RX.search(text):
            return _result("salary", required, "current_salary")
        if _NON_EUR_RX.search(f"{text} {option_text}") and not re.search(r"\beur\b|€|euro", f"{text} {option_text}"):
            return _result("salary", required, hard_stop=unanswerable(label))
        transform, stop = _salary_transform(f"{text} {option_text}", page_text)
        if stop:
            return _result("salary", required, hard_stop=stop)
        return _result("salary", required, "salary_expectation", transform=transform)
    if ftype == "email":
        return _result("contact", required, "email")
    if ftype == "tel":
        return _result("contact", required, "phone")
    if _WITHOUT_SPONSORSHIP_RX.search(text):
        return _result("question", required, "work_authorization")
    checkbox = ftype == "checkbox"
    for key, pattern in _KEY_PATTERNS:
        if not pattern.search(text):
            continue
        if checkbox and key not in _YES_NO_KEYS:
            continue
        if key in {"first_name", "last_name", "full_name", "city", "country", "location"} \
                and _COMPANYISH_RX.search(text):
            continue
        if key in {"languages", "preferred_language"} and re.search(r"programming|programacion|framework", text):
            continue
        if key == "country" and re.search(r"country code|codigo (de )?pais|prefijo", text):
            continue
        kind = "contact" if key in {"email", "phone", "linkedin", "website"} else \
            "personal" if key in {"first_name", "last_name", "full_name", "city", "country", "location",
                                  "citizenship"} else "question"
        return _result(kind, required, key)
    if _norm(field.get("label")) in {"name", "nombre", "your name", "tu nombre"}:
        return _result("personal", required, "full_name")
    if checkbox:
        if _CONSENT_RX.search(text):
            return _result("consent", required, "consent")
        if _MARKETING_RX.search(text):
            return _result("consent", required, "consent" if required else None)
    if free_text and required and _NO_AI_RX.search(page):
        return _result("free_text", required, hard_stop=NO_AI_ANSWER)
    kind = "free_text" if free_text else "unknown"
    return _result(kind, required, hard_stop=unanswerable(label) if required else None)


# --------------------------------------------------------------------------- planning

def _css_string(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _selector(field: dict[str, Any], *, value: str | None = None) -> str | None:
    tag = str(field.get("tag") or "input").casefold() or "input"
    if field.get("id"):
        return f"[id={_css_string(field['id'])}]"
    if field.get("name"):
        selector = f"{tag}[name={_css_string(field['name'])}]"
        if value is not None:
            selector += f"[value={_css_string(value)}]"
        return selector
    return None


_YES = ("yes", "si", "y", "true", "i am", "i do", "i have", "lo estoy", "tengo")
_NO = ("no", "n", "false", "i am not", "i do not", "i don't", "no tengo")
_LEVEL_ALIASES = {
    "c2": ("c2", "native", "nativo", "bilingual", "bilingue", "mother tongue", "proficient"),
    "c1": ("c1", "advanced", "avanzado", "fluent", "fluido", "professional", "profesional", "proficient"),
    "b2": ("b2", "upper intermediate", "intermedio alto", "intermediate", "intermedio"),
}


def _numbers(text: str) -> list[float]:
    values = []
    for raw, suffix in re.findall(r"(\d[\d.,]*)\s*(k\b)?", _norm(text)):
        raw = raw.rstrip(".,")
        if re.fullmatch(r"\d{1,3}([.,]\d{3})+", raw):
            number = float(re.sub(r"[.,]", "", raw))
        else:
            try:
                number = float(raw.replace(",", "."))
            except ValueError:
                continue
        values.append(number * (1000 if suffix else 1))
    return values


def _choose_numeric(options: list[str], value: float) -> str | None:
    for option in options:
        norm = _norm(option)
        numbers = _numbers(option)
        if not numbers:
            continue
        if len(numbers) >= 2 and numbers[0] <= value <= numbers[1]:
            return option
        if len(numbers) == 1:
            if re.search(r"\+|more than|over|at least|mas de|or more|o mas", norm) and value >= numbers[0]:
                return option
            if re.search(r"less than|under|below|menos de|<", norm) and value < numbers[0]:
                return option
            if numbers[0] == value:
                return option
    return None


_CURRENT_OPENING_RX = _rx(
    r"only (?:for )?(?:the |this |my )?(?:current )?(?:opening|position|role|job|vacancy|application)"
    r"(?: i am applying (?:to|for))?"
    r"|(?:this|the current) (?:opening|position|role|job|vacancy|application) only"
    r"|solo para (?:esta|la actual) (?:vacante|oferta|solicitud|posicion)"
    r"|seulement pour (?:ce poste|cette offre|cette candidature)"
)


def _english_locale(value: Any) -> str | None:
    """Recognize English US and generic English, without choosing another locale."""
    text = re.sub(r"[^a-z0-9]+", " ", _norm(value)).strip()
    if text in {"english", "en", "ingles", "anglais"}:
        return ""
    if text in {"en us", "american english"} or re.fullmatch(
        r"(?:english|ingles|anglais) (?:us|usa|united states|eeuu|estados unidos|etats unis)(?: en us)?", text
    ):
        return "us"
    return None


def _choose_option(options: list[str], value: Any, answer_key: str | None) -> str | None:
    if not options:
        return None
    norm_options = [(_norm(option), option) for option in options]
    usable = [(norm, raw) for norm, raw in norm_options
              if norm and not re.fullmatch(r"(select|choose|selecciona|elige|seleccione)\b.*|-+|--.*", norm)]
    target = _norm(value)
    if answer_key == "decline_to_answer":
        return next((raw for norm, raw in usable if _DECLINE_RX.search(norm)), None)
    if answer_key == "salary_currency":
        return next((raw for norm, raw in usable if re.search(r"\beur\b|€|euro", norm)), None)
    for norm, raw in usable:
        if norm == target:
            return raw
    if answer_key == "gender":
        aliases = (
            {"male", "man", "masculino", "hombre", "masculin", "homme"},
            {"female", "woman", "femenino", "mujer", "feminin", "femme"},
        )
        matches = next((group for group in aliases if target in group), {target})
        # Never use substring matching here: "male" is contained in "female".
        return next((raw for norm, raw in usable if norm in matches), None)
    if answer_key == "pronouns":
        identity = lambda text: re.sub(r"\s*/\s*", "/", text)
        return next((raw for norm, raw in usable if identity(norm) == identity(target)), None)
    if answer_key == "preferred_language":
        locale = _english_locale(value)
        if locale is None:
            return None
        for candidate in (["us", ""] if locale == "us" else [""]):
            match = next((raw for _, raw in usable if _english_locale(raw) == candidate), None)
            if match is not None:
                return match
        return None
    if answer_key == "data_sharing_scope":
        if target != "current_opening":
            return None
        return next((raw for norm, raw in usable if _CURRENT_OPENING_RX.fullmatch(norm)), None)
    if target in {"yes", "no"}:
        words = _YES if target == "yes" else _NO
        for norm, raw in usable:
            head = re.split(r"[,.;:(]", norm, maxsplit=1)[0].strip()
            if head in words or any(norm.startswith(word + " ") for word in words if len(word) > 1):
                return raw
        return None
    if answer_key == "english_level":
        aliases = _LEVEL_ALIASES.get(target, (target,))
        for alias in aliases:
            for norm, raw in usable:
                if re.search(rf"\b{re.escape(alias)}\b", norm):
                    return raw
        return None
    if answer_key == "referral_source":
        for alias in ("job board", "job site", "jobs board", "portal de empleo", "web de empleo", "internet",
                      "other", "otro", "otros", "otra"):
            for norm, raw in usable:
                if re.search(rf"\b{re.escape(alias)}\b", norm):
                    return raw
        return None
    if answer_key in {"salary_expectation", "years_experience"}:
        try:
            return _choose_numeric([raw for _, raw in usable], float(str(value)))
        except ValueError:
            return None
    pieces = [_norm(piece) for piece in re.split(r"[,/]", str(value)) if piece.strip()]
    for piece in pieces:
        for norm, raw in usable:
            if norm == piece:
                return raw
    for norm, raw in usable:
        if len(norm) > 2 and (norm in target or target in norm):
            return raw
    return None


def _apply_transform(value: Any, transform: dict[str, Any] | None) -> Any:
    if not transform:
        return value
    try:
        number = float(str(value))
    except ValueError:
        return value
    if transform.get("divide"):
        number = number / int(transform["divide"])
    if transform.get("thousands"):
        number = number / 1000
        return str(int(round(number)))
    return str(int(round(number / 10.0) * 10)) if transform.get("divide") else str(int(round(number)))


def _inspection_parts(inspection: Any) -> tuple[str, list[dict[str, Any]], list[str], str]:
    if isinstance(inspection, dict):
        url = inspection.get("url") or ""
        inputs = inspection.get("inputs") or []
        blockers = inspection.get("blockers") or []
        text = inspection.get("page_text") or inspection.get("text") or inspection.get("text_excerpt") or ""
    else:
        url = getattr(inspection, "url", "") or ""
        inputs = getattr(inspection, "inputs", []) or []
        blockers = getattr(inspection, "blockers", []) or []
        text = getattr(inspection, "page_text", None) or getattr(inspection, "text_excerpt", "") or ""
    return url, list(inputs), list(blockers), text


def page_hard_stops(inspection: Any) -> list[dict[str, str]]:
    """Page-level stops: LinkedIn Easy Apply, a visible/explicit CAPTCHA, phone or identity checks.

    The "protected by reCAPTCHA" footer of invisible reCAPTCHA is not a stop.
    """

    url, _, blockers, text = _inspection_parts(inspection)
    stops: list[dict[str, str]] = []
    host = (urlsplit(url).hostname or "").casefold()
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        stops.append({"label": "LinkedIn Easy Apply", "reason": LINKEDIN_EASY_APPLY})
    lowered = [str(item).casefold() for item in blockers]
    if "human verification" in lowered or _PAGE_CAPTCHA_RX.search(_norm(text)):
        stops.append({"label": "CAPTCHA", "reason": CAPTCHA})
    if any(item in {"phone verification", "verify your phone", "identity verification"} for item in lowered) \
            or _VERIFY_RX.search(_norm(text)):
        stops.append({"label": "Phone or identity verification", "reason": PHONE_OR_IDENTITY})
    return stops


def _groups(inputs: list[dict[str, Any]]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Merge radio buttons (and ``name[]`` checkboxes) of one name into a single question."""

    groups: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    index: dict[tuple[str, str], int] = {}
    for item in inputs:
        if not isinstance(item, dict) or item.get("disabled"):
            continue
        ftype = str(item.get("type") or "").casefold()
        name = str(item.get("name") or "")
        grouped = name and (ftype == "radio" or (ftype == "checkbox" and name.endswith("[]")))
        if not grouped:
            groups.append((item, [item]))
            continue
        key = (ftype, name)
        if key in index:
            groups[index[key]][1].append(item)
            continue
        index[key] = len(groups)
        groups.append((item, [item]))
    merged = []
    for first, members in groups:
        if len(members) == 1 and str(first.get("type") or "").casefold() != "radio":
            merged.append((first, members))
            continue
        question = next((m.get("question") for m in members if m.get("question")), None)
        merged.append(({
            "tag": "input", "type": first.get("type"), "name": first.get("name"), "id": "",
            "label": question or first.get("name") or first.get("label"), "question": question,
            "required": any(m.get("required") for m in members),
            "value_present": any(m.get("value_present") for m in members),
            "options": [str(m.get("label") or m.get("value") or "") for m in members],
        }, members))
    return merged


def plan_fields(inspection: Any, answers: dict[str, dict[str, Any]] | None) -> dict[str, list[dict[str, Any]]]:
    """Return ``{"fill", "leave_blank", "hard_stops", "uploads"}`` for the rendered form.

    ``fill`` items: ``{selector, label, value, answer_key, kind}``; ``value`` is
    the option text to choose for selects/radios and ``True`` to tick a checkbox.
    """

    answers = answers or {}
    _, inputs, _, page_text = _inspection_parts(inspection)
    plan: dict[str, list[dict[str, Any]]] = {"fill": [], "leave_blank": [], "hard_stops": [], "uploads": []}
    seen_stops: set[tuple[str, str]] = set()

    def stop(label: str, reason: str) -> None:
        if (label, reason) not in seen_stops:
            seen_stops.add((label, reason))
            plan["hard_stops"].append({"label": label, "reason": reason})

    for item in page_hard_stops(inspection):
        stop(item["label"], item["reason"])

    for field, members in _groups(inputs):
        if str(field.get("type") or "").casefold() in {"hidden", "submit", "button", "reset", "image", "search"}:
            continue
        result = classify_field(field, page_text)
        if (result["kind"] == "eeo" and _GENDER_RX.search(_field_text(field))
                and not _OTHER_EEO_RX.search(_field_text(field))
                and _real((answers.get("gender") or {}).get("value")) is not None):
            # A confirmed preference overrides the default EEO decline/skip
            # only for the same attribute, never for other diversity questions.
            result = _result("eeo", result["required"], "gender")
        if (result["kind"] == "eeo" and _PRONOUNS_RX.search(_field_text(field))
                and not _GENDER_RX.search(_field_text(field))
                and not _OTHER_EEO_RX.search(_PRONOUNS_RX.sub("", _field_text(field)))
                and _real((answers.get("pronouns") or {}).get("value")) is not None):
            result = _result("eeo", result["required"], "pronouns")
        label = _label(field)
        required = result["required"]
        key = result["answer_key"]
        selector = _selector(field)
        blank = lambda reason: plan["leave_blank"].append({"selector": selector, "label": label, "reason": reason})

        if result["hard_stop"] == COVER_LETTER_REQUIRED and _real((answers.get("cover_letter") or {}).get("value")):
            result = {**result, "hard_stop": None}
        if result["hard_stop"]:
            stop(label, result["hard_stop"])
            continue
        if result["kind"] == "password":
            blank("credential")
            continue
        if result["kind"] == "file" or (key == "cover_letter" and str(field.get("type")).casefold() == "file"):
            plan["uploads"].append({"selector": selector, "label": label, "answer_key": key or "unknown",
                                    "required": required})
            continue
        if key is None:
            if field.get("value_present"):
                blank("already_filled")
            else:
                blank("eeo_optional" if result["kind"] == "eeo" else "not_needed")
            continue
        entry = answers.get(key) or {}
        value = entry.get("value")
        if value is None:
            if required and not field.get("value_present"):
                stop(label, unanswerable(label))
            else:
                blank("missing_answer")
            continue
        value = _apply_transform(value, result.get("transform"))
        ftype = str(field.get("type") or "").casefold()
        tag = str(field.get("tag") or "").casefold()
        if ftype == "radio" or (ftype == "checkbox" and len(members) > 1):
            choice = _choose_option(field.get("options") or [], value, key)
            if choice is None:
                if required:
                    stop(label, unanswerable(label))
                else:
                    blank("no_matching_option")
                continue
            member = next(m for m in members if str(m.get("label") or m.get("value") or "") == choice)
            plan["fill"].append({"selector": _selector(member, value=member.get("value")), "label": label,
                                 "value": choice, "answer_key": key, "kind": result["kind"]})
            continue
        if ftype == "checkbox":
            if key == "consent" or _yes_no(value) == "Yes":
                plan["fill"].append({"selector": selector, "label": label, "value": True,
                                     "answer_key": key, "kind": result["kind"]})
            elif required:
                stop(label, unanswerable(label))
            else:
                blank("answer_no")
            continue
        if tag == "select" or (field.get("options") and tag != "textarea"):
            choice = _choose_option(_options(field), value, key)
            if choice is None:
                if required:
                    stop(label, unanswerable(label))
                else:
                    blank("no_matching_option")
                continue
            value = choice
        if selector is None:
            if required:
                stop(label, unanswerable(label))
            else:
                blank("no_selector")
            continue
        plan["fill"].append({"selector": selector, "label": label, "value": value,
                             "answer_key": key, "kind": result["kind"]})
    return plan


# --------------------------------------------------------------------------- loading for the engine CLI

def load_answers(db_path: str, job_id: str, *, profile_path: str | Path = "data/master-profile.json",
                 jobs_mailbox: str | None = None, answers_path: str | Path | None = None) -> dict[str, Any]:
    """Answers for a policy-run inspect: an explicit JSON file, else profile + cached research."""

    if answers_path is not None:
        payload = json.loads(Path(answers_path).read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("answers"), dict):
            payload = payload["answers"]
        if not isinstance(payload, dict):
            raise ValueError("answers file must contain a JSON object")
        return payload
    try:
        profile = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        profile = {}
    research = None
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            row = conn.execute("SELECT payload_json FROM job_research WHERE job_id=?", (job_id,)).fetchone()
        research = json.loads(row[0]) if row and row[0] else None
    except (sqlite3.Error, ValueError, OSError):
        research = None
    if jobs_mailbox is None:
        try:
            from jobhunter_integrations.gmail_auth import expected_account
            jobs_mailbox = expected_account()
        except Exception:
            jobs_mailbox = None
    return build_answers(profile, research=research, jobs_mailbox=jobs_mailbox)
