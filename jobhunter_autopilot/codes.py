"""Hard-stop codes shared by the autopilot and the unattended engine mode.

A hard stop hands the job back to the manual flow: the application stage
becomes ``blocked_auto_apply`` with ``error='<code>: <detail>'`` and the
research card (Apply/Ignore/Details) is sent. The engine uses the same
literals, so they never change spelling.
"""

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

HARD_STOPS = frozenset({
    CAPTCHA, PHONE_OR_IDENTITY, NO_AI_ANSWER, SALARY_PERIOD_AMBIGUOUS, FORM_UNVERIFIED,
    EMPLOYER_UNVERIFIED, ACCOUNT_CREATION_FAILED, EMAIL_VERIFICATION_UNSUPPORTED,
    PACKAGE_NOT_READY, COVER_LETTER_FAILED, LINKEDIN_EASY_APPLY, RECEIPT_UNVERIFIED,
    BROWSER_UNAVAILABLE, BUDGET_EXCEEDED, RESUME_NEEDS_REVIEW,
})

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
