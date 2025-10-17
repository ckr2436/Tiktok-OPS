# app/features/tenants/users/schemas.py
from __future__ import annotations
from typing import List, Optional, Literal
from datetime import datetime
from pydantic import BaseModel, Field, EmailStr

class UserItem(BaseModel):
    id: int
    email: Optional[str] = None
    username: str
    display_name: Optional[str] = None
    usercode: Optional[str] = None
    role: Literal['owner', 'admin', 'member']
    is_active: bool = True
    workspace_id: int
    created_by_user_id: Optional[int] = None
    created_at: datetime

class UsersListResponse(BaseModel):
    items: List[UserItem] = Field(default_factory=list)

class CreateUserRequest(BaseModel):
    email: Optional[EmailStr] = None
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    role: Literal['admin', 'member'] = 'member'
    display_name: Optional[str] = Field(default=None, max_length=128)

class CreateUserResponse(BaseModel):
    id: int
    role: Literal['admin', 'member']
    usercode: str

