from typing import Literal

from pydantic import BaseModel, Field


class PasswordRequest(BaseModel):
    password: str = Field(min_length=12, max_length=1024)


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    root_path: str = Field(min_length=1, max_length=4096)


class ProjectInspectRequest(BaseModel):
    root_path: str = Field(min_length=1, max_length=4096)


class ProjectUpdateRequest(ProjectCreateRequest):
    """A project is identified by its server-side record, never its browser path."""


class ManagedSessionAdoptRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    provider_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    target: str = Field(min_length=3, max_length=256)


class ObservedTerminalTicketRequest(BaseModel):
    target: str = Field(min_length=3, max_length=256)


class ManagedSessionCreateRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    provider_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)


class ChannelConfigureRequest(BaseModel):
    channel: str = Field(pattern="^(telegram|feishu)$")
    managed_session_id: str = Field(min_length=1, max_length=128)
    remote_chat_id: str = Field(min_length=1, max_length=256)
    credential_id: str = Field(min_length=1, max_length=512)
    credential_secret: str | None = Field(default=None, min_length=1, max_length=2048)
    boss_id: str = Field(min_length=1, max_length=256)
    mention_required: bool = False
class TeamAgentRequest(BaseModel):
    role: Literal["coordinator", "implementer", "reviewer"]
    managed_session_id: str = Field(min_length=1, max_length=256)


class TeamTaskRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    goal: str = Field(min_length=1, max_length=4096)
    role: Literal["coordinator", "implementer", "reviewer"] = "implementer"
    dependencies: list[str] = Field(default_factory=list, max_length=128)
    requires_write: bool = False
    max_attempts: int = Field(default=2, ge=1, le=10)


class CreateTeamRunRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=4096)
    idempotency_key: str = Field(min_length=1, max_length=256)
    agents: list[TeamAgentRequest] = Field(min_length=3, max_length=3)
    tasks: list[TeamTaskRequest] = Field(min_length=1, max_length=256)


class IdempotentCommandRequest(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=256)


class StopTeamRunRequest(IdempotentCommandRequest):
    reason: str = Field(min_length=1, max_length=1024)


class ArtifactRequest(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    uri: str = Field(min_length=1, max_length=4096)
    metadata: dict[str, object] = Field(default_factory=dict)


class CompleteTeamTaskRequest(IdempotentCommandRequest):
    agent_id: str = Field(min_length=1, max_length=256)
    artifacts: list[ArtifactRequest] = Field(min_length=1, max_length=64)


class BlockTeamTaskRequest(IdempotentCommandRequest):
    agent_id: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=4096)


class ReviewTeamTaskRequest(IdempotentCommandRequest):
    reviewer_agent_id: str = Field(min_length=1, max_length=256)
    verdict: Literal["approved", "rejected"]
    notes: str = Field(default="", max_length=4096)
