from datetime import timedelta

from supplier_chaser.workflow.scheduler import retry_delay


def test_retry_backoff_and_rate_limit(clock):
    assert retry_delay(1) == timedelta(seconds=2)
    assert retry_delay(20) <= timedelta(seconds=300)
    assert retry_delay(1, "120", clock.now()) == timedelta(seconds=120)
    assert retry_delay(1, "Sat, 21 Nov 2026 13:03:00 GMT", clock.now()) == timedelta(minutes=3)
    assert retry_delay(1, "invalid", clock.now()) == timedelta(seconds=2)
