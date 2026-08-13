"""Unit tests for persisting discovery run candidates into the review inbox."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.discovery.inbox_persistence import (
    candidate_category,
    candidate_confidence,
    candidate_evidence_payload,
    candidate_fingerprint,
    fingerprint_basis,
    persist_run_candidates,
)
from app.discovery.types import (
    DiscoveryCandidate,
    DiscoveryEvidence,
    DiscoveryObservation,
)


def _observation(
    value: str,
    *,
    confidence: float = 0.9,
    retrieved_at: str = "2026-08-01T00:00:00+00:00",
) -> DiscoveryObservation:
    return DiscoveryObservation(
        source_url="https://directory.example.com/companies",
        retrieved_at=retrieved_at,
        raw_source_id="stub-source",
        value=value,
        confidence=confidence,
        review_at="2026-09-01T00:00:00+00:00",
        expires_at="2026-11-01T00:00:00+00:00",
    )


def _candidate(
    *,
    external_id: str = "stub:1",
    name: str = "Nimbus Analytics",
    signals: tuple[str, ...] = ("source:stub", "category:fintech"),
    observations: tuple[DiscoveryObservation, ...] | None = (
        _observation("name=Nimbus Analytics", confidence=0.95),
        _observation("website=https://nimbus.example", confidence=0.9),
    ),
    snippet: str | None = "Payments infrastructure for platforms.",
    raw_payload: dict | None = None,
) -> DiscoveryCandidate:
    evidence = None
    if observations is not None:
        evidence = DiscoveryEvidence(observations=observations, snippet=snippet)
    return DiscoveryCandidate(
        external_id=external_id,
        name=name,
        domain="nimbus.example",
        website="https://nimbus.example",
        signals=signals,
        evidence=evidence,
        raw_payload=raw_payload,
    )


@pytest.mark.unit
def test_candidate_evidence_payload_matches_inbox_shape() -> None:
    payload = candidate_evidence_payload(_candidate())
    assert payload is not None
    assert payload["snippet"] == "Payments infrastructure for platforms."
    assert len(payload["observations"]) == 2
    observation = payload["observations"][0]
    assert set(observation) == {
        "source_url",
        "retrieved_at",
        "raw_source_id",
        "value",
        "confidence",
        "review_at",
        "expires_at",
    }
    assert observation["value"] == "name=Nimbus Analytics"


@pytest.mark.unit
def test_candidate_evidence_payload_none_without_evidence() -> None:
    assert candidate_evidence_payload(_candidate(observations=None)) is None


@pytest.mark.unit
def test_fingerprint_stable_across_retrieval_timestamps() -> None:
    first = _candidate()
    later = _candidate(
        observations=(
            _observation(
                "name=Nimbus Analytics",
                confidence=0.95,
                retrieved_at="2026-08-08T00:00:00+00:00",
            ),
            _observation(
                "website=https://nimbus.example",
                confidence=0.9,
                retrieved_at="2026-08-08T00:00:00+00:00",
            ),
        )
    )
    assert candidate_fingerprint(first) == candidate_fingerprint(later)


@pytest.mark.unit
def test_fingerprint_ignores_observation_order() -> None:
    reordered = _candidate(
        observations=(
            _observation("website=https://nimbus.example", confidence=0.9),
            _observation("name=Nimbus Analytics", confidence=0.95),
        )
    )
    assert candidate_fingerprint(_candidate()) == candidate_fingerprint(reordered)


@pytest.mark.unit
def test_fingerprint_changes_when_evidence_changes() -> None:
    changed = _candidate(
        observations=(
            _observation("name=Nimbus Analytics", confidence=0.95),
            _observation("website=https://nimbus.io", confidence=0.9),
        )
    )
    assert candidate_fingerprint(_candidate()) != candidate_fingerprint(changed)
    renamed_snippet = _candidate(snippet="New positioning.")
    assert candidate_fingerprint(_candidate()) != candidate_fingerprint(renamed_snippet)


@pytest.mark.unit
def test_fingerprint_basis_none_passthrough() -> None:
    assert fingerprint_basis(None) is None
    no_evidence = _candidate(observations=None)
    assert candidate_fingerprint(no_evidence) == candidate_fingerprint(
        _candidate(observations=None)
    )


@pytest.mark.unit
def test_candidate_category_from_signals() -> None:
    assert candidate_category(_candidate()) == "fintech"


@pytest.mark.unit
def test_candidate_category_falls_back_to_raw_payload() -> None:
    candidate = _candidate(signals=("source:stub",), raw_payload={"suggested_category": "ai_infrastructure"})
    assert candidate_category(candidate) == "ai_infrastructure"


@pytest.mark.unit
def test_candidate_category_none_when_unavailable() -> None:
    assert candidate_category(_candidate(signals=("source:stub",))) is None
    assert candidate_category(_candidate(signals=("category:",))) is None


@pytest.mark.unit
def test_candidate_confidence_uses_strongest_observation() -> None:
    assert candidate_confidence(_candidate()) == 0.95
    assert candidate_confidence(_candidate(observations=None)) is None
    assert candidate_confidence(_candidate(observations=(), snippet=None)) is None


class _FakeInboxRepo:
    def __init__(self, *, inserted: bool = True) -> None:
        self.calls: list[dict] = []
        self._inserted = inserted

    def upsert_candidate(self, conn, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return {"id": uuid4(), "inserted": self._inserted, **kwargs}


@pytest.mark.unit
def test_persist_run_candidates_upserts_each_candidate() -> None:
    repo = _FakeInboxRepo(inserted=True)
    run_id = uuid4()
    candidates = [
        _candidate(external_id="stub:1"),
        _candidate(external_id="stub:2", name="Ledgerflow"),
    ]
    summary = persist_run_candidates(
        object(),  # type: ignore[arg-type]
        run_id=run_id,
        source_id="stub",
        candidates=candidates,
        inbox_repo=repo,  # type: ignore[arg-type]
    )
    assert summary.inserted == 2
    assert summary.refreshed == 0
    assert len(repo.calls) == 2
    first = repo.calls[0]
    assert first["run_id"] == run_id
    assert first["source_id"] == "stub"
    assert first["external_id"] == "stub:1"
    assert first["category"] == "fintech"
    assert first["confidence"] == 0.95
    assert first["signals"] == ["source:stub", "category:fintech"]
    assert first["evidence"]["observations"]
    assert len(first["evidence_fingerprint"]) == 32


@pytest.mark.unit
def test_persist_run_candidates_counts_refreshed_rows() -> None:
    repo = _FakeInboxRepo(inserted=False)
    summary = persist_run_candidates(
        object(),  # type: ignore[arg-type]
        run_id=uuid4(),
        source_id="stub",
        candidates=[_candidate()],
        inbox_repo=repo,  # type: ignore[arg-type]
    )
    assert summary.inserted == 0
    assert summary.refreshed == 1


@pytest.mark.unit
def test_persist_run_candidates_empty_list() -> None:
    repo = _FakeInboxRepo()
    summary = persist_run_candidates(
        object(),  # type: ignore[arg-type]
        run_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        source_id="stub",
        candidates=[],
        inbox_repo=repo,  # type: ignore[arg-type]
    )
    assert summary.inserted == 0
    assert summary.refreshed == 0
    assert repo.calls == []
