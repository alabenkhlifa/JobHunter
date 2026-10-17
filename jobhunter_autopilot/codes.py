"""Hard-stop codes shared by the autopilot and the unattended engine mode.

A hard stop hands the job back to the manual flow: the application stage
becomes ``blocked_auto_apply`` with ``error='<code>: <detail>'`` and the
research card (Apply/Ignore/Details) is sent. The engine uses the same
literals, so they never change spelling. The auto-apply skill's hard-stop
table lists the same codes.

``MANUAL_HOSTS`` are application sites behind anti-bot checks that stop an
automated browser. The plan marks their offers manual up front
(``anti_bot_ats``): no browser attempt, a card with the apply link and the
generated resume instead.

``MANUAL_APPLY`` are the hard stops he finishes himself on the employer's
site; when the job has a tailored package, ``blocked`` sends that same
"Apply by hand" card (link, resume) instead of the research card.
"""

from urllib.parse import urlsplit

CAPTCHA = "captcha"
PHONE_OR_IDENTITY = "phone_or_identity"
NO_AI_ANSWER = "no_ai_answer"
UNANSWERABLE_PREFIX = "unanswerable:"
SALARY_PERIOD_AMBIGUOUS = "salary_period_ambiguous"
FORM_UNVERIFIED = "form_unverified"
EMPLOYER_UNVERIFIED = "employer_unverified"
ACCOUNT_CREATION_FAILED = "account_creation_failed"
EMAIL_VERIFICATION_UNSUPPORTED = "email_verification_unsupported"
PACKAGE_NOT_READY = "package_not_ready"
COVER_LETTER_FAILED = "cover_letter_failed"
LINKEDIN_EASY_APPLY = "linkedin_easy_apply"
RECEIPT_UNVERIFIED = "receipt_unverified"
BROWSER_UNAVAILABLE = "browser_unavailable"
BUDGET_EXCEEDED = "budget_exceeded"
RESUME_NEEDS_REVIEW = "resume_needs_review"
ANTI_BOT_ATS = "anti_bot_ats"
LINKEDIN_SIGNED_OUT = "linkedin_signed_out"
MULTI_STEP_FORM = "multi_step_form"

HARD_STOPS = frozenset({
    CAPTCHA, PHONE_OR_IDENTITY, NO_AI_ANSWER, SALARY_PERIOD_AMBIGUOUS, FORM_UNVERIFIED,
    EMPLOYER_UNVERIFIED, ACCOUNT_CREATION_FAILED, EMAIL_VERIFICATION_UNSUPPORTED,
    PACKAGE_NOT_READY, COVER_LETTER_FAILED, LINKEDIN_EASY_APPLY, RECEIPT_UNVERIFIED,
    BROWSER_UNAVAILABLE, BUDGET_EXCEEDED, RESUME_NEEDS_REVIEW, ANTI_BOT_ATS, LINKEDIN_SIGNED_OUT,
    MULTI_STEP_FORM,
})
MANUAL_APPLY = frozenset({MULTI_STEP_FORM, LINKEDIN_EASY_APPLY, FORM_UNVERIFIED, CAPTCHA, ANTI_BOT_ATS})

# Hosts (and their subdomains) whose apply flow sits behind an anti-bot check.
# SmartRecruiters' application pages answer automated Chromium with a DataDome
# "Device Check"; smartr.me is its one-click apply site.
MANUAL_HOSTS = ("smartrecruiters.com", "smartr.me")

DESCRIPTIONS = {
    CAPTCHA: "the site showed a CAPTCHA",
    PHONE_OR_IDENTITY: "the site asked for phone or identity verification",
    NO_AI_ANSWER: "the employer asks candidates not to use AI to apply",
    SALARY_PERIOD_AMBIGUOUS: "the salary field's period or currency is unclear",
    FORM_UNVERIFIED: "the form could not be matched to this offer",
    EMPLOYER_UNVERIFIED: "the employer behind the form could not be verified",
    ACCOUNT_CREATION_FAILED: "the ATS account could not be created",
    EMAIL_VERIFICATION_UNSUPPORTED: "the email verification step is not supported",
    PACKAGE_NOT_READY: "the resume package could not be generated",
    COVER_LETTER_FAILED: "the required cover letter could not be generated",
    LINKEDIN_EASY_APPLY: "LinkedIn Easy Apply is not automated",
    RECEIPT_UNVERIFIED: "the submission could not be confirmed",
    BROWSER_UNAVAILABLE: "the browser was unavailable",
    BUDGET_EXCEEDED: "the application took longer than its time budget",
    RESUME_NEEDS_REVIEW: "the generated resume scored below the review threshold",
    ANTI_BOT_ATS: "the application site blocks automated browsers; apply by hand",
    LINKEDIN_SIGNED_OUT: "the application browser is not signed in to LinkedIn",
    MULTI_STEP_FORM: "the application is a multi-step form the autopilot cannot fill yet",
}

MAX_LABEL = 120


def is_valid(code):
    """A known hard stop, or ``unanswerable:<label>`` with a short label."""
    if not isinstance(code, str):
        return False
    if code in HARD_STOPS:
        return True
    if code.startswith(UNANSWERABLE_PREFIX):
        label = code[len(UNANSWERABLE_PREFIX):].strip()
        return bool(label) and len(label) <= MAX_LABEL and "\n" not in label
    return False


def describe(code):
    """Plain text for Telegram; unknown codes are shown as they are."""
    code = str(code or "")
    if code.startswith(UNANSWERABLE_PREFIX):
        return f"a question could not be answered safely: {code[len(UNANSWERABLE_PREFIX):].strip()}"
    return DESCRIPTIONS.get(code, code.replace("_", " "))


def manual_host(url):
    """The ``MANUAL_HOSTS`` entry the URL's host belongs to, else None."""
    try:
        host = (urlsplit(str(url or "").strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    for base in MANUAL_HOSTS:
        if host == base or host.endswith("." + base):
            return base
    return None
