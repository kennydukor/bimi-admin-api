"""
Response models — the wire contract.

These mirror `types/admin.ts` field-for-field (snake_case, same shapes) so the
Next.js `server/*` clients need zero changes when they flip off the mock layer.
Keep this file and types/admin.ts in lockstep.
"""
from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel

T = TypeVar("T")

UserRole = Literal["regular_admin", "super_admin"]
UserStatus = Literal["active", "suspended", "pending_verification"]
Frequency = Literal["monthly", "quarterly", "annual"]
RowColumnType = Literal["id", "select", "currency", "text", "number", "datetime"]


# ── Users & auth ─────────────────────────────────────────────
class User(BaseModel):
    id: str
    email: str
    full_name: str
    role: UserRole
    status: UserStatus
    created_at: str
    last_login_at: str | None
    must_change_password: bool = False


class Actor(BaseModel):
    id: str
    full_name: str
    email: str


class Session(BaseModel):
    user: User
    expires_at: str
    # The session token, also set as an httpOnly cookie. Returned in the body so
    # cross-origin SPAs (where third-party cookies are blocked) can send it as an
    # Authorization: Bearer header instead. Store it in memory; treat like a
    # credential.
    access_token: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class InviteUserRequest(BaseModel):
    email: str
    full_name: str
    role: UserRole


class SetRoleRequest(BaseModel):
    role: UserRole


class EmailRequest(BaseModel):
    email: str


class ResetConfirmRequest(BaseModel):
    token: str
    password: str


class TokenRequest(BaseModel):
    token: str


# ── Tables ───────────────────────────────────────────────────
class RowColumnDef(BaseModel):
    key: str
    label: str
    type: RowColumnType
    options: list[str] | None = None
    nullable: bool | None = None
    # For FK columns: [{value: code, label: friendly_name}] for the edit dropdown.
    fk_options: list[dict] | None = None
    read_only: bool | None = None

    model_config = {"populate_by_name": True}

    def model_dump(self, **kw):  # emit camelCase readOnly to match TS
        data = super().model_dump(**kw)
        if "read_only" in data:
            data["readOnly"] = data.pop("read_only")
        if data.get("options") is None:
            data.pop("options", None)
        if data.get("nullable") is None:
            data.pop("nullable", None)
        if "fk_options" in data:
            if data["fk_options"] is None:
                data.pop("fk_options")
            else:
                data["fkOptions"] = data.pop("fk_options")
        if data.get("readOnly") is None:
            data.pop("readOnly", None)
        return data


class DatasetTable(BaseModel):
    name: str
    label: str
    category: str
    frequency: Frequency
    is_reference: bool = False
    row_count: int
    column_count: int
    last_updated_at: str | None
    required_columns: list[str]
    row_columns: list[dict]     # already-serialised RowColumnDef dicts


# ── Datasets (uploads) ───────────────────────────────────────
class Dataset(BaseModel):
    id: str
    file_name: str
    table_name: str
    frequency: Frequency
    source: str
    row_count: int
    size_bytes: int
    uploaded_by: Actor
    uploaded_at: str
    deleted_at: str | None


# ── Validation ───────────────────────────────────────────────
ValidationIssueType = Literal[
    "missing_column", "type_mismatch", "unknown_code", "duplicate_row",
    "unexpected_null", "invalid_enum", "unseen_value"
]


class ValidationIssue(BaseModel):
    type: ValidationIssueType
    row: int | None
    column: str | None
    message: str


class ValidationReport(BaseModel):
    valid: bool
    total_rows: int
    valid_rows: int
    duplicate_rows: int
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]


UploadStatus = Literal["validating", "rejected", "committing", "committed"]


class UploadResult(BaseModel):
    upload_id: str
    status: UploadStatus
    report: ValidationReport
    dataset: Dataset | None


# ── Rows ─────────────────────────────────────────────────────
class RowsResponse(BaseModel):
    table_name: str
    columns: list[dict]
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int
    # {fk_column: {code: friendly_name}} so the table can show names for codes.
    fk_labels: dict[str, dict[str, str]] | None = None


class BulkDeleteResponse(BaseModel):
    deleted: int


# ── Audit ────────────────────────────────────────────────────
AuditAction = Literal[
    "login", "upload", "edit_row", "delete_row", "bulk_delete",
    "delete_file", "restore", "add_user", "suspend_user", "remove_user",
]


class AuditLogEntry(BaseModel):
    id: str
    action: AuditAction
    actor: Actor
    target: str
    detail: str | None = None
    timestamp: str


# ── Dashboard ────────────────────────────────────────────────
class ActivityPoint(BaseModel):
    date: str
    count: int


class SourceBreakdown(BaseModel):
    source: str
    count: int


class DashboardStats(BaseModel):
    total_datasets: int
    total_columns: int
    total_rows: int
    rows_added_this_month: int
    recent_uploads: int
    upload_contributors: int
    recent_deletions: int
    total_users: int
    admin_count: int
    uploader_count: int
    own_dataset_count: int
    own_row_count: int
    login_activity: list[ActivityPoint]
    datasets_by_source: list[SourceBreakdown]
    recoverable_rows: int


# ── Transport ────────────────────────────────────────────────
class Paginated(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
