from sqlalchemy import Column, Integer, Float, String, DateTime, ForeignKey, JSON, Boolean, Text
from datetime import datetime
from database import Base


# =====================================================
# USER TABLE
# =====================================================
class User(Base):
    __tablename__ = "users"

    id             = Column(Integer, primary_key=True, index=True)
    name           = Column(String, nullable=False)
    email          = Column(String, unique=True, index=True, nullable=False)
    password_hash  = Column(String, nullable=False)
    wallet_address = Column(String, unique=True, index=True, nullable=False)
    payout_account_holder = Column(String, nullable=True)
    payout_bank_name      = Column(String, nullable=True)
    payout_account_number = Column(String, nullable=True)
    payout_ifsc_code      = Column(String, nullable=True)
    payout_branch_name    = Column(String, nullable=True)
    payout_updated_at     = Column(DateTime, nullable=True)
    role           = Column(String, default="land_owner")   # land_owner | organization | auditor | admin
    is_verified    = Column(Boolean, default=False)
    is_active      = Column(Boolean, default=True)
    is_approved    = Column(Boolean, default=False)         # controlled by admin toggle
    created_at     = Column(DateTime, default=datetime.utcnow)


# =====================================================
# API KEY TABLE
# =====================================================
class ApiKey(Base):
    __tablename__ = "api_keys"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    key_hash   = Column(String, unique=True, nullable=False)
    name       = Column(String, nullable=False)
    is_active  = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used  = Column(DateTime, nullable=True)


# =====================================================
# WALLET TABLE
# =====================================================
class Wallet(Base):
    __tablename__ = "wallets"

    id                 = Column(Integer, primary_key=True, index=True)
    user_id            = Column(Integer, ForeignKey("users.id"), nullable=False)
    total_credits      = Column(Float, default=0.0)
    available_credits  = Column(Float, default=0.0)
    retired_credits    = Column(Float, default=0.0)
    buffer_contributed = Column(Float, default=0.0)
    status             = Column(String, default="active")
    last_updated       = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =====================================================
# PROJECT TABLE
# =====================================================
class Project(Base):
    __tablename__ = "projects"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=False)
    project_name    = Column(String, nullable=True)
    coordinates_key = Column(String, unique=True, index=True, nullable=True)
    area_hectares   = Column(Float, nullable=False)
    baseline_carbon = Column(Float, nullable=False)
    coordinates     = Column(JSON, nullable=False)
    land_verified   = Column(Boolean, default=False)
    land_doc_url    = Column(String, nullable=True)
    status          = Column(String, default="pending")     # pending | approved | verified | rejected
    is_minted       = Column(Boolean, default=False)
    mint_tx_hash    = Column(String, nullable=True)
    minted_at       = Column(DateTime, nullable=True)
    approved_by     = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at     = Column(DateTime, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    is_blacklisted  = Column(Boolean, default=False, nullable=True)
    is_flagged      = Column(Boolean, default=False, nullable=True)
    flag_reason     = Column(String, nullable=True)


# =====================================================
# CARBON RECORD TABLE
# =====================================================
class CarbonRecord(Base):
    __tablename__ = "carbon_records"

    id                       = Column(Integer, primary_key=True, index=True)
    project_id               = Column(Integer, ForeignKey("projects.id"), nullable=False)
    carbon_stock             = Column(Float, nullable=False)
    carbon_credits_generated = Column(Float, default=0.0)
    buffer_credits_added     = Column(Float, default=0.0)
    measured_at              = Column(DateTime, default=datetime.utcnow)


# =====================================================
# BUFFER POOL TABLE
# =====================================================
class BufferPool(Base):
    __tablename__ = "buffer_pool"

    id                   = Column(Integer, primary_key=True, index=True)
    total_buffer_credits = Column(Float, default=0.0)
    last_updated         = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =====================================================
# CREDIT TRANSFER TABLE
# =====================================================
class CreditTransfer(Base):
    __tablename__ = "credit_transfers"

    id             = Column(Integer, primary_key=True, index=True)
    from_user_id   = Column(Integer, ForeignKey("users.id"), nullable=False)
    to_user_id     = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount         = Column(Float, nullable=False)
    blockchain_tx  = Column(String, nullable=True)
    note           = Column(String, nullable=True)
    transferred_at = Column(DateTime, default=datetime.utcnow)


# =====================================================
# MARKETPLACE LISTING TABLE
# =====================================================
class MarketplaceListing(Base):
    __tablename__ = "marketplace_listings"

    id               = Column(Integer, primary_key=True, index=True)
    seller_id        = Column(Integer, ForeignKey("users.id"), nullable=False)
    project_id       = Column(Integer, ForeignKey("projects.id"), nullable=False)
    credits_amount   = Column(Float, nullable=False)
    price_per_credit = Column(Float, nullable=False)
    status           = Column(String, default="active")     # active | sold | cancelled
    created_at       = Column(DateTime, default=datetime.utcnow)
    sold_at          = Column(DateTime, nullable=True)
    buyer_id         = Column(Integer, ForeignKey("users.id"), nullable=True)
    payment_method   = Column(String, nullable=True)        # metamask | razorpay | cct_transfer
    payment_tx       = Column(String, nullable=True)
    # ── CCT auto-payment fields ──────────────────────────
    platform_fee     = Column(Float, nullable=True)         # 2% deducted before listing
    source           = Column(String, default="manual")     # manual | auto_cct_payment


# =====================================================
# CERTIFICATE TABLE
# =====================================================
class Certificate(Base):
    __tablename__ = "certificates"

    id             = Column(Integer, primary_key=True, index=True)
    project_id     = Column(Integer, ForeignKey("projects.id"), nullable=False)
    user_id        = Column(Integer, ForeignKey("users.id"), nullable=False)
    credits_amount = Column(Float, nullable=False)
    blockchain_tx  = Column(String, nullable=True)
    pdf_path       = Column(String, nullable=True)
    issued_at      = Column(DateTime, default=datetime.utcnow)


# =====================================================
# AUDIT LOG TABLE
# =====================================================
class AuditLog(Base):
    __tablename__ = "audit_logs"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=True)
    action     = Column(String, nullable=False)
    details    = Column(Text, nullable=True)
    ip_address = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# =====================================================
# DOCUMENT TABLE  (NEW)
# =====================================================
class Document(Base):
    __tablename__ = "documents"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), nullable=False)
    project_id   = Column(Integer, ForeignKey("projects.id"), nullable=True)
    file_path    = Column(String, nullable=False)
    file_name    = Column(String, nullable=False)
    file_type    = Column(String, nullable=False)            # pdf | jpg | png
    doc_type     = Column(String, nullable=False)            # aadhaar | land_deed | ngo_cert | gst | cin | incorporation
    status       = Column(String, default="pending")         # pending | approved | rejected
    reviewed_by  = Column(Integer, ForeignKey("users.id"), nullable=True)
    review_note  = Column(String, nullable=True)
    reviewed_at  = Column(DateTime, nullable=True)
    uploaded_at  = Column(DateTime, default=datetime.utcnow)


# =====================================================
# CREDIT RETIREMENT TABLE  (NEW)
# =====================================================
class CreditRetirement(Base):
    __tablename__ = "credit_retirements"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=False)
    amount          = Column(Float, nullable=False)
    retirement_id   = Column(String, unique=True, nullable=False)   # RET-0001-202603
    reason          = Column(String, nullable=True)                 # ESG compliance / carbon tax
    blockchain_tx   = Column(String, nullable=True)
    pdf_path        = Column(String, nullable=True)
    retired_at      = Column(DateTime, default=datetime.utcnow)


# =====================================================
# SYSTEM SETTINGS TABLE  (NEW)
# =====================================================
class SystemSetting(Base):
    __tablename__ = "system_settings"

    id          = Column(Integer, primary_key=True, index=True)
    key         = Column(String, unique=True, nullable=False)
    value       = Column(String, nullable=False)
    description = Column(String, nullable=True)
    updated_by  = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# =====================================================
# BLOCKCHAIN TRANSACTION TABLE  (NEW - CRITICAL)
# =====================================================
class BlockchainTransaction(Base):
    __tablename__ = "blockchain_transactions"

    id           = Column(Integer, primary_key=True, index=True)

    user_id      = Column(Integer, ForeignKey("users.id"), nullable=False)
    project_id   = Column(Integer, ForeignKey("projects.id"), nullable=True)

    tx_hash      = Column(String, nullable=False)
    tx_type      = Column(String, nullable=False)   # mint | transfer | retire
    amount       = Column(Float, nullable=False)

    status       = Column(String, nullable=False)   # success | failed
    error_msg    = Column(Text, nullable=True)

    created_at   = Column(DateTime, default=datetime.utcnow)


# =====================================================
# PROCESSED TRANSACTIONS TABLE  (CCT auto-payment)
# =====================================================
class ProcessedTransaction(Base):
    __tablename__ = "processed_transactions"

    id            = Column(Integer, primary_key=True, index=True)
    tx_hash       = Column(String, unique=True, nullable=False, index=True)   # lowercase
    sender_wallet = Column(String, nullable=False)
    amount        = Column(Float, nullable=False)        # full amount received (CCT)
    fee           = Column(Float, nullable=False)        # 2% platform fee (CCT)
    listing_id    = Column(Integer, ForeignKey("marketplace_listings.id"), nullable=True)
    note          = Column(String, nullable=True)        # status note / error reason
    created_at    = Column(DateTime, default=datetime.utcnow)