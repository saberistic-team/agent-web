"""CRM repository package."""

from app.repositories.postgres import (
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresProjectBriefRepository,
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
    ProjectBriefRepository,
    CompanyRepository,
    ContactRepository,
    SourceRecordRepository,
)

__all__ = [
    "ActivityRepository",
    "AdminUserRepository",
    "ProjectBriefRepository",
    "AuditEventRepository",
    "CompanyRepository",
    "ContactRepository",
    "SourceRecordRepository",
    "PostgresActivityRepository",
    "PostgresAdminUserRepository",
    "PostgresProjectBriefRepository",
    "PostgresAuditEventRepository",
    "PostgresCompanyRepository",
    "PostgresContactRepository",
    "PostgresRepositories",
    "PostgresSourceRecordRepository",
    "default_repositories",
    "get_repositories",
]
