import scraper


def test_find_tech_uses_whole_terms_inside_composite_words():
    found = scraper._find_tech_in_text(
        "JavaScript supports ongoing zero-trust delivery."
    )

    assert "javascript" in found
    assert "zero-trust" in found
    assert "java" not in found
    assert "go" not in found
    assert "rust" not in found


def test_find_tech_matches_standalone_short_language_names():
    found = scraper._find_tech_in_text("Required languages: Java, Go, and Rust.")

    assert "java" in found
    assert "go" in found
    assert "rust" in found


def test_find_tech_matches_punctuation_bearing_terms():
    found = scraper._find_tech_in_text(
        "The platform uses C#, .NET, Node.js, and CI/CD pipelines."
    )

    assert "c#" in found
    assert ".net" in found
    assert "node.js" in found
    assert "ci/cd" in found


def test_find_tech_matches_names_followed_by_hyphenated_modifiers():
    found = scraper._find_tech_in_text(
        "Go-based services integrate with Node.js-based and C#-based systems."
    )

    assert "go" in found
    assert "node.js" in found
    assert "c#" in found


def test_extract_tech_keywords_tracks_architecture_platform_requirements():
    required, nice_to_have = scraper.extract_tech_keywords(
        """Requirements
        Design a cloud-native SaaS platform using Kubernetes, a service mesh,
        GitOps, observability, multi-tenant security, zero-trust controls,
        privacy, compliance, and data residency.
        Nice to have
        AI model operations, MLOps, and LLMOps experience.
        """
    )

    assert {
        "cloud-native",
        "saas",
        "kubernetes",
        "service mesh",
        "gitops",
        "observability",
        "multi-tenant",
        "security",
        "zero-trust",
        "privacy",
        "compliance",
        "data residency",
    }.issubset(required)
    assert {"ai model operations", "mlops", "llmops"}.issubset(nice_to_have)
    assert "rust" not in required


def test_find_tech_ignores_rest_used_as_an_english_word():
    # "projects through the rest of the year" recorded tech_required = 'rest'
    # on a building-architecture posting. 19 corpus rows read the same way.
    assert "rest" not in scraper._find_tech_in_text(
        "A secured pipeline of projects through the rest of the year."
    )
    assert "rest" not in scraper._find_tech_in_text(
        "Rest assured that we consider every applicant fairly."
    )


def test_find_tech_still_reads_rest_as_a_technology():
    for text in ("Design and build REST APIs.", "Experience with REST services.",
                 "You will own our REST endpoints.", "SOAP/REST integration patterns."):
        assert "rest" in scraper._find_tech_in_text(text), text


def test_find_tech_reads_rest_per_occurrence_not_per_description():
    # One English use must not veto a real one in the same description.
    found = scraper._find_tech_in_text(
        "Through the rest of the year we will ship REST APIs in Java."
    )
    assert "rest" in found


def test_find_tech_ignores_go_used_as_an_english_word():
    # 205 of the 657 whole-word "go" hits in the corpus are "go to market".
    for text in ("Own the go-to-market plan.", "Support go live readiness.",
                 "We go beyond what customers expect.", "A go-getter attitude."):
        assert "go" not in scraper._find_tech_in_text(text), text


def test_find_tech_still_reads_go_as_a_language_next_to_the_english_word():
    found = scraper._find_tech_in_text(
        "Own the go-to-market plan; the services themselves are written in Go."
    )
    assert "go" in found
