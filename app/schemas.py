"""Pydantic schemas for request/response validation."""

from pydantic import BaseModel


class LoginRequest(BaseModel):
    employee_name: str
    password: str
    app_id: str
    redirect_uri: str


class RegisterRequest(BaseModel):
    employee_name: str
    password: str
    confirm_password: str


class TokenRequest(BaseModel):
    code: str
    app_id: str
    client_secret: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 43200  # 12 hours


class ForgotPasswordRequest(BaseModel):
    employee_name: str


class StaffInfo(BaseModel):
    employee_name: str
    name: str
    dept_code: str
    level: int
    ext: str = ""
