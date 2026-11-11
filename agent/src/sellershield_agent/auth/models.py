"""Authentication and authorization models for SellerShield Agent."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Role(StrEnum):
    """Allowed principal roles in the store and agent domain."""

    customer = "customer"
    seller_staff = "seller_staff"
    seller_owner = "seller_owner"
    service = "service"


class Principal(BaseModel):
    """Authenticated identity extracted from verified store-issued JWT."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: str
    role: Role
    customer_id: str | None = None
<<<<<<< HEAD
    store_id: str = "nestkart"
=======
>>>>>>> 547a5bef9dd9e200c6118b2900efc9f5447e87be
    scopes: list[str] = Field(default_factory=list)
    auth_time: int | None = None
    jti: str
    token_exp: int
