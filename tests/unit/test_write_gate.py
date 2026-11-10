"""Offline tests for the ADR-0003 write gate.

Covers the token maths (round-trip, expiry, tampering, wrong secret), the two-phase
dry-run/apply flow, the ``WRITE_DISABLED`` opt-in and the idempotent store ledger.
No provider is contacted: ``execute`` is a local callable.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from lv24_seo.core.errors import CONFIRM_REQUIRED, INVALID_INPUT, WRITE_DISABLED, SeoError
from lv24_seo.core.write_gate import (
    WriteGate,
    WritePlan,
    make_token,
    plan_hash,
    verify_token,
)

SECRET = "unit-test-write-secret"
URLS = ["https://example.com/a", "https://example.com/b"]

PLAN = WritePlan(
    action="indexnow",
    target="example.com",
    summary="submit 2 URL(s) to IndexNow for example.com",
    payload={"host": "example.com", "key": "abc123", "urls": list(URLS)},
)


class FakeLedger:
    """Minimal ``record_send`` ledger: the first claim wins, repeats return ``False``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._slots: set[tuple[str, str, str, str, str]] = set()

    def record_send(
        self,
        kind: str,
        property_id: str,
        period: str,
        content_hash: str,
        channel: str,
    ) -> bool:
        self.calls.append(
            {
                "kind": kind,
                "property_id": property_id,
                "period": period,
                "content_hash": content_hash,
                "channel": channel,
            }
        )
        slot = (kind, property_id, period, content_hash, channel)
        if slot in self._slots:
            return False
        self._slots.add(slot)
        return True


def _gate(*, allow_write: bool = True, store: object | None = None) -> WriteGate:
    return WriteGate(allow_write=allow_write, secret=SECRET, store=store)


# -- plan hash -------------------------------------------------------------------------------


def test_plan_hash_is_canonical_and_covers_payload_and_summary() -> None:
    reordered = WritePlan(
        action=PLAN.action,
        target=PLAN.target,
        summary=PLAN.summary,
        payload={"urls": list(URLS), "key": "abc123", "host": "example.com"},
    )
    changed_urls = replace(
        PLAN,
        payload={"host": "example.com", "key": "abc123", "urls": [URLS[0]]},
    )

    assert plan_hash(PLAN) == plan_hash(reordered)
    assert plan_hash(PLAN) != plan_hash(changed_urls)
    assert plan_hash(PLAN) != plan_hash(replace(PLAN, summary="different summary"))
    assert plan_hash(PLAN) != plan_hash(replace(PLAN, target="other.example"))


# -- tokens ----------------------------------------------------------------------------------


def test_token_round_trip_reports_expiry_boundary() -> None:
    token = make_token(PLAN, SECRET, ttl_seconds=60, now=1000)

    assert token.split(":", 1)[0] == "1060"
    assert verify_token(PLAN, token, SECRET, now=1000) is True
    assert verify_token(PLAN, token, SECRET, now=1059) is True
    assert verify_token(PLAN, token, SECRET, now=1060) is False
    assert SECRET not in token


def test_expired_token_is_rejected() -> None:
    token = make_token(PLAN, SECRET, ttl_seconds=5, now=2000)

    assert verify_token(PLAN, token, SECRET, now=2006) is False


def test_changed_plan_invalidates_the_token() -> None:
    token = make_token(PLAN, SECRET, now=1000)
    changed = replace(PLAN, payload={"host": "example.com", "key": "abc123", "urls": [URLS[0]]})

    assert verify_token(changed, token, SECRET, now=1000) is False
    assert verify_token(replace(PLAN, target="evil.example"), token, SECRET, now=1000) is False


def test_wrong_secret_is_rejected() -> None:
    token = make_token(PLAN, SECRET, now=1000)

    assert verify_token(PLAN, token, "another-secret", now=1000) is False


@pytest.mark.parametrize(
    "token",
    ["", "not-a-token", "1000:", "1000:zzzz", "1000:" + "0" * 63, "1000:" + "f" * 65],
)
def test_malformed_tokens_are_rejected(token: str) -> None:
    assert verify_token(PLAN, token, SECRET, now=1000) is False


def test_empty_secret_disables_token_creation_and_verification() -> None:
    with pytest.raises(SeoError) as caught:
        make_token(PLAN, "   ")

    assert caught.value.code == WRITE_DISABLED
    assert "LV24_SEO_WRITE_SECRET" in caught.value.message

    with pytest.raises(SeoError) as caught:
        verify_token(PLAN, "1000:" + "0" * 64, "")

    assert caught.value.code == WRITE_DISABLED


def test_non_positive_ttl_is_invalid_input() -> None:
    with pytest.raises(SeoError) as caught:
        make_token(PLAN, SECRET, ttl_seconds=0)

    assert caught.value.code == INVALID_INPUT


def test_gate_repr_never_contains_the_secret() -> None:
    gate = _gate()

    assert SECRET not in repr(gate)
    assert "allow_write=True" in repr(gate)
    assert "store=none" in repr(gate)


# -- plan / dry run --------------------------------------------------------------------------


def test_plan_requires_an_explicit_opt_in() -> None:
    gate = _gate(allow_write=False)

    with pytest.raises(SeoError) as caught:
        gate.plan(PLAN)

    assert caught.value.code == WRITE_DISABLED
    assert "--allow-write" in (caught.value.next_action or "")


def test_plan_rejects_an_unknown_action() -> None:
    with pytest.raises(SeoError) as caught:
        _gate().plan(replace(PLAN, action="delete_everything"))

    assert caught.value.code == INVALID_INPUT


def test_plan_rejects_an_empty_target() -> None:
    with pytest.raises(SeoError) as caught:
        _gate().plan(replace(PLAN, target="   "))

    assert caught.value.code == INVALID_INPUT


def test_dry_run_returns_the_plan_and_a_token() -> None:
    result = _gate().plan(PLAN)

    assert result.status == "dry_run"
    assert result.plan == PLAN
    assert result.confirm_token
    assert result.reason == ""
    assert result.detail["plan_hash"] == plan_hash(PLAN)
    assert result.detail["ttl_seconds"] == 300
    assert plan_hash(PLAN) not in result.confirm_token


# -- apply -----------------------------------------------------------------------------------


def test_apply_verifies_the_token_and_runs_execute_once() -> None:
    gate = _gate()
    token = gate.plan(PLAN).confirm_token
    seen: list[WritePlan] = []

    def execute(plan: WritePlan) -> dict[str, Any]:
        seen.append(plan)
        return {"queued": len(plan.payload["urls"])}

    result = gate.apply(PLAN, token, execute=execute)

    assert result.status == "applied"
    assert result.plan == PLAN
    assert result.detail == {"result": {"queued": 2}}
    assert seen == [PLAN]


def test_apply_without_execute_still_records_an_applied_result() -> None:
    gate = _gate()
    token = gate.plan(PLAN).confirm_token

    result = gate.apply(PLAN, token)

    assert result.status == "applied"
    assert result.detail == {}


def test_apply_with_a_wrong_token_requires_confirmation() -> None:
    gate = _gate()
    gate.plan(PLAN)
    called: list[WritePlan] = []

    with pytest.raises(SeoError) as caught:
        gate.apply(PLAN, "wrong-token", execute=called.append)

    assert caught.value.code == CONFIRM_REQUIRED
    assert "wrong-token" not in str(caught.value)
    assert called == []


def test_apply_with_a_token_for_a_changed_plan_requires_confirmation() -> None:
    gate = _gate()
    token = gate.plan(PLAN).confirm_token
    changed = replace(PLAN, payload={"host": "example.com", "key": "abc123", "urls": [URLS[0]]})

    with pytest.raises(SeoError) as caught:
        gate.apply(changed, token)

    assert caught.value.code == CONFIRM_REQUIRED


def test_apply_with_an_expired_token_requires_confirmation() -> None:
    gate = _gate()
    expired = make_token(PLAN, SECRET, ttl_seconds=1, now=1000)

    with pytest.raises(SeoError) as caught:
        gate.apply(PLAN, expired)

    assert caught.value.code == CONFIRM_REQUIRED


def test_apply_without_allow_write_is_refused_even_with_a_valid_token() -> None:
    token = make_token(PLAN, SECRET)

    with pytest.raises(SeoError) as caught:
        _gate(allow_write=False).apply(PLAN, token)

    assert caught.value.code == WRITE_DISABLED


def test_apply_with_a_missing_token_requires_confirmation() -> None:
    with pytest.raises(SeoError) as caught:
        _gate().apply(PLAN, "")

    assert caught.value.code == CONFIRM_REQUIRED


# -- idempotency ledger ----------------------------------------------------------------------


def test_duplicate_apply_is_skipped_without_calling_execute_again() -> None:
    ledger = FakeLedger()
    gate = _gate(store=ledger)
    token = gate.plan(PLAN).confirm_token
    calls: list[WritePlan] = []

    def execute(plan: WritePlan) -> dict[str, Any]:
        calls.append(plan)
        return {"queued": 2}

    first = gate.apply(PLAN, token, idempotency_key="job-1", execute=execute)
    second = gate.apply(PLAN, token, idempotency_key="job-1", execute=execute)

    assert first.status == "applied"
    assert second.status == "skipped"
    assert second.reason == "duplicate"
    assert calls == [PLAN]
    assert len(ledger.calls) == 2
    assert ledger.calls[0] == {
        "kind": "indexnow",
        "property_id": "example.com",
        "period": "job-1",
        "content_hash": plan_hash(PLAN),
        "channel": "write",
    }


def test_default_idempotency_period_is_the_plan_hash() -> None:
    ledger = FakeLedger()
    gate = _gate(store=ledger)
    token = gate.plan(PLAN).confirm_token

    gate.apply(PLAN, token)

    assert ledger.calls[0]["period"] == plan_hash(PLAN)


def test_store_without_record_send_is_invalid_input() -> None:
    gate = _gate(store=object())
    token = gate.plan(PLAN).confirm_token

    with pytest.raises(SeoError) as caught:
        gate.apply(PLAN, token)

    assert caught.value.code == INVALID_INPUT


# -- rendering -------------------------------------------------------------------------------


def test_gate_result_to_dict_hides_empty_optional_fields() -> None:
    dry_run = _gate().plan(PLAN).to_dict()

    assert dry_run["status"] == "dry_run"
    assert dry_run["plan"]["action"] == "indexnow"
    assert ":" in dry_run["confirm_token"]
    assert len(dry_run["confirm_token"].rsplit(":", 1)[1]) == 64
    assert "reason" not in dry_run
    assert "detail" in dry_run
