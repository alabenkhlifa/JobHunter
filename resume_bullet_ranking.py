"""Order unchanged resume evidence against the responsibilities of a role."""

from dataclasses import dataclass
import re


def _text(value):
    return " ".join(re.sub(r"[^a-z0-9+#./-]+", " ", str(value or "").lower()).split())


def _has(text, pattern):
    return re.search(pattern, text) is not None


@dataclass(frozen=True)
class BulletMatch:
    score: int
    facets: frozenset[str]


def responsibility_weights(job_text, job_title=""):
    job, title = _text(job_text), _text(job_title)
    leading = _has(title, r"\b(lead|manager|head|chief|cto)\b")
    owning = leading or _has(title, r"\b(architect|principal)\b")
    backend = _has(job, r"\b(backend|back-end|services|microservices|jvm|api)\b")
    quality = _has(job, r"\b(test\w*|quality|code review|review code|ci|validator\w*)\b")
    return {
        "leadership": 140 if leading else 65 if _has(job, r"\b(lead|manag\w*|mentor\w*|grow)\b.{0,40}\b(team|engineer\w*)\b") else 0,
        "ownership": 70 if owning else 35 if _has(job, r"\b(own\w*|end.to.end|accountab\w*)\b") else 0,
        "backend": 80 if backend else 0,
        "production": 25 if _has(job, r"\b(production|live|cloud|operat\w*|reliab\w*)\b") else 0,
        "integrations": 110 if _has(job, r"\b(idempot\w*|reconcil\w*|eventual consistency|unreliable delivery|duplicates|two.way)\b") else 80 if _has(job, r"\b(sync|integrat\w*|messag\w*|notification\w*|upstream)\b") else 0,
        "migrations": 95 if _has(job, r"\b(migrat\w*|backfill\w*|dual writes|staged rollouts)\b") else 0,
        "quality": (105 if leading else 55) if quality else 0,
        "ai_workflow": 45 if _has(job, r"llm.generated code|language models.{0,60}(write code|writing code)|llm.{0,30}(code|coding)|ai.assisted|agentic development") else 0,
        "reliability": 60 if _has(job, r"\b(reliab\w*|correctness|incident\w*|availability|resilien\w*|on-call)\b") else 0,
    }


def bullet_match(bullet, job_text, job_title=""):
    candidate, job = _text(bullet), _text(job_text)
    weights = responsibility_weights(job, job_title)
    delivered = _has(candidate, r"\b(built|build|maintain\w*|operat\w*|develop\w*|designed|deployed|delivered|rewrote|owned|architected)\b")
    backend = _has(candidate, r"\b(backend|back-end|services|microservices|platform|api)\b")
    facets = set()
    if delivered and backend:
        facets.add("backend")
    if _has(candidate, r"\b(led|lead|manag\w*|coordinat\w*|mentor\w*|hired)\b") and _has(candidate, r"\b(team|engineer\w*|developer\w*)\b"):
        facets.add("leadership")
    if backend and _has(candidate, r"\b(sole\w*|single-handedly|owner|ownership|owned|end.to.end|responsible for)\b"):
        facets.add("ownership")
    if backend and _has(candidate, r"\b(production|operat\w*|deployed|serving|users)\b"):
        facets.add("production")
    if _has(candidate, r"\b(webhook\w*|idempot\w*|reconcil\w*|retry|retries|sync\w*|duplicate\w*)\b"):
        facets.add("integrations")
    if _has(candidate, r"\b(migrat\w*|upgrad\w*|backfill\w*)\b") and _has(candidate, r"\b(production|downtime|live|services|microservices|schema|database)\b"):
        facets.add("migrations")
    if _has(candidate, r"\b(test\w*|validator\w*|code review|pr review|ci gates|automated tests)\b"):
        facets.add("quality")
    if _has(candidate, r"\b(agent\w*|llm\w*|ai.assisted|agentic)\b") and _has(candidate, r"\b(workflow|code|coding|review|development|validators|tools|prototypes)\b"):
        facets.add("ai_workflow")
    if _has(candidate, r"\b(eliminat\w*|prevent\w*|fixed|resolved|without|zero)\b") and _has(candidate, r"\b(duplicate\w*|outage\w*|downtime|retry|retries|failure\w*)\b"):
        facets.add("reliability")
    facets = {facet for facet in facets if weights[facet]}
    score = sum(weights[facet] for facet in facets)

    # A language in validators is adjacent evidence, not production-service experience.
    skill_score = 0
    for language in ("java", "kotlin", "python", "typescript", "c#", "golang"):
        token = rf"(?<![a-z0-9]){re.escape(language)}(?![a-z0-9])"
        if _has(job, token) and _has(candidate, token):
            points = 40 if backend and delivered else 12
            tooling_only = _has(candidate, r"\b(validator\w*|script\w*|git hooks)\b") and not _has(candidate, r"\b(python (services|microservices|api)|production python|python backend)\b")
            if language == "python" and tooling_only and _has(job, r"python.{0,55}(services|production)|production.{0,55}python"):
                points = 2
            skill_score = max(skill_score, points)
    score += skill_score
    if facets:
        if _has(candidate, r"\b\d[\d,]*\b.{0,35}\b(users|services|engineers|requests|batches|seconds|minutes)\b|\bteam of (four|4)\b"):
            score += 15
        if _has(candidate, r"\b(eliminated|without downtime|zero.downtime|prevented|reduced)\b"):
            score += 10
    ignored = {"the", "and", "for", "with", "from", "that", "this", "our", "your", "you", "team", "software", "engineer", "engineering"}
    words = set(re.findall(r"[a-z0-9+#.]+", candidate)) - ignored
    job_words = set(re.findall(r"[a-z0-9+#.]+", job)) - ignored
    score += min(20, 2 * len({word for word in words & job_words if len(word) > 2}))
    return BulletMatch(score, frozenset(facets))


def order_bullet_indexes(bullets, job_text, job_title=""):
    """Cover distinct responsibilities in the opening three, with stable ties."""
    matches = [bullet_match(bullet, job_text, job_title) for bullet in bullets]
    weights = responsibility_weights(job_text, job_title)
    pending = list(range(len(bullets)))
    ordered, covered = [], set()
    while pending:
        def priority(index):
            novelty = sum(weights[facet] for facet in matches[index].facets - covered)
            bonus = novelty / 3 if len(ordered) < 3 and ordered else 0
            return matches[index].score + bonus, -index
        best = max(pending, key=priority)
        pending.remove(best)
        ordered.append(best)
        covered.update(matches[best].facets)
    return ordered
