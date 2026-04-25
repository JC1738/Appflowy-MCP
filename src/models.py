from pydantic import BaseModel
from typing import Optional, Dict, Any, List

# AppFlowy Cloud Models


class LoginRequest(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = None


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str


class Workspace(BaseModel):
    id: str
    name: str
    database_id: Optional[str] = None


class Database(BaseModel):
    id: str
    name: str
    workspace_id: str


class RowDetail(BaseModel):
    id: str
    cells: Dict[str, Any]
    document: Optional[str] = None


class RowCreateRequest(BaseModel):
    cells: Dict[str, Any]
    document: Optional[str] = None


class RowUpdateRequest(BaseModel):
    pre_hash: Optional[str] = None
    cells: Dict[str, Any]
    document: Optional[str] = None


# Todoist Models (existing)
class Task(BaseModel):
    id: str | None = None
    content: str
    description: str
    project_id: str | None = None
    priority: int
