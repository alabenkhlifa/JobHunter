from collections import Counter

from resume_bullet_ranking import bullet_match, order_bullet_indexes, responsibility_weights


def ordered(bullets, description, title):
    return [bullets[index] for index in order_bullet_indexes(bullets, description, title)]


def test_jvm_opening_prioritizes_production_outcomes_over_generic_progression():
    bullets = [
        "Progressed to Senior Engineer while coordinating four engineers.",
        "Implemented HTTP communication and MQTT messaging.",
        "Built ten Kotlin microservices serving 15,000 users.",
        "Eliminated duplicate notifications caused by webhook retries.",
        "Migrated three production services without downtime.",
        "Rewrote two Java services in Kotlin to improve readability.",
    ]
    result = ordered(bullets,
                     "Kotlin backend services, integration correctness, idempotency, "
                     "live migrations, reliability and testing.", "Senior Backend Engineer")

    assert set(result[:3]) == set(bullets[2:5])
    assert Counter(result) == Counter(bullets)


def test_lead_opening_covers_team_product_and_integrations_without_repetition():
    bullets = [
        "Built agent tools for spec and PR review.",
        "Owned architecture across ten backend services serving 15,000 users.",
        "Led a team of four engineers; deployed and operated backend services.",
        "Solely designed and built a backend serving 4,000 users with an AI-assisted workflow.",
        "Eliminated duplicate notifications from webhook retries.",
    ]
    result = ordered(bullets,
                     "Own production backend services, lead engineers, prevent sync duplicates, "
                     "reliability, code review and LLM-generated code quality.", "Lead Engineer")

    assert result[:3] == [bullets[2], bullets[3], bullets[4]]
    assert Counter(result) == Counter(bullets)


def test_python_validator_is_not_ranked_as_production_python_delivery():
    job = "Build and operate production Python backend services."
    service = bullet_match("Built and operated production Python services serving 1,000 users.", job)
    tooling = bullet_match("Built Python validators for the backend release workflow.", job)

    assert service.score > tooling.score
    assert "production" not in tooling.facets
    assert "production" in service.facets


def test_java_does_not_match_javascript_and_repeated_keywords_do_not_add_weight():
    job = "Java backend services in production."
    javascript = "Built JavaScript backend services."
    java = "Built Java backend services."

    assert bullet_match(java, job).score > bullet_match(javascript, job).score
    assert bullet_match(java, job).score == bullet_match(java + " Java Java Java", job).score
    assert bullet_match(java, job).score > bullet_match("Java production backend services", job).score


def test_ties_empty_inputs_and_irrelevant_bullets_keep_source_order():
    assert order_bullet_indexes([], "backend") == []
    assert order_bullet_indexes(["First unrelated fact.", "Second unrelated fact."], "backend") == [0, 1]
    assert order_bullet_indexes(["Built Java services."] * 3, "Java backend") == [0, 1, 2]


def test_architecture_title_alone_does_not_create_a_team_leadership_requirement():
    assert responsibility_weights("Design cloud services", "Software Architect")["leadership"] == 0
    assert responsibility_weights("Lead a team of engineers", "Software Architect")["leadership"] > 0
