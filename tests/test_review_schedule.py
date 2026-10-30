from __future__ import annotations

import pytest

from h2h_dev_tools import review_execution as execution


@pytest.mark.parametrize(
    ("shards", "timeout", "estimate", "waves", "fits"),
    [
        (54, 3600, 900, 14, False),
        (54, 14400, 900, 14, True),
        (8, 3600, 900, 2, True),
        (4, 900, 600, 1, True),
        (5, 900, 600, 2, False),
    ],
)
def test_complete_queue_must_fit_the_existing_budget(
    shards: int, timeout: int, estimate: int, waves: int, fits: bool
) -> None:
    started = 100.0
    cutoff = execution.shard_deadline(started, started + timeout, shards)
    schedule = execution.review_schedule(
        shards,
        execution_deadline=cutoff - execution.CLEANUP_RESERVE,
        now=started + 30,
        minimum_shard_seconds=estimate,
    )
    assert schedule.waves == waves
    assert schedule.required_seconds == waves * estimate
    assert schedule.concurrency == min(shards, 4)
    if fits:
        schedule.require_capacity()
    else:
        with pytest.raises(
            execution.ExecutionError, match="Review schedule cannot fit"
        ):
            schedule.require_capacity()


def test_capacity_accounts_for_elapsed_preparation_and_exact_boundary() -> None:
    at_boundary = execution.review_schedule(
        5, execution_deadline=1300, now=100, minimum_shard_seconds=600
    )
    at_boundary.require_capacity()
    after_preparation = execution.review_schedule(
        5, execution_deadline=1300, now=101, minimum_shard_seconds=600
    )
    with pytest.raises(execution.ExecutionError, match=r"1199\.0s available"):
        after_preparation.require_capacity()


def test_unspecified_estimate_does_not_invent_a_per_shard_limit() -> None:
    schedule = execution.review_schedule(
        54, execution_deadline=200, now=100, minimum_shard_seconds=0
    )
    schedule.require_capacity()
    assert schedule.required_seconds == 0
    expired = execution.review_schedule(
        1, execution_deadline=100, now=100, minimum_shard_seconds=0
    )
    with pytest.raises(execution.ExecutionError):
        expired.require_capacity()


@pytest.mark.parametrize(
    ("shards", "deadline", "now", "estimate"),
    [
        (0, 100, 0, 0),
        (1, float("inf"), 0, 0),
        (1, 100, float("nan"), 0),
        (1, 100, 0, -1),
        (1, 100, 0, 14401),
    ],
)
def test_invalid_scheduling_inputs_fail_closed(
    shards: int, deadline: float, now: float, estimate: int
) -> None:
    with pytest.raises(execution.ExecutionError, match="Invalid review scheduling"):
        execution.review_schedule(
            shards,
            execution_deadline=deadline,
            now=now,
            minimum_shard_seconds=estimate,
        )
