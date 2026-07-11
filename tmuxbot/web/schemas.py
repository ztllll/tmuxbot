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
