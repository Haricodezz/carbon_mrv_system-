"""
payment_processor.py
====================
Processes incoming CCT token transfers to the platform wallet.

Flow:
  1. Verify transaction has >= MIN_CONFIRMATIONS on-chain
  2. Guard against double-processing via processed_transactions table
  3. Resolve sender wallet → registered User
  4. Apply 2% platform fee
  5. Resolve seller's most-recently minted project
  6. Auto-create an active MarketplaceListing (no auditor approval needed)
  7. Record ProcessedTransaction to prevent replay
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from web3 import Web3

import models
from database import SessionLocal

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────
PLATFORM_FEE_RATE    = 0.02          # 2 %
MIN_CONFIRMATIONS    = 3
DEFAULT_PRICE_PER_CREDIT = 1.0       # fallback; overridden by system_settings

# ── RPC (HTTP, used only for confirmation count) ──────────
_RPC_URL = os.getenv("RPC_URL", "")
_w3_http: Optional[Web3] = None


def _get_http_w3() -> Web3:
    """Lazy-initialise an HTTP Web3 instance for confirmation checks."""
    global _w3_http
    if _w3_http is None or not _w3_http.is_connected():
        _w3_http = Web3(Web3.HTTPProvider(_RPC_URL))
    return _w3_http


# ── Helpers ───────────────────────────────────────────────

def _get_setting(db: Session, key: str, default: str) -> str:
    row = db.query(models.SystemSetting).filter_by(key=key).first()
    return row.value if row else default


def _confirmation_count(tx_hash: str) -> int:
    """
    Return the number of block confirmations for *tx_hash*.
    Returns 0 if the transaction is pending or not found.
    """
    try:
        w3  = _get_http_w3()
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        if receipt is None:
            return 0
        latest = w3.eth.block_number
        return max(0, latest - receipt.blockNumber)
    except Exception as exc:
        logger.warning("Could not fetch confirmations for %s: %s", tx_hash, exc)
        return 0


def _is_already_processed(db: Session, tx_hash: str) -> bool:
    return (
        db.query(models.ProcessedTransaction)
        .filter_by(tx_hash=tx_hash.lower())
        .first()
    ) is not None


def _resolve_user(db: Session, wallet_address: str) -> Optional[models.User]:
    """Look up the platform user who owns *wallet_address* (case-insensitive)."""
    normalised = wallet_address.lower()
    for user in db.query(models.User).all():
        if user.wallet_address and user.wallet_address.lower() == normalised:
            return user
    return None


def _resolve_project(db: Session, user_id: int) -> Optional[models.Project]:
    """
    Return the seller's most recently minted project eligible for listing.
    Eligibility: is_minted=True, status in ('approved','verified').
    """
    return (
        db.query(models.Project)
        .filter(
            models.Project.user_id == user_id,
            models.Project.is_minted == True,          # noqa: E712
            models.Project.status.in_(["approved", "verified"]),
        )
        .order_by(models.Project.minted_at.desc())
        .first()
    )


# ── Main entry point ──────────────────────────────────────

def process_cct_payment(
    sender_wallet: str,
    raw_amount: int,
    tx_hash: str,
) -> dict:
    """
    Process an on-chain CCT Transfer event directed at the platform wallet.

    Parameters
    ----------
    sender_wallet : str
        The wallet address of the token sender (checksummed or lower).
    raw_amount : int
        Token amount in *wei* units (18 decimals, as emitted by the Transfer event).
    tx_hash : str
        Transaction hash of the Transfer event.

    Returns
    -------
    dict  with keys: status, message, listing_id (optional), fee (optional)
    """
    tx_hash_lower = tx_hash.lower()
    amount_float  = raw_amount / (10 ** 18)      # convert wei → CCT float

    logger.info(
        "Processing CCT payment | tx=%s | from=%s | amount=%.6f CCT",
        tx_hash_lower, sender_wallet, amount_float,
    )

    # ── 1. Confirmation gate ──────────────────────────────
    confirmations = _confirmation_count(tx_hash)
    if confirmations < MIN_CONFIRMATIONS:
        msg = (
            f"Transaction {tx_hash} only has {confirmations} confirmation(s); "
            f"minimum required: {MIN_CONFIRMATIONS}. Will retry."
        )
        logger.info(msg)
        return {"status": "pending", "message": msg}

    db: Session = SessionLocal()
    try:
        # ── 2. Idempotency guard ──────────────────────────
        if _is_already_processed(db, tx_hash_lower):
            msg = f"Transaction {tx_hash} already processed — skipping."
            logger.info(msg)
            return {"status": "duplicate", "message": msg}

        # ── 3. Resolve sender → User ──────────────────────
        user = _resolve_user(db, sender_wallet)
        if user is None:
            # Record as processed to avoid spamming retries; log the anomaly.
            _record_processed(
                db,
                tx_hash=tx_hash_lower,
                sender_wallet=sender_wallet,
                amount=amount_float,
                fee=0.0,
                listing_id=None,
                note="sender_not_registered",
            )
            msg = (
                f"Sender wallet {sender_wallet} is not registered on the platform. "
                f"Transaction {tx_hash} marked as processed (no listing created)."
            )
            logger.warning(msg)
            return {"status": "unregistered_sender", "message": msg}

        # ── 4. Branch on role ─────────────────────────────
        #
        #   land_owner   → auto-create marketplace listing (existing flow)
        #   organization → credit vault directly (no listing, no fee)
        #   anything else → record as unhandled and return
        #
        if user.role == "organization":
            return _process_org_vault_deposit(
                db=db,
                user=user,
                amount_float=amount_float,
                tx_hash_lower=tx_hash_lower,
                sender_wallet=sender_wallet,
            )

        if user.role not in ("land_owner", "admin"):
            msg = (
                f"Sender {sender_wallet} has role '{user.role}' which is not "
                f"handled for CCT deposits. Transaction {tx_hash} recorded."
            )
            logger.warning(msg)
            _record_processed(
                db,
                tx_hash=tx_hash_lower,
                sender_wallet=sender_wallet,
                amount=amount_float,
                fee=0.0,
                listing_id=None,
                note=f"unhandled_role_{user.role}",
            )
            db.commit()
            return {"status": "unhandled_role", "message": msg}

        # ── 5. Fee calculation (land_owner path) ──────────
        platform_fee   = round(amount_float * PLATFORM_FEE_RATE, 8)
        listing_amount = round(amount_float - platform_fee, 8)

        if listing_amount <= 0:
            msg = (
                f"Listing amount after fee is <= 0 (amount={amount_float:.6f} CCT). "
                f"Transaction {tx_hash} skipped."
            )
            logger.warning(msg)
            _record_processed(
                db,
                tx_hash=tx_hash_lower,
                sender_wallet=sender_wallet,
                amount=amount_float,
                fee=platform_fee,
                listing_id=None,
                note="amount_too_small",
            )
            return {"status": "skipped", "message": msg}

        # ── 6. Resolve project ────────────────────────────
        project = _resolve_project(db, user.id)
        if project is None:
            msg = (
                f"User {user.id} ({user.email}) has no eligible minted project. "
                f"Transaction {tx_hash} recorded but listing NOT created."
            )
            logger.warning(msg)
            _record_processed(
                db,
                tx_hash=tx_hash_lower,
                sender_wallet=sender_wallet,
                amount=amount_float,
                fee=platform_fee,
                listing_id=None,
                note="no_eligible_project",
            )
            return {"status": "no_project", "message": msg}

        # ── 7. Price per credit (from system settings) ────
        try:
            price_per_credit = float(
                _get_setting(db, "default_listing_price", str(DEFAULT_PRICE_PER_CREDIT))
            )
        except (ValueError, TypeError):
            price_per_credit = DEFAULT_PRICE_PER_CREDIT

        # ── 8. Create marketplace listing (auto-active) ───
        listing = models.MarketplaceListing(
            seller_id        = user.id,
            project_id       = project.id,
            credits_amount   = listing_amount,
            price_per_credit = price_per_credit,
            platform_fee     = platform_fee,
            source           = "auto_cct_payment",
            payment_tx       = tx_hash_lower,
            payment_method   = "cct_transfer",
            status           = "active",          # No auditor review needed
            created_at       = datetime.utcnow(),
        )
        db.add(listing)
        db.flush()
        listing_id = listing.id

        # ── 9. Record processed transaction ───────────────
        _record_processed(
            db,
            tx_hash=tx_hash_lower,
            sender_wallet=sender_wallet,
            amount=amount_float,
            fee=platform_fee,
            listing_id=listing_id,
            note="listing_created",
        )

        # ── 10. Audit log ─────────────────────────────────
        db.add(models.AuditLog(
            user_id    = user.id,
            action     = "auto_cct_payment_listed",
            details    = (
                f'{{"tx_hash":"{tx_hash_lower}",'
                f'"amount":{amount_float},'
                f'"fee":{platform_fee},'
                f'"listing_amount":{listing_amount},'
                f'"listing_id":{listing_id},'
                f'"project_id":{project.id}}}'
            ),
            ip_address = "blockchain_listener",
            created_at = datetime.utcnow(),
        ))

        db.commit()

        logger.info(
            "Auto-listed | user=%s | listing_id=%d | credits=%.6f CCT | fee=%.6f CCT | tx=%s",
            user.email, listing_id, listing_amount, platform_fee, tx_hash_lower,
        )

        return {
            "status":         "success",
            "message":        "Marketplace listing created automatically.",
            "listing_id":     listing_id,
            "seller_id":      user.id,
            "project_id":     project.id,
            "credits_listed": listing_amount,
            "fee":            platform_fee,
            "tx_hash":        tx_hash_lower,
        }

    except Exception as exc:
        db.rollback()
        logger.exception("Error processing CCT payment %s: %s", tx_hash, exc)
        return {"status": "error", "message": str(exc)}
    finally:
        db.close()


# ── Internal recorder ─────────────────────────────────────

# ── Organisation vault deposit ───────────────────────────
#
# When an organisation sends CCT to the platform wallet, we credit
# their virtual vault (Wallet row) directly — no listing, no fee.
# The full transferred amount is credited because they are depositing
# their own tokens, not selling.

def _process_org_vault_deposit(
    db: Session,
    user: "models.User",
    amount_float: float,
    tx_hash_lower: str,
    sender_wallet: str,
) -> dict:
    """
    Credit an organisation's vault with the full CCT amount they sent
    to the platform wallet.  No platform fee (deposit, not a sale).
    """
    try:
        wallet = (
            db.query(models.Wallet)
            .filter_by(user_id=user.id)
            .with_for_update()
            .first()
        )

        if wallet is None:
            wallet = models.Wallet(
                user_id            = user.id,
                total_credits      = 0.0,
                available_credits  = 0.0,
                retired_credits    = 0.0,
                buffer_contributed = 0.0,
                status             = "active",
            )
            db.add(wallet)
            db.flush()
            logger.info("Created missing wallet for org user %s", user.id)

        wallet.total_credits     = round(wallet.total_credits     + amount_float, 8)
        wallet.available_credits = round(wallet.available_credits + amount_float, 8)

        _record_processed(
            db,
            tx_hash=tx_hash_lower,
            sender_wallet=sender_wallet,
            amount=amount_float,
            fee=0.0,
            listing_id=None,
            note="org_vault_deposit",
        )

        db.add(models.AuditLog(
            user_id    = user.id,
            action     = "org_cct_vault_deposit",
            details    = (
                f'{{"tx_hash":"{tx_hash_lower}",'
                f'"amount":{amount_float},'
                f'\"new_available\":{wallet.available_credits}}}'
            ),
            ip_address = "blockchain_listener",
            created_at = datetime.utcnow(),
        ))

        db.commit()

        logger.info(
            "Org vault deposit | user=%s | amount=%.6f CCT | new_balance=%.6f | tx=%s",
            user.email, amount_float, wallet.available_credits, tx_hash_lower,
        )

        return {
            "status":          "success",
            "message":         "Organisation vault credited automatically.",
            "user_id":         user.id,
            "amount_credited": amount_float,
            "new_available":   wallet.available_credits,
            "tx_hash":         tx_hash_lower,
        }

    except Exception as exc:
        db.rollback()
        logger.exception(
            "Error crediting org vault for tx %s: %s", tx_hash_lower, exc
        )
        return {"status": "error", "message": str(exc)}


def _record_processed(
    db: Session,
    *,
    tx_hash: str,
    sender_wallet: str,
    amount: float,
    fee: float,
    listing_id: Optional[int],
    note: str,
) -> None:
    """
    Insert a row in processed_transactions.
    Called *before* commit so it rolls back atomically on failure.
    """
    db.add(models.ProcessedTransaction(
        tx_hash       = tx_hash,
        sender_wallet = sender_wallet,
        amount        = amount,
        fee           = fee,
        listing_id    = listing_id,
        note          = note,
        created_at    = datetime.utcnow(),
    ))