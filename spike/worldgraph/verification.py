"""Claim and verification prototypes — trust levels remain separate."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

TrustLevel = Literal[
    "source_observation",
    "creator_claimed",
    "domain_verified",
    "github_verified",
    "email_domain_verified",
    "saberistic_verified",
]

# Ordered from lowest to highest operational trust (not all are equivalent evidentiary strength).
TRUST_ORDER: tuple[TrustLevel, ...] = (
    "source_observation",
    "creator_claimed",
    "email_domain_verified",
    "github_verified",
    "domain_verified",
    "saberistic_verified",
)


@dataclass(frozen=True)
class ClaimChallenge:
    method: str
    instructions: str
    expected_token: str
    trust_level: TrustLevel


@dataclass(frozen=True)
class ClaimVerificationResult:
    method: str
    verified: bool
    trust_level: TrustLevel
    details: str


def issue_domain_well_known_challenge(domain: str, world_id: str) -> ClaimChallenge:
    token = hashlib.sha256(f"{domain}:{world_id}".encode()).hexdigest()[:24]
    return ClaimChallenge(
        method="domain_well_known",
        instructions=f"Publish token at https://{domain}/.well-known/worldgraph-verification.txt",
        expected_token=token,
        trust_level="domain_verified",
    )


def verify_domain_well_known(content: str, *, expected_token: str) -> ClaimVerificationResult:
    verified = expected_token in content
    return ClaimVerificationResult(
        method="domain_well_known",
        verified=verified,
        trust_level="domain_verified",
        details="matched well-known token" if verified else "token missing",
    )


def issue_dns_txt_challenge(domain: str, world_id: str) -> ClaimChallenge:
    token = hashlib.sha256(f"dns:{domain}:{world_id}".encode()).hexdigest()[:32]
    return ClaimChallenge(
        method="dns_txt",
        instructions=f"Add TXT record worldgraph-verification={token} on {domain}",
        expected_token=token,
        trust_level="domain_verified",
    )


def verify_dns_txt(records: list[str], *, expected_token: str) -> ClaimVerificationResult:
    pattern = re.compile(rf"worldgraph-verification={re.escape(expected_token)}")
    verified = any(pattern.search(record) for record in records)
    return ClaimVerificationResult(
        method="dns_txt",
        verified=verified,
        trust_level="domain_verified",
        details="TXT record matched" if verified else "TXT record missing",
    )


def verify_github_repo(
    *,
    repo_url: str,
    authenticated_login: str | None,
    repo_owner: str,
) -> ClaimVerificationResult:
    parsed = urlparse(repo_url)
    verified = authenticated_login is not None and authenticated_login.lower() == repo_owner.lower()
    return ClaimVerificationResult(
        method="github_repo",
        verified=verified,
        trust_level="github_verified",
        details="GitHub OAuth matched repo owner" if verified else "GitHub ownership not proven",
    )


def verify_email_domain_magic_link(
    *,
    email: str,
    world_domain: str,
    token_valid: bool,
) -> ClaimVerificationResult:
    email_domain = email.split("@", 1)[-1].lower()
    domain_match = email_domain == world_domain.lower()
    verified = domain_match and token_valid
    return ClaimVerificationResult(
        method="email_domain_magic_link",
        verified=verified,
        trust_level="email_domain_verified",
        details="email domain matched and token valid" if verified else "fallback claim not verified",
    )


def separate_trust_concepts(
    *,
    creator_claim: bool,
    domain_verified: bool,
    source_observed: bool,
    saberistic_verified: bool,
) -> dict[str, bool]:
    """Explicit separation required by spike acceptance criteria."""
    return {
        "creator_claim_active": creator_claim,
        "domain_control_proven": domain_verified,
        "source_observation_recorded": source_observed,
        "saberistic_verification": saberistic_verified,
    }
