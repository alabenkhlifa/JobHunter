import datetime as dt
import json
import sqlite3

import pytest

from jobhunter_auto_apply import answers as module
from jobhunter_auto_apply.answers import build_answers, classify_field, plan_fields
from jobhunter_auto_apply.engine import PageInspection


PROFILE = {
    "name": "Ala Ben Khlifa",
    "email": "personal@example.com",
    "phone": "+216 00 000 000",
    "linkedin": "linkedin.com/in/example",
    "website": "https://example.dev/",
    "location": "Tunis, Tunisia · Open to relocation",
    "additional": {"languages": "Arabic (Native) | English (C1) | French (C1)"},
    "experience": [
        {"title": "Lead Software Engineer", "company": "Acme", "dates": "October 2024 – Present"},
        {"title": "CTO (alongside full-time role)", "company": "Side", "dates": "October 2025 – Present"},
        {"title": "Software Engineer", "company": "First", "dates": "August 2019 - September 2020"},
    ],
    "application_defaults": {
        "notice_period": "1 month",
        "citizenship": "Tunisian",
        "requires_visa_sponsorship": "yes",
        "allow_contact_for_other_vacancies": "yes",
    },
}
RESEARCH = {"salary": {"low": 52000, "high": 66000, "currency": "EUR", "period": "year",
                       "basis": "market", "ask_amount": 62000}}
MAILBOX = "jobs@example.com"


def _answers(profile=PROFILE, research=RESEARCH, **kwargs):
    return build_answers(profile, research=research, jobs_mailbox=MAILBOX, today=dt.date(2026, 10, 15), **kwargs)


def test_build_answers_values_and_sources():
    answers = _answers()
    assert answers["first_name"] == {"value": "Ala", "source": "profile", "kind": "text"}
    assert answers["last_name"]["value"] == "Ben Khlifa"
    assert answers["full_name"]["value"] == "Ala Ben Khlifa"
    assert answers["email"] == {"value": MAILBOX, "source": "jobs_mailbox", "kind": "email"}
    assert answers["linkedin"]["value"] == "https://linkedin.com/in/example"
    assert answers["location"]["value"] == "Tunis, Tunisia"
    assert answers["city"]["value"] == "Tunis" and answers["country"]["value"] == "Tunisia"
    assert answers["salary_expectation"] == {"value": "62000", "source": "research:market", "kind": "number"}
    assert answers["notice_period"]["value"] == "1 month"
    assert answers["requires_visa_sponsorship"]["value"] == "Yes"
    assert answers["work_authorization"]["value"] == "No"
    assert answers["citizenship"]["value"] == "Tunisian"
    assert answers["allow_contact_for_other_vacancies"]["value"] == "Yes"
    assert answers["languages"]["value"] == "Arabic (Native), English (C1), French (C1)"
    assert answers["english_level"]["value"] == "C1"
    assert answers["years_experience"]["value"] == "7"
    assert answers["willing_to_relocate"]["value"] == "Yes"
    assert answers["current_title"]["value"] == "Lead Software Engineer"
    assert answers["current_company"]["value"] == "Acme"


def test_missing_application_defaults_are_marked_missing():
    profile = {key: value for key, value in PROFILE.items() if key != "application_defaults"}
    answers = _answers(profile=profile, research=None)
    for key in ("notice_period", "requires_visa_sponsorship", "work_authorization", "citizenship",
                "allow_contact_for_other_vacancies", "salary_expectation"):
        assert answers[key]["value"] is None and answers[key]["source"] == "missing", key
    assert "notice_period" in module.missing_answers(answers)


@pytest.mark.parametrize("value", ["ask_user", "Example: 1 month", "yes/no", "private", ""])
def test_placeholder_defaults_are_missing(value):
    profile = {**PROFILE, "application_defaults": {"notice_period": value, "requires_visa_sponsorship": value}}
    answers = _answers(profile=profile)
    assert answers["notice_period"]["value"] is None
    assert answers["requires_visa_sponsorship"]["value"] is None


def test_no_jobs_mailbox_never_falls_back_to_personal_email():
    answers = build_answers(PROFILE, research=RESEARCH, jobs_mailbox=None)
    assert answers["email"]["value"] is None


@pytest.mark.parametrize("salary", [{"ask_amount": 60000, "currency": "USD"}, {"ask_amount": None},
                                    {"ask_amount": 5000, "period": "month"}])
def test_salary_only_from_eur_yearly_research(salary):
    assert _answers(research={"salary": salary})["salary_expectation"]["value"] is None


def test_cached_answers_fill_gaps_only():
    profile = {key: value for key, value in PROFILE.items() if key != "application_defaults"}
    answers = _answers(profile=profile, cached={"notice_period": {"value": "2 weeks", "source": "telegram"},
                                                 "phone": "+34 600 000 000"})
    assert answers["notice_period"] == {"value": "2 weeks", "source": "telegram", "kind": "text"}
    assert answers["phone"]["value"] == PROFILE["phone"]


def _field(label, *, tag="input", type="text", required=True, options=None, name="", id="f", **extra):
    return {"tag": tag, "type": type, "label": label, "required": required, "options": options or [],
            "name": name, "id": id, **extra}


@pytest.mark.parametrize("field,key", [
    (_field("First name"), "first_name"),
    (_field("Nombre"), "first_name"),
    (_field("Last name"), "last_name"),
    (_field("Apellidos"), "last_name"),
    (_field("Nombre y apellidos"), "full_name"),
    (_field("Full Name"), "full_name"),
    (_field("Name"), "full_name"),
    (_field("Email", type="email"), "email"),
    (_field("Correo electrónico"), "email"),
    (_field("Phone", type="tel"), "phone"),
    (_field("Teléfono móvil"), "phone"),
    (_field("LinkedIn Profile"), "linkedin"),
    (_field("Website / Portfolio"), "website"),
    (_field("Ciudad"), "city"),
    (_field("Location (City)"), "city"),
    (_field("Where are you based?"), "location"),
    (_field("Notice period"), "notice_period"),
    (_field("¿Cuál es tu periodo de preaviso?"), "notice_period"),
    (_field("Are you legally authorized to work in Spain?"), "work_authorization"),
    (_field("¿Tienes permiso de trabajo en la UE?"), "work_authorization"),
    (_field("Can you work in Spain without visa sponsorship?"), "work_authorization"),
    (_field("Will you now or in the future require visa sponsorship?"), "requires_visa_sponsorship"),
    (_field("¿Necesitas patrocinio de visado?"), "requires_visa_sponsorship"),
    (_field("Nationality"), "citizenship"),
    (_field("Nacionalidad"), "citizenship"),
    (_field("Languages"), "languages"),
    (_field("Idiomas"), "languages"),
    (_field("English level", tag="select", options=["Select", "B2", "C1", "C2"]), "english_level"),
    (_field("Years of experience"), "years_experience"),
    (_field("Años de experiencia"), "years_experience"),
    (_field("Expected salary (EUR gross per year)"), "salary_expectation"),
    (_field("Pretensiones salariales (bruto anual)"), "salary_expectation"),
    (_field("Salary expectations"), "salary_expectation"),
    (_field("Can we contact you about other vacancies?", type="checkbox"), "allow_contact_for_other_vacancies"),
    (_field("Acepto que conservéis mis datos para futuros procesos", type="checkbox"),
     "allow_contact_for_other_vacancies"),
    (_field("I have read and accept the privacy policy", type="checkbox"), "consent"),
    (_field("He leído y acepto la política de privacidad", type="checkbox"), "consent"),
    (_field("I certify that the information is true and correct", type="checkbox"), "consent"),
    (_field("Resume/CV", type="file"), "resume"),
    (_field("Curriculum", type="file"), "resume"),
    (_field("How did you hear about us?", tag="select", options=["LinkedIn", "Job board", "Other"]),
     "referral_source"),
    (_field("Are you willing to relocate?"), "willing_to_relocate"),
    (_field("Gender", tag="select", options=["Male", "Female", "Prefer not to say"]), "decline_to_answer"),
])
def test_classify_field_answer_keys(field, key):
    result = classify_field(field)
    assert result["hard_stop"] is None, result
    assert result["answer_key"] == key


@pytest.mark.parametrize("field,page_text,reason", [
    (_field("Please complete the captcha"), "", "captcha"),
    (_field("SMS verification code"), "", "phone_or_identity"),
    (_field("Verify your identity"), "", "phone_or_identity"),
    (_field("Why do you want to join us?", tag="textarea", type="textarea"),
     "Please answer without using AI tools.", "no_ai_answer"),
    (_field("¿Por qué quieres trabajar aquí?", tag="textarea", type="textarea"),
     "Responde sin IA, por favor.", "no_ai_answer"),
    (_field("Have you ever been convicted of a criminal offence?"), "", "unanswerable:Have you ever been convicted of a criminal offence?"),
    (_field("¿Tienes antecedentes penales?", required=False), "", "unanswerable:¿Tienes antecedentes penales?"),
    (_field("Do you consent to a background check?", required=False), "",
     "unanswerable:Do you consent to a background check?"),
    (_field("Do you have any medical condition?"), "", "unanswerable:Do you have any medical condition?"),
    (_field("Gender", tag="select", options=["Male", "Female"]), "", "unanswerable:Gender"),
    (_field("Salario deseado (mensual)"), "", "salary_period_ambiguous"),
    (_field("Expected monthly salary"), "", "salary_period_ambiguous"),
    (_field("Salary currency", tag="select", options=["USD", "GBP"]), "", "unanswerable:Salary currency"),
    (_field("Expected salary (USD)"), "", "unanswerable:Expected salary (USD)"),
    (_field("Cover letter", type="file"), "", "cover_letter_required"),
    (_field("Carta de presentación", tag="textarea", type="textarea"), "", "cover_letter_required"),
    (_field("Tell us about a project you are proud of", tag="textarea", type="textarea"), "",
     "unanswerable:Tell us about a project you are proud of"),
    (_field("Favourite colour", tag="select", options=["Red", "Blue"]), "", "unanswerable:Favourite colour"),
])
def test_classify_field_hard_stops(field, page_text, reason):
    assert classify_field(field, page_text)["hard_stop"] == reason


@pytest.mark.parametrize("field", [
    _field("Gender", tag="select", options=["Male", "Female"], required=False),
    _field("Tell us about a project you are proud of", tag="textarea", type="textarea", required=False),
    _field("Cover letter", type="file", required=False),
    _field("Subscribe to our newsletter", type="checkbox", required=False),
])
def test_optional_unknowns_are_left_blank_not_stopped(field):
    result = classify_field(field, "Please answer without AI")
    assert result["hard_stop"] is None and result["required"] is False


def test_monthly_salary_with_instalments_is_converted():
    result = classify_field(_field("Salario mensual bruto (14 pagas)"))
    assert result["answer_key"] == "salary_expectation" and result["transform"] == {"divide": 14}
    thousands = classify_field(_field("Expected annual salary (k€)"))
    assert thousands["transform"] == {"thousands": True}


def test_currency_select_with_eur_is_answered():
    result = classify_field(_field("Currency", tag="select", options=["USD", "EUR", "GBP"]))
    assert result["answer_key"] == "salary_currency"


def _inspection(inputs, text="", url="https://jobs.example.test/apply", blockers=()):
    page = PageInspection(url, "Apply", text[:2000], inputs=inputs, blockers=list(blockers))
    page.page_text = text
    return page


def test_plan_fields_fills_selects_and_radios_and_stops_on_unanswerables():
    inputs = [
        _field("First name", id="first_name"),
        _field("Email", type="email", id="email"),
        _field("Expected salary (EUR gross per year)", id="salary"),
        _field("Monthly salary (14 pagas)", id="monthly"),
        {"tag": "input", "type": "radio", "name": "sponsor", "id": "sponsor_yes", "label": "Yes", "value": "1",
         "question": "Will you require visa sponsorship?", "required": True},
        {"tag": "input", "type": "radio", "name": "sponsor", "id": "", "label": "No", "value": "0",
         "question": "Will you require visa sponsorship?", "required": True},
        _field("¿Cuál es tu nivel de inglés?", tag="select", id="eng", options=["Selecciona", "Básico", "Avanzado (C1)"]),
        _field("Privacy policy", type="checkbox", id="gdpr"),
        _field("Resume", type="file", id="resume"),
        _field("Password", type="password", id="pw"),
        _field("Gender", tag="select", id="gender", options=["Male", "Female"], required=False),
        _field("Notice period", id="notice"),
        _field("Expected start date", id="start", value_present=True, required=True),
        _field("Hidden", type="hidden", id="h"),
    ]
    profile = {**PROFILE, "application_defaults": {"requires_visa_sponsorship": "no"}}
    plan = plan_fields(_inspection(inputs), _answers(profile=profile))
    filled = {item["answer_key"]: item for item in plan["fill"]}
    assert filled["first_name"]["value"] == "Ala" and filled["first_name"]["selector"] == '[id="first_name"]'
    assert filled["email"]["value"] == MAILBOX
    assert [item["value"] for item in plan["fill"] if item["selector"] == '[id="salary"]'] == ["62000"]
    assert [item["value"] for item in plan["fill"] if item["selector"] == '[id="monthly"]'] == ["4430"]
    assert filled["requires_visa_sponsorship"] == {
        "selector": 'input[name="sponsor"][value="0"]', "label": "Will you require visa sponsorship?",
        "value": "No", "answer_key": "requires_visa_sponsorship", "kind": "question"}
    assert filled["english_level"]["value"] == "Avanzado (C1)"
    assert filled["consent"]["value"] is True
    assert plan["uploads"] == [{"selector": '[id="resume"]', "label": "Resume", "answer_key": "resume",
                                "required": True}]
    reasons = {item["reason"] for item in plan["leave_blank"]}
    assert {"credential", "eeo_optional"} <= reasons
    assert plan["hard_stops"] == [{"label": "Notice period", "reason": "unanswerable:Notice period"}]


def test_plan_fields_page_level_stops():
    plan = plan_fields(_inspection([], "Please complete the CAPTCHA to continue",
                                   url="https://www.linkedin.com/jobs/view/1"), {})
    assert [item["reason"] for item in plan["hard_stops"]] == ["linkedin_easy_apply", "captcha"]
    footer = plan_fields(_inspection([], "This site is protected by reCAPTCHA and the Google Privacy Policy"), {})
    assert footer["hard_stops"] == []
    visible = plan_fields(_inspection([], "", blockers=["human verification"]), {})
    assert visible["hard_stops"] == [{"label": "CAPTCHA", "reason": "captcha"}]


def test_plan_fields_cover_letter_upload_when_package_has_one():
    inputs = [_field("Cover letter", type="file", id="cover")]
    assert plan_fields(_inspection(inputs), {})["hard_stops"][0]["reason"] == "cover_letter_required"
    plan = plan_fields(_inspection(inputs), {"cover_letter": {"value": "/tmp/cover.pdf"}})
    assert plan["hard_stops"] == [] and plan["uploads"][0]["answer_key"] == "cover_letter"


def test_plan_fields_sensitive_page_text_alone_does_not_stop():
    inputs = [_field("First name", id="fn"), _field("Email", type="email", id="em")]
    text = "Salary transparency\nWe sponsor visas\nPrivacy policy\nI certify that…"
    plan = plan_fields(_inspection(inputs, text), _answers())
    assert plan["hard_stops"] == [] and len(plan["fill"]) == 2


def test_load_answers_from_file_and_profile(tmp_path):
    path = tmp_path / "answers.json"
    path.write_text(json.dumps({"answers": {"phone": {"value": "1", "source": "x", "kind": "tel"}}}))
    assert module.load_answers(str(tmp_path / "jobs.db"), "j", answers_path=path)["phone"]["value"] == "1"
    db = tmp_path / "jobs.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE job_research(job_id TEXT PRIMARY KEY, payload_json TEXT)")
        conn.execute("INSERT INTO job_research VALUES(?, ?)", ("j", json.dumps(RESEARCH)))
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(PROFILE))
    loaded = module.load_answers(str(db), "j", profile_path=profile, jobs_mailbox=MAILBOX)
    assert loaded["salary_expectation"]["value"] == "62000" and loaded["email"]["value"] == MAILBOX
