"""CRM repository package."""

from app.repositories.postgres import (
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresAuditEventRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresResearchRecordRepository,
    PostgresSourceRecordRepository,
    default_repositories,
    get_repositories,
)
from app.repositories.protocols import (
    ActivityRepository,
    AdminUserRepository,
    AuditEventRepository,
    CompanyRepository,
    ContactRepository,
    ResearchRecordRepository,
    SourceRecordRepository,
)

__all__ = [
    "ActivityRepository",
    "AdminUserRepository",
    "AuditEventRepository",
    "CompanyRepository",
    "ContactRepository",
    "ResearchRecordRepository",
    "SourceRecordRepository",
    "PostgresActivityRepository",
    "PostgresAdminUserRepository",
    "PostgresAuditEventRepository",
    "PostgresCompanyRepository",
    "PostgresContactRepository",
    "PostgresResearchRecordRepository",
    "PostgresSourceRecordRepository",
    "default_repositories",
    "get_repositories",
]
