from typing import Literal

from pydantic import BaseModel, Field


class PrincipalOut(BaseModel):
    subject: str
    display_name: str
    roles: list[str]
    capabilities: list[str]


class StatusCount(BaseModel):
    status: str
    count: int


class DimensionCount(BaseModel):
    dimension: str
    count: int


class OverviewOut(BaseModel):
    users: int
    entries: int
    pending_review: int
    conflicts: int
    tombstones: int
    db_bytes: int
    wal_bytes: int
    by_status: list[StatusCount]
    by_dimension: list[DimensionCount]
    updated_at: str | None


class ReviewQueueItem(BaseModel):
    entry_id: str
    user_source: str
    user_id: str
    dimension: str
    topic: str
    scope: str
    summary: str
    confidence: float
    status: str
    entry_version: str
    note: str | None = None


class ReviewQueueOut(BaseModel):
    items: list[ReviewQueueItem]
    total: int
    page: int
    page_size: int


class ReviewFacetValue(BaseModel):
    value: str
    count: int


class ReviewFacetsOut(BaseModel):
    dimensions: list[ReviewFacetValue]
    sources: list[ReviewFacetValue]


class ReviewEntryOut(BaseModel):
    user_source: str
    user_id: str
    dimension: str
    topic: str
    scope: str
    id: str
    confidence: float
    status: str
    body: str
    summary: str
    created_at: str
    updated_at: str
    last_reinforced_at: str
    degraded_reason: str | None = None
    superseded_by: str | None = None
    topic_new: str | None = None
    conflict_with: str | None = None
    conflict_marks: list[str] = Field(default_factory=list)
    reinforcements: int
    archived_at: str | None = None


class ReviewSourceOut(BaseModel):
    seq: int
    origin_type: str
    origin_ref: str
    quote: str
    kind: str


class ReviewHistoryOut(BaseModel):
    decision: str
    actor: str
    ts: str
    note: str | None = None
    entry_version: str


class ReviewClarificationOut(BaseModel):
    conflicting_ids: list[str]
    question: str
    issued_at: str
    ttl_days: int


class ReviewConflictOut(BaseModel):
    id: str
    dimension: str
    topic: str
    scope: str
    summary: str
    status: str


class ReviewDetailOut(BaseModel):
    entry: ReviewEntryOut
    sources: list[ReviewSourceOut]
    history: list[ReviewHistoryOut]
    clarifications: list[ReviewClarificationOut]
    conflicts: list[ReviewConflictOut]
    allowed_topics: list[str] = Field(default_factory=list)  # confirm 时可归一的目标 topic


class ReviewDecisionIn(BaseModel):
    user_source: str
    user_id: str
    scope: str
    entry_id: str
    decision: Literal["confirm", "remove", "fix"]
    entry_version: str
    note: str | None = None
    body: str | None = None
    summary: str | None = None
    topic: str | None = None  # confirm 时归一目标 topic（必须在维度白名单内）


class ReviewDecisionOut(BaseModel):
    noop: bool
    updated: int | None = None
    merged_into: str | None = None
    rekeyed_to: str | None = None
    invalid_topic: bool | None = None


class UserListItem(BaseModel):
    user_source: str
    user_id: str
    entries: int
    pending_review: int
    conflicts: int
    updated_at: str | None = None


class UserListOut(BaseModel):
    items: list[UserListItem]
    total: int
    page: int
    page_size: int


class TombstoneItem(BaseModel):
    user_source: str
    user_id: str
    deleted_at: str
    requested_by: str
    purged_at: str | None = None


class TombstoneListOut(BaseModel):
    items: list[TombstoneItem]
    total: int
    page: int
    page_size: int


class UserEntryOut(BaseModel):
    id: str
    dimension: str
    topic: str
    scope: str
    status: str
    confidence: float
    summary: str
    body: str
    created_at: str
    updated_at: str
    last_reinforced_at: str
    degraded_reason: str | None = None
    superseded_by: str | None = None
    topic_new: str | None = None
    conflict_with: str | None = None
    reinforcements: int
    archived_at: str | None = None
    sources_count: int = 0


class TombstoneInfo(BaseModel):
    deleted_at: str
    requested_by: str
    purged_at: str | None = None


class UserDetailOut(BaseModel):
    user_source: str
    user_id: str
    entries: list[UserEntryOut]
    by_dimension: list[DimensionCount]
    by_status: list[StatusCount]
    clarifications: list[ReviewClarificationOut]
    tombstone: TombstoneInfo | None = None
    entries_count: int
    pending_review: int
    conflicts: int
    first_seen: str | None = None
    updated_at: str | None = None
    last_reinforced_at: str | None = None


class AssembleEntryOut(BaseModel):
    id: str
    text: str
    confidence: float
    status: str


class AssembleClarificationOut(BaseModel):
    conflicting_ids: list[str]
    question: str


class AssembleOut(BaseModel):
    entries: list[AssembleEntryOut]
    budget_used: int
    merge_task: bool
    clarification_needed: list[AssembleClarificationOut]
    rendered: str


class DeleteUserOut(BaseModel):
    deleted: bool
    mode: str = "soft"


class MergeTaskOut(BaseModel):
    id: int
    user_source: str
    user_id: str
    taboo_count: int
    scope_filter: str | None = None
    status: Literal["open", "done", "dismissed"]
    proposal_json: str | None = None
    note: str | None = None
    created_at: str
    updated_at: str


class MergeTaskListOut(BaseModel):
    items: list[MergeTaskOut]
    total: int


class MergeTaskResolveIn(BaseModel):
    status: Literal["done", "dismissed"]
    note: str | None = None


class RestoreUserOut(BaseModel):
    restored: bool


class PurgeUserOut(BaseModel):
    purged: bool


class EntryListItem(BaseModel):
    id: str
    user_source: str
    user_id: str
    dimension: str
    topic: str
    scope: str
    summary: str
    status: str
    confidence: float
    updated_at: str
    last_reinforced_at: str
    reinforcements: int
    conflict_with: str | None = None
    sources_count: int = 0


class EntryListOut(BaseModel):
    items: list[EntryListItem]
    total: int
    page: int
    page_size: int


class EntryFacetValue(BaseModel):
    value: str
    count: int


class EntryFacetsOut(BaseModel):
    dimensions: list[EntryFacetValue]
    statuses: list[EntryFacetValue]
    sources: list[EntryFacetValue]
    scopes: list[EntryFacetValue]


class EntryEditIn(BaseModel):
    user_source: str
    user_id: str
    entry_id: str
    scope: str
    expected_version: str
    new_dimension: str
    new_topic: str
    new_scope: str
    confidence: float = Field(ge=0, le=1)
    status: Literal["pending_review", "orphan_topic", "active", "degraded", "archived"]
    summary: str
    body: str
    degraded_reason: str | None = None


class EntryEditConflict(BaseModel):
    id: str
    dimension: str
    topic: str
    scope: str
    status: str
    summary: str


class EntryEditCheckOut(BaseModel):
    ok: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    conflicts: list[EntryEditConflict] = Field(default_factory=list)


class EntryEditOut(BaseModel):
    ok: bool
    id: str | None = None
    updated_at: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ConfigWriteIn(BaseModel):
    expected_version: str
    yaml: str | None = None
    data: dict | None = None


class ConfigValidateIn(BaseModel):
    yaml: str | None = None
    data: dict | None = None


class ConfigBackupOut(BaseModel):
    id: str
    name: str
    size: int
    updated_at: str
    version: str
    kind: str


class ConfigWriteOut(BaseModel):
    ok: bool
    version: str
    applied: list[str] = []
    restart_required: list[str] = []
    warnings: list[str] = []
    backup: str | None = None


class SystemHealthOut(BaseModel):
    db_ok: bool
    integrity: str
    error: str | None = None
    db_bytes: int = 0
    wal_bytes: int = 0
    journal_mode: str | None = None
    sqlite_version: str | None = None
    entries: int = 0
    users: int = 0
    pending_review: int = 0
    tombstones: int = 0
    updated_at: str | None = None


class JobRunOut(BaseModel):
    id: int
    job: str
    started_at: str
    finished_at: str | None = None
    ok: bool | None = None
    stats: dict | None = None
    error: str | None = None


class SystemStatsOut(BaseModel):
    by_status: list[dict]
    by_dimension: list[dict]
    by_source: list[dict]
    top_topics: list[dict]
    review_trend: list[dict]
    pending_backlog: list[dict]


class AdminAuditOut(BaseModel):
    id: int
    actor: str
    action: str
    target: str
    ts: str
    detail: str | None = None
