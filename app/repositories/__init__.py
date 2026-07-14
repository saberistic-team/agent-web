"""CRM repository package."""

from app.repositories.postgres import (
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresAuditEventRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresSourceRecordRepository,
    PostgresStageHistoryRepository,
    default_repositories,
)
from app.repositories.protocols import (
    ActivityRepository,
    AdminUserRepository,
    AuditEventRepository,
    CompanyRepository,
    ContactRepository,
    SourceRecordRepository,
    StageHistoryRepository,
)

__all__ = [
    "ActivityRepository",
    "AdminUserRepository",
    "AuditEventRepository",
    "CompanyRepository",
    "ContactRepository",
    "SourceRecordRepository",
    "StageHistoryRepository",
    "PostgresActivityRepository",
    "PostgresAdminUserRepository",
    "PostgresAuditEventRepository",
    "PostgresCompanyRepository",
    "PostgresContactRepository",
    "PostgresSourceRecordRepository",
    "PostgresStageHistoryRepository",
    "default_repositories",
]
