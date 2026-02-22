"""Pydantic request/response models for API validation."""
from typing import Optional, List
from pydantic import BaseModel, Field


class TaskCreateRequest(BaseModel):
    description: str = Field(max_length=50000)
    assigned_to: str = ""
    status: str = "created"
    depends_on: List[int] = []
    project: str = ""
    branch: str = ""
    task_external_id: str = ""
    parent_id: Optional[int] = None
    priority: int = Field(default=5, ge=1, le=10)
    created_by: str = "user"
    required_role: str = ""
    max_retries: int = Field(default=2, ge=0, le=10)


class TaskUpdateRequest(BaseModel):
    status: Optional[str] = None
    detail: Optional[str] = None
    description: Optional[str] = Field(default=None, max_length=50000)
    assigned_to: Optional[str] = None
    priority: Optional[int] = Field(default=None, ge=1, le=10)
    depends_on: Optional[List[int]] = None
    project: Optional[str] = None
    branch: Optional[str] = None
    review_status: Optional[str] = None
    review_notes: Optional[str] = None
    no_retry: Optional[bool] = None

    model_config = {"extra": "allow"}


class MessageSendRequest(BaseModel):
    sender: str = Field(max_length=50)
    receiver: str = Field(max_length=50)
    content: str = Field(max_length=100000)
    msg_type: str = "message"
    task_external_id: str = ""
    task_id: str = ""

    model_config = {"extra": "allow"}
