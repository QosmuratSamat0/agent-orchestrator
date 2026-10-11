import pytest

from agent_orchestrator import statuses, waiter


@pytest.mark.parametrize(
    ("alias", "category"),
    [
        (alias, category)
        for aliases, category in (
            (statuses.COMPLETED_STATUSES, "completed"),
            (statuses.FAILED_STATUSES, "failed"),
            (statuses.CANCELLED_STATUSES, "cancelled"),
            (statuses.RUNNING_STATUSES, "running"),
            (statuses.QUEUED_STATUSES, "queued"),
        )
        for alias in sorted(aliases)
    ],
)
def test_classify_every_alias(alias, category):
    assert statuses.classify_bridge_status(alias) == category


@pytest.mark.parametrize("raw", [None, "", "weird", 42])
def test_unknown_status(raw):
    assert statuses.classify_bridge_status(raw) == "unknown"


def test_status_is_normalized():
    assert statuses.classify_bridge_status("  RUNNING  ") == "running"


@pytest.mark.parametrize(
    "name",
    ["COMPLETED_STATUSES", "FAILED_STATUSES", "CANCELLED_STATUSES", "RUNNING_STATUSES", "QUEUED_STATUSES"],
)
def test_waiter_reexports_same_set(name):
    assert getattr(waiter, name) is getattr(statuses, name)
