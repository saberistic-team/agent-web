"""CRM repository package."""

from app.repositories.postgres import (
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresProjectBriefRepository,
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
    ProjectBriefRepository,
    CompanyRepository,
    ContactRepository,
    ProjectBriefRepository,
    ResearchRecordRepository,
    SourceRecordRepository,
)

__all__ = [
    "ActivityRepository",
    "AdminUserRepository",
    "ProjectBriefRepository",
    "AuditEventRepository",
    "CompanyRepository",
    "ContactRepository",
    "ProjectBriefRepository",
    "ResearchRecordRepository",
    "SourceRecordRepository",
    "PostgresActivityRepository",
    "PostgresAdminUserRepository",
    "PostgresProjectBriefRepository",
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
