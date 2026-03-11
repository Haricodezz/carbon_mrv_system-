import os
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from jose import jwt, JWTError
from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, validator

# ── Config ────────────────────────────────────────────────
SECRET_KEY  = os.getenv("JWT_SECRET_KEY", "carbon-mrv-super-secret-key-change-in-production")
ALGORITHM   = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7 # 7 days

bearer = HTTPBearer(auto_error=False)


# ── Pydantic Models for Request Validation ────────────────
class LoginRequest(BaseModel):
    """Request model for user login"""
    email: str
    password: str
    
    @validator('email')
    def email_required(cls, v):
        """Validate email is not empty and normalize"""
        if not v or not str(v).strip():
            raise ValueError('Email is required')
        return str(v).strip().lower()
    
    @validator('password')
    def password_required(cls, v):
        """Validate password is not empty"""
        if not v:
            raise ValueError('Password is required')
        return v


class RegisterRequest(BaseModel):
    """Request model for user registration"""
    name: str
    email: str
    password: str
    # Put role BEFORE wallet_address so validators for wallet can see role
    role: str = "land_owner"
    wallet_address: Optional[str] = None  # Optional at model level; validated per-role below

    @validator('name')
    def name_required(cls, v):
        """Validate name is not empty"""
        if not v or not str(v).strip():
            raise ValueError('Name is required')
        return str(v).strip()
    
    @validator('email')
    def email_required(cls, v):
        """Validate email is not empty and normalize"""
        if not v or not str(v).strip():
            raise ValueError('Email is required')
        return str(v).strip().lower()
    
    @validator('password')
    def password_min_length(cls, v):
        """Validate password is at least 8 characters"""
        if not v or len(v) < 8:
            raise ValueError('Password must be at least 8 characters')
        return v
    
    @validator('role')
    def validate_role(cls, v):
        """Validate role is one of allowed values"""
        if v not in ["land_owner", "organization", "auditor", "admin"]:
            raise ValueError('Role must be: land_owner, organization, auditor, or admin')
        return v

    @validator('wallet_address')
    def wallet_optional_for_auditor(cls, v, values):
        """
        Per-role wallet validation:
        - land_owner / organization: wallet is required (non-empty)
        - auditor / admin: wallet is ignored and not required
        """
        role = values.get('role', 'land_owner')

        # Auditors and admins: never require / validate wallet
        if role in ['auditor', 'admin']:
            return None

        # Land owners & organizations: wallet is mandatory
        if not v or not str(v).strip():
            raise ValueError(f'Wallet address is required for {role}')

        return str(v).strip()


class TokenResponse(BaseModel):
    """Response model for authentication"""
    access_token: str
    token_type: str
    user_id: int
    role: str
    name: str
    expires_in: str


class VerifyUserRequest(BaseModel):
    """Request model for user verification by admin/auditor"""
    verified: bool = True
    reason: Optional[str] = None


# ── Password ──────────────────────────────────────────────
def hash_password(password: str) -> str:
    """Hash password using bcrypt (max 72 bytes)"""
    pwd_bytes = password.encode("utf-8")[:72]
    return bcrypt.hashpw(pwd_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify plain text password against bcrypt hash"""
    pwd_bytes    = plain.encode("utf-8")[:72]
    hashed_bytes = hashed.encode("utf-8")
    return bcrypt.checkpw(pwd_bytes, hashed_bytes)


# ── JWT ───────────────────────────────────────────────────
def create_access_token(user_id: int, role: str) -> str:
    """Create JWT access token with user info and expiration"""
    expire  = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "role": role, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode JWT token and return payload"""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token.")


# ── API Key ───────────────────────────────────────────────
def generate_api_key() -> tuple[str, str]:
    """Returns (raw_key, hashed_key). Store only the hash."""
    raw    = "mrv_" + secrets.token_hex(32)
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    return raw, hashed


def hash_api_key(raw: str) -> str:
    """Hash API key using SHA-256"""
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Dependency: get current user from JWT or API Key ──────
def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(bearer),
):
    """Extract and validate user from Bearer token (JWT or API Key)"""
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required.")

    token = credentials.credentials

    # API Key path
    if token.startswith("mrv_"):
        return {"token": token, "auth_type": "api_key"}

    # JWT path
    payload = decode_token(token)
    return {
        "user_id":   int(payload["sub"]),
        "role":      payload["role"],
        "auth_type": "jwt",
    }


def require_role(allowed_roles: list):
    """Dependency factory — restricts endpoint to certain roles."""
    def checker(current_user: dict = Security(get_current_user)):
        if current_user["auth_type"] == "api_key":
            return current_user
        if current_user["role"] not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Access denied. Required roles: {allowed_roles}"
            )
        return current_user
    return checker


def require_admin(current_user: dict = Security(get_current_user)):
    """Dependency to require admin role"""
    if current_user["auth_type"] == "api_key":
        return current_user
    if current_user["role"] != "admin":
        raise HTTPException(
            status_code=403,
            detail="Admin access required"
        )
    return current_user


def require_auditor_or_admin(current_user: dict = Security(get_current_user)):
    """Dependency to require auditor or admin role"""
    if current_user["auth_type"] == "api_key":
        return current_user
    if current_user["role"] not in ["auditor", "admin"]:
        raise HTTPException(
            status_code=403,
            detail="Auditor or Admin access required"
        )
    return current_user