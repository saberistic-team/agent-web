"""CRM repository exports."""

from app.repositories.postgres import (
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresAuditEventRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresRepositories,
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
    SourceRecordRepository,
)

__all__ = [
    "ActivityRepository",
    "AdminUserRepository",
    "AuditEventRepository",
    "CompanyRepository",
    "ContactRepository",
    "SourceRecordRepository",
    "PostgresActivityRepository",
    "PostgresAdminUserRepository",
    "PostgresAuditEventRepository",
    "PostgresCompanyRepository",
    "PostgresContactRepository",
    "PostgresRepositories",
    "PostgresSourceRecordRepository",
    "default_repositories",
    "get_repositories",
]
