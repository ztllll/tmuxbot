from pydantic import BaseModel, Field


class PasswordRequest(BaseModel):
    password: str = Field(min_length=12, max_length=1024)


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    root_path: str = Field(min_length=1, max_length=4096)


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
