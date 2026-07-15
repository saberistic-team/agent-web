"""Verification claim prototypes for the WorldGraph spike."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from app.worldgraph_spike.manifest_v0 import TrustLevel


class ClaimMethod(str, Enum):
    WELL_KNOWN_FILE = "well_known_file"
    DNS_TXT = "dns_txt"
    GITHUB_REPO = "github_repo"
    EMAIL_MAGIC_LINK = "email_magic_link"


class VerificationStatus(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    EXPIRED = "expired"
    FAILED = "failed"


@dataclass
class ClaimChallenge:
    world_slug: str
    method: ClaimMethod
    trust_level: TrustLevel
    challenge_token: str
    instructions: str
    expires_at: datetime


@dataclass
class ClaimVerificationResult:
    status: VerificationStatus
    trust_level: TrustLevel
    method: ClaimMethod
    detail: str


def trust_level_for_method(method: ClaimMethod) -> TrustLevel:
    mapping = {
        ClaimMethod.WELL_KNOWN_FILE: TrustLevel.DOMAIN_CONTROL,
        ClaimMethod.DNS_TXT: TrustLevel.DOMAIN_CONTROL,
        ClaimMethod.GITHUB_REPO: TrustLevel.PLATFORM_OWNERSHIP,
        ClaimMethod.EMAIL_MAGIC_LINK: TrustLevel.EMAIL_DOMAIN,
    }
    return mapping[method]


def issue_challenge(
    *,
    world_slug: str,
    method: ClaimMethod,
    domain: str | None = None,
    github_repo: str | None = None,
    email: str | None = None,
    ttl_hours: int = 72,
) -> ClaimChallenge:
    token = secrets.token_urlsafe(24)
    trust = trust_level_for_method(method)
    if method == ClaimMethod.WELL_KNOWN_FILE:
        instructions = (
            f"Place token at https://{domain}/.well-known/worldgraph-challenge.txt"
        )
    elif method == ClaimMethod.DNS_TXT:
        instructions = f"Publish DNS TXT record worldgraph-challenge={token} on {domain}"
    elif method == ClaimMethod.GITHUB_REPO:
        instructions = (
            f"Create file .well-known/worldgraph-challenge.txt in {github_repo} "
            f"containing token {token}"
        )
    else:
        instructions = f"Confirm magic link sent to {email} within {ttl_hours}h"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    return ClaimChallenge(
        world_slug=world_slug,
        method=method,
        trust_level=trust,
        challenge_token=token,
        instructions=instructions,
        expires_at=expires_at,
    )


def verify_well_known_file(*, fetched_body: str, expected_token: str) -> ClaimVerificationResult:
    if expected_token.strip() == fetched_body.strip():
        return ClaimVerificationResult(
            status=VerificationStatus.VERIFIED,
            trust_level=TrustLevel.DOMAIN_CONTROL,
            method=ClaimMethod.WELL_KNOWN_FILE,
            detail="well-known challenge token matched",
        )
    return ClaimVerificationResult(
        status=VerificationStatus.FAILED,
        trust_level=TrustLevel.DOMAIN_CONTROL,
        method=ClaimMethod.WELL_KNOWN_FILE,
        detail="token mismatch",
    )


def verify_github_repo(
    *,
    repo_owner: str,
    repo_name: str,
    authenticated_login: str,
    collaborator_confirmed: bool,
) -> ClaimVerificationResult:
    if repo_owner.lower() != authenticated_login.lower() and not collaborator_confirmed:
        return ClaimVerificationResult(
            status=VerificationStatus.FAILED,
            trust_level=TrustLevel.PLATFORM_OWNERSHIP,
            method=ClaimMethod.GITHUB_REPO,
            detail="authenticated user does not control repository",
        )
    fingerprint = hashlib.sha256(f"{repo_owner}/{repo_name}".encode()).hexdigest()[:12]
    return ClaimVerificationResult(
        status=VerificationStatus.VERIFIED,
        trust_level=TrustLevel.PLATFORM_OWNERSHIP,
        method=ClaimMethod.GITHUB_REPO,
        detail=f"github ownership confirmed ({fingerprint})",
    )


def verify_email_magic_link(*, token_match: bool, domain_matches_creator: bool) -> ClaimVerificationResult:
    if not token_match:
        return ClaimVerificationResult(
            status=VerificationStatus.FAILED,
            trust_level=TrustLevel.EMAIL_DOMAIN,
            method=ClaimMethod.EMAIL_MAGIC_LINK,
            detail="invalid or expired magic link",
        )
    detail = "email domain confirmed" if domain_matches_creator else "email confirmed (lower trust)"
    return ClaimVerificationResult(
        status=VerificationStatus.VERIFIED,
        trust_level=TrustLevel.EMAIL_DOMAIN,
        method=ClaimMethod.EMAIL_MAGIC_LINK,
        detail=detail,
    )
