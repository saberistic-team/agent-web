"""CRM repository exports."""

from app.repositories.postgres import (
    PostgresActivityRepository,
    PostgresAdminUserRepository,
    PostgresCompanyRepository,
    PostgresContactRepository,
    PostgresSourceRecordRepository,
    default_repositories,
)
from app.repositories.protocols import (
    ActivityRepository,
    AdminUserRepository,
    CompanyRepository,
    ContactRepository,
    SourceRecordRepository,
)

__all__ = [
    "ActivityRepository",
    "AdminUserRepository",
    "CompanyRepository",
    "ContactRepository",
    "SourceRecordRepository",
    "PostgresActivityRepository",
    "PostgresAdminUserRepository",
    "PostgresCompanyRepository",
    "PostgresContactRepository",
    "PostgresSourceRecordRepository",
    "default_repositories",
]
