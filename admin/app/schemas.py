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


# ---------------------------------------------------------------------------
# PKI: device/app certificates
# ---------------------------------------------------------------------------
class ActivateRequest(BaseModel):
    """``POST /api/device/activate`` / ``POST /api/app/activate`` (one-way HTTPS).

    ``csr`` is a PEM PKCS#10 request whose private key never leaves the device.
    ``token`` is the single-use activation token minted by an admin.
    ``timestamp``/``nonce``/``signature`` are the optional proof of possession
    over ``activation_canonical(device_id, csr, timestamp, nonce)``.
    """

    model_config = ConfigDict(extra="allow")
    device_id: str = ""
    csr: str = ""
    token: str = ""
    uuid: str = ""
    timestamp: str = ""
    nonce: str = ""
    signature: str = ""


class RenewRequest(BaseModel):
    """``POST /api/cert/renew`` (mTLS).

    ``device_id`` is ignored on purpose: the identity comes from the client
    certificate, so a renewal can never widen its own scope.
    """

    model_config = ConfigDict(extra="allow")
    csr: str = ""
    device_id: str = ""


class ActivationTokenRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    device_id: str = ""
    cert_type: str = "device"  # device | app
    ttl_hours: int = 24
    allow_reissue: bool = False


class RevokeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    serial: str = ""
    device_id: str = ""
    cert_type: str = ""
    reason: str = "unspecified"
