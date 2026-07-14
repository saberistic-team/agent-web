"""CRM repository package."""

from app.repositories.postgres import (
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresResearchRecordRepository,
    PostgresSourceRecordRepository,
    default_repositories,
)
from app.repositories.protocols import (
    ActivityRepository,
    AdminUserRepository,
    CompanyRepository,
    ContactRepository,
    ResearchRecordRepository,
    SourceRecordRepository,
)

__all__ = [
    "ActivityRepository",
    "AdminUserRepository",
    "CompanyRepository",
    "ContactRepository",
    "ResearchRecordRepository",
    "SourceRecordRepository",
    "PostgresActivityRepository",
    "PostgresAdminUserRepository",
    "PostgresCompanyRepository",
    "PostgresContactRepository",
    "PostgresResearchRecordRepository",
    "PostgresSourceRecordRepository",
    "default_repositories",
]
