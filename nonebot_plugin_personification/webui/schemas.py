from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    qq: str = Field(..., min_length=4, max_length=20, description="管理员 QQ 号")


class LoginResponse(BaseModel):
    sent: bool
    cooldown_seconds: int = 0
    message: str = ""
    request_id: str = ""
    passwordless: bool = False
    pending: bool = False


class VerifyRequest(BaseModel):
    qq: str = Field(..., min_length=4, max_length=20)
    code: str = Field(..., min_length=6, max_length=6)
    device_label: str = Field(default="", max_length=64)


class VerifyResponse(BaseModel):
    success: bool
    message: str = ""
    pending: bool = False


class DeviceInfo(BaseModel):
    id: str
    qq: str
    label: str
    ua: str
    created_at: float
    last_seen: float
    status: str = "approved"


class DeviceListResponse(BaseModel):
    devices: list[DeviceInfo]
    current_device_id: str = ""


class PendingDeviceListResponse(BaseModel):
    devices: list[DeviceInfo]


class ConfigEntryView(BaseModel):
    key: str
    field_name: str
    label: str
    description: str
    group: str
    kind: str
    value_type: str
    required: bool
    secret: bool
    advanced: bool = False
    example: str = ""
    aliases: list[str] = []
    search_index: list[str] = []
    default: Any = None
    current: Any = None
    active_source: str = "default"
    sources: dict[str, Any] = {}
    choices: list[str] = []
    min_value: float | None = None
    max_value: float | None = None
    scope: str = "global"


class ConfigEntriesResponse(BaseModel):
    entries: list[ConfigEntryView]
    groups: list[str]


class ConfigUpdateRequest(BaseModel):
    field_name: str
    value: Any


class ConfigUpdateResponse(BaseModel):
    success: bool
    errors: list[str] = []
    dotenv_path: str | None = None
    env_json_path: str | None = None
    new_value: Any = None


class AdminIdentity(BaseModel):
    qq: str
    device_id: str
    label: str
