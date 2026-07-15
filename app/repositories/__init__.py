"""CRM repository exports."""

from app.repositories.postgres import (
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresAuditEventRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresProjectBriefRepository,
    PostgresRepositories,
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
    ProjectBriefRepository,
    ResearchRecordRepository,
    SourceRecordRepository,
)

__all__ = [
    "ActivityRepository",
    "AdminUserRepository",
    "AuditEventRepository",
    "CompanyRepository",
    "ContactRepository",
    "ProjectBriefRepository",
    "ResearchRecordRepository",
    "SourceRecordRepository",
    "PostgresActivityRepository",
    "PostgresAdminUserRepository",
    "PostgresAuditEventRepository",
    "PostgresCompanyRepository",
    "PostgresContactRepository",
    "PostgresProjectBriefRepository",
    "PostgresRepositories",
    "PostgresResearchRecordRepository",
    "PostgresSourceRecordRepository",
    "default_repositories",
    "get_repositories",
]
