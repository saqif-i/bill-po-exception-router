"""Reading every bill in a poll. Pure: Xero is a fake that orders, filters and
pages like the Invoices endpoint."""

from __future__ import annotations

import pytest

from policy_service.domain.normalisation import xero_timestamp
from policy_service.domain.poller import walk_pages

PAGE = 50
BASE = 1789430400000
STEP = 10_000  # ten seconds between bills


def _stamp(millis: int) -> str:
    return f"/Date({millis}+0000)/"


class FakeXero:
    """Draft bills, oldest first, filtered by If-Modified-Since and paged.

    `strict` makes "since" exclusive. Xero's exact comparison is not documented,
    so the walk must be correct either way.
    """

    def __init__(self, count: int, *, strict: bool = False, step: int = STEP) -> None:
        self.bills = [
            {"InvoiceID": f"b{i}", "UpdatedDateUTC": _stamp(BASE + i * step)} for i in range(count)
        ]
        self.strict = strict
        self.calls = 0
        self.before_call = None

    def __call__(self, since, page: int) -> list[dict]:
        self.calls += 1
        if self.before_call:
            self.before_call(self.calls)
        rows = sorted(self.bills, key=lambda b: xero_timestamp(b["UpdatedDateUTC"]))
        if since is not None:
            rows = [
                b
                for b in rows
                if (xero_timestamp(b["UpdatedDateUTC"]) > since)
                or (not self.strict and xero_timestamp(b["UpdatedDateUTC"]) == since)
            ]
        # Copies, as a real response is: an edit must not rewrite what was read.
        return [dict(b) for b in rows[(page - 1) * PAGE : page * PAGE]]

    def edit(self, invoice_id: str, millis: int) -> None:
        for bill in self.bills:
            if bill["InvoiceID"] == invoice_id:
                bill["UpdatedDateUTC"] = _stamp(millis)


def _walk(xero: FakeXero, max_pages: int = 20):
    handled: list[dict] = []
    walk = walk_pages(xero, handled.append, since=None, page_size=PAGE, max_pages=max_pages)
    return walk, handled


@pytest.mark.parametrize("strict", [False, True])
def test_every_bill_is_read_until_a_short_query(strict):
    walk, handled = _walk(FakeXero(107, strict=strict))
    assert sorted(b["InvoiceID"] for b in handled) == sorted(f"b{i}" for i in range(107))
    assert walk.truncated is False


def test_an_exactly_full_last_page_needs_one_more_read():
    walk, handled = _walk(FakeXero(50))
    assert len(handled) == 50
    assert walk.truncated is False


@pytest.mark.parametrize("strict", [False, True])
def test_a_bill_edited_mid_poll_does_not_make_the_next_one_disappear(strict):
    """With page numbers, the edited bill jumped to the end, every later bill
    moved back one, and page 2 started at b51, skipping b50."""
    xero = FakeXero(120, strict=strict)
    xero.before_call = lambda n: xero.edit("b0", BASE + 1000 * STEP) if n == 2 else None
    walk, handled = _walk(xero)
    assert {b["InvoiceID"] for b in handled} == {f"b{i}" for i in range(120)}
    assert walk.truncated is False


def test_the_edited_version_of_a_bill_is_handled_as_well():
    xero = FakeXero(120)
    xero.before_call = lambda n: xero.edit("b0", BASE + 1000 * STEP) if n == 2 else None
    _, handled = _walk(xero)
    versions = [b["UpdatedDateUTC"] for b in handled if b["InvoiceID"] == "b0"]
    assert versions == [_stamp(BASE), _stamp(BASE + 1000 * STEP)]


def test_more_than_a_page_sharing_one_timestamp_still_makes_progress():
    walk, handled = _walk(FakeXero(120, step=0))
    assert len({b["InvoiceID"] for b in handled}) == 120
    assert walk.truncated is False


def test_hitting_the_request_cap_is_reported_with_the_last_bill_handled():
    walk, handled = _walk(FakeXero(500), max_pages=2)
    assert walk.truncated is True
    assert walk.last_updated == xero_timestamp(handled[-1]["UpdatedDateUTC"])


def test_an_empty_poll_is_complete():
    walk, handled = _walk(FakeXero(0))
    assert handled == []
    assert walk.truncated is False
    assert walk.last_updated is None
