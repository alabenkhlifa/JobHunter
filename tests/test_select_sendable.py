import job_scoring


def test_market_region_finds_each_of_the_five_markets():
    assert job_scoring.market_region("Dubai, United Arab Emirates") == "dubai"
    assert job_scoring.market_region("Abu Dhabi, United Arab Emirates") == "abu dhabi"
    assert job_scoring.market_region("Jeddah, Saudi Arabia") == "jeddah"
    assert job_scoring.market_region("Jiddah, Makkah, Saudi Arabia") == "jeddah"
    assert job_scoring.market_region("Riyadh, Saudi Arabia") == "riyadh"
    assert job_scoring.market_region("Zürich, Switzerland") == "switzerland"
    assert job_scoring.market_region("Geneva, Switzerland") == "switzerland"


def test_market_region_does_not_merge_dubai_and_abu_dhabi():
    # The bug this function exists to avoid: market_country groups both
    # under "uae". market_region must not.
    assert job_scoring.market_region("Dubai") != job_scoring.market_region("Abu Dhabi")


def test_market_region_is_unknown_for_a_bare_country_or_unplaced_location():
    assert job_scoring.market_region("United Arab Emirates") == "unknown"
    assert job_scoring.market_region("Saudi Arabia") == "unknown"
    assert job_scoring.market_region("") == "unknown"
    assert job_scoring.market_region(None) == "unknown"


def review(**over):
    base = {
        "id": "j1", "market": "dubai",
        "ai_verdict": "send", "ai_sponsorship": "implied", "ai_rank": 1,
    }
    base.update(over)
    return base


def test_select_sendable_returns_nothing_for_empty_input():
    assert job_scoring.select_sendable([]) == []


def test_select_sendable_filters_out_non_send_verdicts():
    jobs = [
        review(id="hold", ai_verdict="hold"),
        review(id="reject", ai_verdict="reject"),
        review(id="send", ai_verdict="send"),
    ]
    result = job_scoring.select_sendable(jobs)
    assert [j["id"] for j in result] == ["send"]


def test_select_sendable_filters_out_doubtful_and_excluded_sponsorship():
    jobs = [
        review(id="doubtful", ai_sponsorship="doubtful"),
        review(id="excluded", ai_sponsorship="excluded"),
        review(id="offered", ai_sponsorship="offered"),
        review(id="implied", ai_sponsorship="implied"),
    ]
    result = job_scoring.select_sendable(jobs)
    assert {j["id"] for j in result} == {"offered", "implied"}


def test_select_sendable_holds_a_market_to_its_floor_when_others_use_their_full_share():
    # Dubai has 5 candidates but Jeddah is using its own full 3-slot share,
    # so there is no unused capacity anywhere for Dubai's 4th/5th to spill
    # into -- the floor holds exactly at per_market for both.
    jobs = [review(id=f"dubai-{i}", market="dubai", ai_rank=i) for i in range(1, 6)]
    jobs += [review(id=f"jeddah-{i}", market="jeddah", ai_rank=i) for i in range(6, 9)]
    result = job_scoring.select_sendable(jobs, per_market=3, cap=6)
    assert [j["id"] for j in result] == [
        "dubai-1", "dubai-2", "dubai-3", "jeddah-6", "jeddah-7", "jeddah-8",
    ]


def test_select_sendable_lets_one_market_exceed_the_floor_when_every_other_is_empty():
    # Confirmed design: a market's own unused capacity flows to whichever
    # market has more candidates. With every other market absent, all of
    # their unused slots are available, so Dubai can use the whole cap.
    jobs = [review(id=f"dubai-{i}", market="dubai", ai_rank=i) for i in range(1, 6)]
    result = job_scoring.select_sendable(jobs, per_market=3, cap=12)
    assert [j["id"] for j in result] == [f"dubai-{i}" for i in range(1, 6)]


def test_select_sendable_spills_remaining_slots_to_other_markets_by_rank():
    jobs = [
        review(id="dubai-1", market="dubai", ai_rank=1),
        review(id="dubai-2", market="dubai", ai_rank=2),
        review(id="jeddah-1", market="jeddah", ai_rank=3),
    ]
    result = job_scoring.select_sendable(jobs, per_market=3, cap=12)
    assert [j["id"] for j in result] == ["dubai-1", "dubai-2", "jeddah-1"]


def test_select_sendable_caps_total_at_the_global_limit_via_spillover():
    # 4 markets x 2 jobs each = 8, all within each market's top 3, so every
    # job clears the per-market floor. Cap trims the spillover, not the floor.
    jobs = []
    rank = 1
    for market in ("dubai", "abu dhabi", "jeddah", "switzerland"):
        for _ in range(2):
            jobs.append(review(id=f"{market}-{rank}", market=market, ai_rank=rank))
            rank += 1
    result = job_scoring.select_sendable(jobs, per_market=3, cap=6)
    assert len(result) == 6
    assert [j["ai_rank"] for j in result] == [1, 2, 3, 4, 5, 6]


def test_select_sendable_truncates_the_per_market_floor_itself_when_it_exceeds_the_cap():
    # 5 markets x 3 jobs each = 15 sendable jobs, every one inside its own
    # market's top 3 -- the floor alone exceeds cap=12. No market may exceed
    # 3, but the overall list truncates to the global top 12 by rank.
    jobs = []
    rank = 1
    for market in ("dubai", "abu dhabi", "jeddah", "switzerland", "riyadh"):
        for _ in range(3):
            jobs.append(review(id=f"{market}-{rank}", market=market, ai_rank=rank))
            rank += 1
    result = job_scoring.select_sendable(jobs, per_market=3, cap=12)
    assert len(result) == 12
    ranks = [j["ai_rank"] for j in result]
    assert ranks == sorted(ranks)
    assert ranks[-1] == 12
    counts = {}
    for j in result:
        counts[j["market"]] = counts.get(j["market"], 0) + 1
    assert all(count <= 3 for count in counts.values())


def test_select_sendable_a_thin_market_frees_spillover_capacity():
    jobs = [
        review(id="dubai-1", market="dubai", ai_rank=1),
        # jeddah has only 1 sendable job -- its other 2 slots are unused,
        # not blocked, so a lower-ranked dubai job can spill in.
        review(id="jeddah-1", market="jeddah", ai_rank=2),
        review(id="dubai-2", market="dubai", ai_rank=3),
        review(id="dubai-3", market="dubai", ai_rank=4),
        review(id="dubai-4", market="dubai", ai_rank=5),
    ]
    result = job_scoring.select_sendable(jobs, per_market=3, cap=12)
    ids = {j["id"] for j in result}
    assert ids == {"dubai-1", "dubai-2", "dubai-3", "jeddah-1", "dubai-4"}


def test_select_sendable_returns_jobs_in_rank_order():
    jobs = [
        review(id="third", market="dubai", ai_rank=3),
        review(id="first", market="jeddah", ai_rank=1),
        review(id="second", market="switzerland", ai_rank=2),
    ]
    result = job_scoring.select_sendable(jobs, per_market=3, cap=12)
    assert [j["id"] for j in result] == ["first", "second", "third"]
