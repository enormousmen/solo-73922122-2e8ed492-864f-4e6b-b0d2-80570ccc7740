from __future__ import annotations

import random

import pytest

from app.matcher import (
    InstrumentRow,
    SubmissionRow,
    analyze_matches,
    candidate_distance,
    levenshtein,
    normalize_sample_no,
)


def subs(*pairs: tuple[str, str]) -> list[SubmissionRow]:
    return [
        SubmissionRow(row_id=f"S{index}", sample_no=sample, sampling_point=point)
        for index, (sample, point) in enumerate(pairs)
    ]


def instruments(*pairs: tuple[str, float]) -> list[InstrumentRow]:
    return [
        InstrumentRow(row_id=f"I{index}", sample_no=sample, value=value)
        for index, (sample, value) in enumerate(pairs)
    ]


def brute_force(
    submission_rows: list[SubmissionRow],
    instrument_rows: list[InstrumentRow],
):
    """小规模穷举，作为最优性和候选集合的独立基准。"""

    edges: dict[tuple[int, int], int] = {}
    for i, instrument in enumerate(instrument_rows):
        for j, submission in enumerate(submission_rows):
            distance = candidate_distance(submission.sample_no, instrument.sample_no)
            if distance is not None:
                edges[(i, j)] = distance

    best_pairs = -1
    best_cost = 10**9
    optimal: list[tuple[int | None, ...]] = []

    def visit(
        inst_index: int,
        used_submissions: frozenset[int],
        pairs: int,
        cost: int,
        assignments: tuple[int | None, ...],
    ) -> None:
        nonlocal best_pairs, best_cost, optimal
        if inst_index == len(instrument_rows):
            if pairs > best_pairs or (pairs == best_pairs and cost < best_cost):
                best_pairs = pairs
                best_cost = cost
                optimal = [assignments]
            elif pairs == best_pairs and cost == best_cost:
                optimal.append(assignments)
            return

        visit(
            inst_index + 1,
            used_submissions,
            pairs,
            cost,
            assignments + (None,),
        )
        for sub_index in range(len(submission_rows)):
            if sub_index in used_submissions:
                continue
            distance = edges.get((inst_index, sub_index))
            if distance is None:
                continue
            visit(
                inst_index + 1,
                used_submissions | {sub_index},
                pairs + 1,
                cost + distance,
                assignments + (sub_index,),
            )

    visit(0, frozenset(), 0, 0, ())

    possible_subs = [
        {
            assignment[inst_index]
            for assignment in optimal
            if assignment[inst_index] is not None
        }
        for inst_index in range(len(instrument_rows))
    ]
    possible_unmatched = [
        any(assignment[inst_index] is None for assignment in optimal)
        for inst_index in range(len(instrument_rows))
    ]
    return best_pairs, best_cost, possible_subs, possible_unmatched


def test_levenshtein_and_case_insensitive_candidate_distance():
    assert levenshtein("ABC", "abc") == 3
    assert normalize_sample_no("aBc1") == "ABC1"
    assert candidate_distance("Wq1001", "wQ1002") == 1
    assert candidate_distance("ABC", "abcd") == 1
    assert candidate_distance("ABC", "XYZ") is None


def test_exact_match_is_confirmed():
    submission_rows = subs(("WQ1001", "东岸"))
    instrument_rows = instruments(("wq1001", 1.2))

    analysis = analyze_matches(submission_rows, instrument_rows)

    assert analysis.max_pairs == 1
    assert analysis.min_total_distance == 0
    resolution = analysis.resolutions[0]
    assert resolution.is_confirmed
    assert resolution.candidates[0].submission.row_id == "S0"


def test_candidate_can_be_ambiguous_without_possible_unmatched():
    submission_rows = subs(
        ("WQ1001", "东岸"),
        ("WQ1003", "西岸"),
        ("WQ5555", "南岸"),
        ("WQ1003", "西岸复采"),
    )
    instrument_rows = instruments(
        ("WQ1002", 7.4),
        ("WQ1004", 7.2),
        ("WQ1000", 7.5),
        ("WQ5555", 8.0),
        ("ZZZ999", 6.9),
    )

    analysis = analyze_matches(submission_rows, instrument_rows)

    assert analysis.max_pairs == 4
    assert analysis.min_total_distance == 3
    assert analysis.resolutions[0].is_confirmed is False
    assert analysis.resolutions[3].is_confirmed is True
    assert analysis.resolutions[3].candidates[0].submission.row_id == "S2"
    assert analysis.resolutions[4].candidates == ()
    assert analysis.resolutions[4].possible_unmatched is True


def test_matchable_record_can_be_unmatched_in_another_optimal_solution():
    submission_rows = subs(
        ("WQ1001", "东岸"),
        ("WQ1003", "西岸"),
    )
    instrument_rows = instruments(
        ("WQ1002", 7.4),
        ("WQ1004", 7.2),
        ("WQ1000", 7.5),
    )

    analysis = analyze_matches(submission_rows, instrument_rows)

    assert analysis.max_pairs == 2
    assert analysis.min_total_distance == 2
    for resolution in analysis.resolutions:
        assert resolution.possible_unmatched is True
        assert {row.submission.row_id for row in resolution.candidates} == {
            "S0",
            "S1",
        }
        assert resolution.is_confirmed is False


def test_duplicate_sample_numbers_stay_separate_rows():
    submission_rows = subs(
        ("WQ1001", "东岸"),
        ("WQ1001", "西岸"),
    )
    instrument_rows = instruments(("WQ1001", 5.0))

    analysis = analyze_matches(submission_rows, instrument_rows)

    resolution = analysis.resolutions[0]
    assert resolution.possible_unmatched is False
    assert {row.submission.sampling_point for row in resolution.candidates} == {
        "东岸",
        "西岸",
    }
    assert resolution.is_confirmed is False


def test_maximum_pair_count_has_priority_over_distance():
    submission_rows = subs(
        ("WQ1001", "东岸"),
        ("WQ1009", "西岸"),
    )
    instrument_rows = instruments(
        ("WQ1001", 1.0),
        ("WQ1008", 2.0),
    )

    analysis = analyze_matches(submission_rows, instrument_rows)

    assert analysis.max_pairs == 2
    # 一个精确匹配且另一条不配对的方案总距离为 0，但题设要求先最大化
    # 配对数；两对时的最小总距离为 1。
    assert analysis.min_total_distance == 1


@pytest.mark.parametrize("seed", range(30))
def test_matches_brute_force_on_random_small_inputs(seed: int):
    rng = random.Random(seed)

    def random_ids() -> list[str]:
        # 使用短编号，确保较容易产生距离不超过 2 的候选边。
        return [
            "".join(rng.choice("AB12") for _ in range(rng.choice([3, 4, 5])))
            for _ in range(rng.randint(1, 3))
        ]

    samples = random_ids()
    recorded = random_ids()

    submission_rows = [
        SubmissionRow(f"S{index}", sample, f"P{index}")
        for index, sample in enumerate(samples)
    ]
    instrument_rows = [
        InstrumentRow(f"I{index}", sample, float(index))
        for index, sample in enumerate(recorded)
    ]

    analysis = analyze_matches(submission_rows, instrument_rows)
    expected_pairs, expected_cost, expected_candidates, expected_unmatched = (
        brute_force(submission_rows, instrument_rows)
    )

    assert analysis.max_pairs == expected_pairs
    assert analysis.min_total_distance == expected_cost
    for index, resolution in enumerate(analysis.resolutions):
        assert {candidate.submission.row_id for candidate in resolution.candidates} == {
            submission_rows[sub_index].row_id
            for sub_index in expected_candidates[index]
            if sub_index is not None
        }
        assert resolution.possible_unmatched == expected_unmatched[index]
