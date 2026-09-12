"""Keyword pre-read of what a posting says about visa sponsorship."""
import pytest

import job_scoring


@pytest.mark.parametrize("text", [
    "Benefits include visa sponsorship, flights and 2 weeks paid accommodation.",
    "On-site in Dubai. We sponsor the employment visa. Why take this seat?",
    "Either remote or UAE based (with visa sponsorship). Paid access to tools.",
    "Benefits can include family visas, annual flight tickets, medical insurance.",
    "Planned support includes a UAE employment visa, medical insurance, the initial flight.",
    "We offer a comprehensive relocation package for international candidates.",
    "Work permit support is provided for the successful candidate.",
    "Visa sponsorship is available for this position.",
    "Relocation assistance and visa support provided.",
    "Employment visa will be provided by the company.",
    # Wordings the corpus audit found the first pattern set missed.
    "Benefits: Family benefits: visa, insurance, yearly airline ticket.",
    "Salary: 40k/50k AED PM Tax Free + Visa + Healthcare for self and family.",
    "The gross salary is maximum 26,000 AED with company provided visa, health insurance and yearly airfare.",
    "Red Hat will support relocation to the UAE for a successful candidate.",
    "25 days of annual leave, variety of pension plans, and relocation packages.",
    "Competitive tax-free salary + full relocation + premium airline travel benefits.",
    "The employer covers visa and accommodation costs, and candidates go through interviews.",
    "We help you with your relocation.",
    "Is role eligible for Immigration Sponsorship?: Yes",
    "Relocation & Residency: Full support for UAE residency and assistance with the UAE Golden Visa.",
])
def test_positive_wordings_read_as_offered(text):
    signal, evidence = job_scoring.sponsorship_signal(text)
    assert signal == "offered"
    assert evidence and evidence in " ".join(text.split())


@pytest.mark.parametrize("text", [
    "Please note that visa sponsorship and relocation support are not available for this position.",
    "Must have the right to work in Switzerland as the role does not provide sponsorship.",
    "No visa sponsorship. Candidates must already hold a valid UAE residence visa.",
    "We are unable to sponsor work visas for this role.",
    "This position will not sponsor visas. Local candidates only.",
    "Applicants must have existing work authorization; the company cannot sponsor.",
    "Sponsorship is not offered. No relocation.",
    "This is not a position for which sponsorship will be provided.",
    "Relocation and visa sponsorship will not be supported.",
    "Eligibility: Must hold a Saudi Premium Residency Visa or be a Saudi National.",
    "UAE Residence Visa: Yes (Self) UAE Health Insurance: Yes (Self)",
    "Please note, candidates will need to have the right to work in the jurisdiction.",
    "WORK AUTHORIZATION: Candidates must be authorized to work in assigned location.",
    "Only EU/Swiss Nationals or candidates with valid B/C Work Permits will be considered.",
    "Visa Requirements: Valid Visa for KSA",
    "Individuals with temporary visas such as H-1 or who need sponsorship for work authorization are not eligible.",
    "Candidate must currently be in the country on a transferable visa or a visit visa.",
])
def test_refusals_read_as_excluded(text):
    signal, evidence = job_scoring.sponsorship_signal(text)
    assert signal == "excluded"
    assert evidence


@pytest.mark.parametrize("text", [
    "Ebenfalls möglich ist ein MBA-Sponsorship-Programm!",
    "Our employee-led and company-sponsored affinity groups promote inclusion.",
    "You will benefit from strong executive sponsorship and strategic investment.",
    "Advertising, Partnerships & Sponsorships and overall marketing services.",
    "Company-sponsored team events and wellness resources.",
    "Build scalable Java services on AWS.",
    "Must be able to comply with export laws without sponsorship for an export license.",
    "Travel Requirements 10-25% Relocation Provided None Position Type New Grad",
    "Additional Information Relocation Assistance Provided: No",
    "Medical Insurance Premium for the dependents on residence visa will be paid on a copay policy.",
    "Company Description Senior IT Jobs UK is a specialized platform that connects professionals with visa-sponsored career opportunities.",
    "Experience integrating Visa and Mastercard payment networks.",
    "",
    None,
])
def test_unrelated_sponsor_mentions_and_silence_read_as_nothing(text):
    assert job_scoring.sponsorship_signal(text) == ("", "")


def test_an_explicit_refusal_outranks_a_relocation_mention():
    text = ("We offer a relocation package. However, visa sponsorship is not available "
            "for this position.")
    signal, _ = job_scoring.sponsorship_signal(text)
    assert signal == "excluded"


def test_evidence_is_the_matching_sentence_bounded_in_length():
    text = "Intro. " + ("Blah " * 200) + "We sponsor the employment visa for hires. Outro."
    signal, evidence = job_scoring.sponsorship_signal(text)
    assert signal == "offered"
    assert "sponsor the employment visa" in evidence
    assert len(evidence) <= job_scoring.SPONSORSHIP_EVIDENCE_LIMIT


def test_quote_matches_description_ignoring_case_and_spacing():
    description = "Benefits:\n  Visa   sponsorship, flights\nand housing."
    assert job_scoring.quote_in_text("visa sponsorship, flights and housing", description)
    assert not job_scoring.quote_in_text("we sponsor your visa", description)
    assert not job_scoring.quote_in_text("", description)
