"""Reading every page of a poll. Pure: the page fetcher is a fake."""

from __future__ import annotations

from datetime import UTC, datetime

from policy_service.domain.poller import walk_pages

PAGE = 50


def _bills(start: int, count: int) -> list[dict]:
    # Ten seconds apart, oldest first, as Xero returns them.
    base = 1789430400000
    return [
        {"n": i, "UpdatedDateUTC": f"/Date({base + i * 10_000}+0000)/"}
        for i in range(start, start + count)
    ]


def _walk(pages: list[list[dict]], max_pages: int = 20):
    seen: list[int] = []
    walk = walk_pages(
        lambda page: pages[page - 1] if page <= len(pages) else [],
        lambda raw: seen.append(raw["n"]),
        page_size=PAGE,
        max_pages=max_pages,
    )
    return walk, seen


def test_every_page_is_read_until_a_short_one():
    """The first page alone used to be read, and the cursor moved past the rest."""
    walk, seen = _walk([_bills(0, 50), _bills(50, 50), _bills(100, 7)])
    assert seen == list(range(107))
    assert walk.truncated is False


def test_an_exactly_full_last_page_needs_one_more_empty_read():
    walk, seen = _walk([_bills(0, 50)])
    assert len(seen) == 50
    assert walk.truncated is False


def test_hitting_the_page_cap_is_reported_with_the_last_bill_seen():
    walk, seen = _walk([_bills(0, 50), _bills(50, 50), _bills(100, 50)], max_pages=2)
    assert len(seen) == 100
    assert walk.truncated is True
    assert walk.last_updated == datetime.fromtimestamp((1789430400000 + 99 * 10_000) / 1000, UTC)


def test_an_empty_poll_is_complete():
    walk, seen = _walk([])
    assert seen == []
    assert walk.truncated is False
    assert walk.last_updated is None
