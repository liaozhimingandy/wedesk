"""Pydantic request models.

Responses are returned as plain dicts (not response_model) so the exact JSON key
ordering/shape expected by the RustDesk client is preserved. Request bodies use
``extra="allow"`` because the client sends several optional fields we ignore.
"""

from pydantic import BaseModel, ConfigDict


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    username: str = ""
    password: str = ""


class CurrentUserRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = ""
    uuid: str = ""


class LogoutRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = ""
    uuid: str = ""


class AbSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="allow")


class SwitchGrantRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = ""
    switch_code_verifier: str = ""
    timestamp: str = ""
    signature: str = ""


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = ""
    password: str = ""
    email: str = ""
    note: str = ""
    group_name: str = ""
    is_admin: bool = False


class UpdateUserRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = ""
    email: str = ""
    note: str = ""
    group_name: str = ""
    password: str = ""


class ForceLogoutRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    user_guids: list = []


class TfaEnforceRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    user_guids: list = []
    enforce: bool = False
    url: str = ""


class DisableLoginVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    user_guids: list = []
    type: str = ""


class CreateAbRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = ""
    owner: str = ""
    note: str = ""


class AddAbPeerRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    peers: list = []


class UpdateAbPeerRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    peers: list = []


class AbRuleRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    ab: str = ""
    users: list = []
    rule: int = 1


class DeployRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = ""
    uuid: str = ""
    pk: str = ""
