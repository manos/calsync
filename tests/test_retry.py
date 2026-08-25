import pytest

from calsync.providers.base import ProviderError, TransientError
from calsync.retry import retry_call


def test_returns_the_value_without_retrying():
    calls = []
    assert retry_call(lambda: calls.append(1) or "ok", sleep=lambda _: None) == "ok"
    assert len(calls) == 1


def test_retries_transient_failures_until_one_succeeds():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise TransientError("429")
        return "ok"

    assert retry_call(flaky, sleep=lambda _: None) == "ok"
    assert len(attempts) == 3


def test_gives_up_after_the_attempt_limit():
    attempts = []

    def always_failing():
        attempts.append(1)
        raise TransientError("503")

    with pytest.raises(TransientError):
        retry_call(always_failing, attempts=3, sleep=lambda _: None)
    assert len(attempts) == 3


def test_permanent_errors_are_not_retried():
    attempts = []

    def bad_request():
        attempts.append(1)
        raise ProviderError("400 invalid")

    with pytest.raises(ProviderError):
        retry_call(bad_request, sleep=lambda _: None)
    assert len(attempts) == 1


def test_delays_grow_exponentially():
    delays: list[float] = []

    def always_failing():
        raise TransientError("503")

    with pytest.raises(TransientError):
        retry_call(
            always_failing,
            attempts=4,
            base_delay=1.0,
            sleep=delays.append,
            jitter=lambda: 0.0,
        )
    assert delays == [1.0, 2.0, 4.0]
