"""Unit tests for funding-headline company name extraction."""

from __future__ import annotations

import pytest

from app.discovery.adapters.rss import extract_company_from_funding_title


@pytest.mark.unit
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Serval raises $47M to bring AI agents to IT service management", "Serval"),
        ("Meridian raises $17 million to remake the agentic spreadsheet", "Meridian"),
        ("Exclusive: ClearJet raises $25M to build the 'Uber of Cargo'", "ClearJet"),
        ("AI chip maker SambaNova raises $1B at $11B valuation", "SambaNova"),
        ("Airbnb-backed WeRoad raises $58M to take its group travel platform to the US", "WeRoad"),
        ("Sam Altman-backed Coco Robotics raises $80M", "Coco Robotics"),
        ("Conifer locks down $20M seed round for its electric hub motor", "Conifer"),
        ("Capsule captures $12M to build the next version of its AI video editor", "Capsule"),
        ("Convective Capital raises an $85 million fund to build disaster resilience", "Convective Capital"),
        ("Zipline charts drone delivery expansion with $600M in new funding", None),
        ("How 2 UC Berkeley dropouts raised $28M for their AI marketing startup", None),
        ("Kerry Washington invests in wedding marketplace Cheersy", None),
        ("The perfect pitch: This NEA partner says every founder should answer these 5 questions", None),
        ("Sector Snapshot: Fitness Startup Funding Is Rebounding", None),
        ("", None),
    ],
)
def test_extract_company_from_funding_title(title: str, expected: str | None) -> None:
    assert extract_company_from_funding_title(title) == expected
