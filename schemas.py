"""Request bodies of the HTTP API (pydantic)."""
from typing import Literal

from pydantic import BaseModel, Field


class AuthReq(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    password: str = Field(min_length=8, max_length=128)


class RefreshReq(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=200)


class LogoutReq(BaseModel):
    refresh_token: str | None = Field(default=None, max_length=200)
    all: bool = False  # true = end every session of this account (needs a valid access token)


class ForgotReq(BaseModel):
    email: str = Field(min_length=5, max_length=254)


class ResetReq(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=8, max_length=128)


class VerifyReq(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class DeleteAccountReq(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class PrefsReq(BaseModel):
    lang: str | None = Field(default=None, max_length=8)
    theme: str | None = Field(default=None, max_length=8)


class AiReq(BaseModel):
    query: str = Field(min_length=1, max_length=300)
    lang: str = "en"


class SeedReq(BaseModel):
    url: str = Field(min_length=8, max_length=2000)


class SeedEnabledReq(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    enabled: bool


class RoleReq(BaseModel):
    role: Literal["user", "admin"]


class HistoryReq(BaseModel):
    query: str = Field(min_length=1, max_length=200)


class SavedReq(BaseModel):
    title: str = Field(max_length=300)
    url: str = Field(max_length=2000)
    snippet: str = Field(default="", max_length=1000)
    thumbnail: str | None = Field(default=None, max_length=2000)


class CrawlReq(BaseModel):
    seeds: list[str] = Field(default_factory=list, max_length=50)  # empty = use the enabled seeds saved in the dashboard
    max_pages: int = Field(default=100, ge=1, le=5000)
    max_depth: int = Field(default=2, ge=0, le=5)
    delay: float = Field(default=1.0, ge=0.2, le=10)

