from pydantic import BaseModel, Field


class PasswordRequest(BaseModel):
    password: str = Field(min_length=12, max_length=1024)
