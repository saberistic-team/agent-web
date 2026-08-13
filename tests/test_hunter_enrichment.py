"""Tests for Hunter.io contact enrichment client and CRM service wiring."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import httpx
import pytest

from app.crm_service import CrmService
from app.hunter_enrichment import (
    HunterContact,
    HunterError,
    fetch_domain_contacts,
    parse_domain_search,
)

FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "enrichment" / "hunter-domain-search.json").read_bytes()
)
COMPANY_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddd01")


@pytest.mark.unit
def test_parse_domain_search_maps_contacts() -> None:
    contacts = parse_domain_search(FIXTURE)
    assert len(contacts) == 3
    first = contacts[0]
    assert first.email == "maya.chen@ledgerflow.example"  # lowercased
    assert first.full_name == "Maya Chen"
    assert first.position == "Co-Founder & CEO"
    assert first.confidence == 96
    assert first.source_urls == ("https://ledgerflow.example/about",)
    generic = contacts[1]
    assert generic.full_name == "info"  # local-part fallback
    assert generic.position is None
    assert generic.confidence == 72


@pytest.mark.unit
def test_parse_domain_search_rejects_bad_payloads() -> None:
    with pytest.raises(HunterError):
        parse_domain_search({})
    with pytest.raises(HunterError):
        parse_domain_search({"data": {"emails": "nope"}})
    assert parse_domain_search({"data": {"emails": [{"value": ""}, "junk", {}]}}) == []


def _mock_client(status: int, payload: object) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return httpx.Response(status, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.unit
def test_fetch_domain_contacts_success() -> None:
    contacts = fetch_domain_contacts(
        "ledgerflow.example",
        api_key="test-key",
        client=_mock_client(200, FIXTURE),
    )
    assert [c.email for c in contacts] == [
        "maya.chen@ledgerflow.example",
        "info@ledgerflow.example",
        "maya.chen@ledgerflow.example",  # case-variant dupe; service dedups
    ]


@pytest.mark.unit
def test_fetch_domain_contacts_error_mapping() -> None:
    with pytest.raises(HunterError, match="rejected"):
        fetch_domain_contacts("x.example", api_key="bad", client=_mock_client(401, {}))
    with pytest.raises(HunterError, match="rate limit"):
        fetch_domain_contacts("x.example", api_key="k", client=_mock_client(429, {}))
    assert fetch_domain_contacts("x.example", api_key="k", client=_mock_client(404, {})) == []
    with pytest.raises(HunterError, match="HTTP 500"):
        fetch_domain_contacts("x.example", api_key="k", client=_mock_client(500, {}))
    with pytest.raises(HunterError, match="not valid JSON"):
        fetch_domain_contacts("x.example", api_key="k", client=_mock_client(200, b"<html>"))


def _crm_with_company(company: dict | None) -> CrmService:
    repos = MagicMock()
    repos.companies.get_by_id.return_value = company
    return CrmService(repos=repos)


def _company(**overrides) -> dict:
    base = {
        "id": str(COMPANY_ID),
        "name": "LedgerFlow",
        "domain": "ledgerflow.example",
        "website": "https://ledgerflow.example",
    }
    base.update(overrides)
    return base


@pytest.mark.unit
@pytest.mark.integration
def test_enrich_company_contacts_creates_and_skips() -> None:
    crm = _crm_with_company(_company())
    existing = {"id": str(uuid4()), "email": "info@ledgerflow.example"}
    crm._repos.contacts.get_active_by_email.side_effect = (
        lambda conn, email: existing if email == "info@ledgerflow.example" else None
    )
    crm._repos.contacts.create.side_effect = lambda conn, **kwargs: {
        "id": uuid4(),
        **kwargs,
    }
    contacts = [
        HunterContact(
            email="maya.chen@ledgerflow.example",
            full_name="Maya Chen",
            position="Co-Founder & CEO",
            confidence=96,
            source_urls=("https://ledgerflow.example/about",),
        ),
        HunterContact(email="info@ledgerflow.example", full_name="info"),
        HunterContact(email="maya.chen@ledgerflow.example", full_name="Maya Chen"),
    ]
    actor = MagicMock()
    with (
        patch("app.crm_service.record_contact_create") as rec_create,
        patch("app.crm_service.audit_service") as audit,
    ):
        outcome = crm.enrich_company_contacts(
            MagicMock(),
            COMPANY_ID,
            actor_context=actor,
            api_key="k",
            fetcher=lambda domain, api_key: contacts,
        )
    assert outcome is not None
    assert outcome["found"] == 3
    assert outcome["domain"] == "ledgerflow.example"
    assert len(outcome["created"]) == 1  # in-batch duplicate skipped too
    assert outcome["skipped"] == ["info@ledgerflow.example"]
    created_kwargs = crm._repos.contacts.create.call_args.kwargs
    assert created_kwargs["email"] == "maya.chen@ledgerflow.example"
    assert created_kwargs["company_id"] == COMPANY_ID
    assert created_kwargs["email_permission"] == "inferred"
    assert "Hunter.io domain search (ledgerflow.example)" in created_kwargs["notes"]
    assert rec_create.call_count == 1
    audit.record_enrichment_contacts.assert_called_once()
    summary = audit.record_enrichment_contacts.call_args.kwargs["summary_after"]
    assert summary["domain"] == "ledgerflow.example"
    assert summary["found"] == 3
    assert len(summary["created_contact_ids"]) == 1
    assert summary["skipped_existing_count"] == 1
    assert "skipped_existing_emails" not in summary  # emails never enter audit


@pytest.mark.unit
@pytest.mark.integration
def test_enrich_company_contacts_resolves_domain_from_website() -> None:
    crm = _crm_with_company(_company(domain=None, website="https://ledgerflow.example/about"))
    crm._repos.contacts.get_active_by_email.return_value = None
    seen_domains: list[str] = []
    with (
        patch("app.crm_service.record_contact_create"),
        patch("app.crm_service.audit_service"),
    ):
        outcome = crm.enrich_company_contacts(
            MagicMock(),
            COMPANY_ID,
            actor_context=MagicMock(),
            api_key="k",
            fetcher=lambda domain, api_key: seen_domains.append(domain) or [],
        )
    assert outcome is not None
    assert seen_domains == ["ledgerflow.example"]


@pytest.mark.unit
@pytest.mark.integration
def test_enrich_company_contacts_missing_company_and_domain() -> None:
    crm = _crm_with_company(None)
    assert (
        crm.enrich_company_contacts(
            MagicMock(), COMPANY_ID, actor_context=MagicMock(), api_key="k"
        )
        is None
    )
    crm = _crm_with_company(_company(domain=None, website=None))
    with pytest.raises(ValueError, match="no domain or website"):
        crm.enrich_company_contacts(
            MagicMock(), COMPANY_ID, actor_context=MagicMock(), api_key="k"
        )
