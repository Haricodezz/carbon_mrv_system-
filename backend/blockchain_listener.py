"""
blockchain_listener.py
======================
Listens for ERC-20 Transfer events on the CCT contract where
    to == PLATFORM_WALLET_ADDRESS

Two transport modes (auto-selected):
  ① WebSocket  – if WS_RPC_URL is set in .env  (recommended for production)
  ② HTTP poll  – fallback using HTTP RPC_URL    (Polygon Amoy default)

Both modes call payment_processor.process_cct_payment() on each match.

Reconnect logic:
  - WebSocket: exponential back-off up to MAX_BACKOFF_SECONDS
  - HTTP poll:  fixed POLL_INTERVAL_SECONDS interval, never exits

Usage (called from FastAPI lifespan):
    asyncio.create_task(start_listener())
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from payment_processor import process_cct_payment

logger = logging.getLogger(__name__)

# ── Environment ───────────────────────────────────────────
RPC_URL             = os.getenv("RPC_URL", "")
WS_RPC_URL          = os.getenv("WS_RPC_URL", "")          # Optional WebSocket URL
CONTRACT_ADDRESS    = os.getenv("CONTRACT_ADDRESS", "")
PLATFORM_WALLET     = os.getenv("PLATFORM_WALLET_ADDRESS", "")

# ── Tuning ────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = 15            # seconds between HTTP polls
MAX_BACKOFF_SECONDS   = 300           # 5 min ceiling for WS reconnect
CONFIRMATION_WAIT     = 12            # seconds to wait before processing new block

# ── ERC-20 Transfer event ABI ─────────────────────────────
TRANSFER_EVENT_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "internalType": "address", "name": "from",  "type": "address"},
            {"indexed": True,  "internalType": "address", "name": "to",    "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "value", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    }
]

# ── Validation ────────────────────────────────────────────
def _validate_config() -> bool:
    missing = [k for k, v in {
        "RPC_URL":                  RPC_URL,
        "CONTRACT_ADDRESS":         CONTRACT_ADDRESS,
        "PLATFORM_WALLET_ADDRESS":  PLATFORM_WALLET,
    }.items() if not v]
    if missing:
        logger.error("Blockchain listener disabled — missing env vars: %s", missing)
        return False
    return True


def _build_w3_http() -> Optional[Web3]:
    try:
        w3 = Web3(Web3.HTTPProvider(RPC_URL))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        if w3.is_connected():
            return w3
        logger.error("HTTP Web3 not connected to %s", RPC_URL)
    except Exception as exc:
        logger.error("HTTP Web3 build failed: %s", exc)
    return None


def _build_w3_ws() -> Optional[Web3]:
    if not WS_RPC_URL:
        return None
    try:
        from web3 import WebsocketProvider
        w3 = Web3(WebsocketProvider(WS_RPC_URL))
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        if w3.is_connected():
            logger.info("WebSocket connected to %s", WS_RPC_URL)
            return w3
        logger.warning("WebSocket provider built but not connected: %s", WS_RPC_URL)
    except Exception as exc:
        logger.warning("WebSocket Web3 build failed: %s", exc)
    return None


def _get_contract(w3: Web3):
    return w3.eth.contract(
        address=Web3.to_checksum_address(CONTRACT_ADDRESS),
        abi=TRANSFER_EVENT_ABI,
    )


# ── Pending-retry queue ───────────────────────────────────
# Stores tuples of (sender, raw_amount, tx_hash) for transactions
# that were detected but had fewer than MIN_CONFIRMATIONS at the time.
# The listener re-tries these on every subsequent poll cycle until
# they are either processed or permanently skipped.
_pending_retries: list = []


def _safe_dispatch(sender: str, raw_amount: int, tx_hash: str) -> None:
    """Call the payment processor, swallowing exceptions so listener never dies."""
    try:
        result = process_cct_payment(
            sender_wallet=sender,
            raw_amount=raw_amount,
            tx_hash=tx_hash,
        )
        status = result.get("status")
        if status == "success":
            logger.info(
                "Payment processed -> user credited | tx=%s | detail=%s",
                tx_hash, result.get("message", "")
            )
        elif status == "pending":
            # Not enough confirmations yet — add to retry queue if not already there
            if not any(t[2] == tx_hash for t in _pending_retries):
                _pending_retries.append((sender, raw_amount, tx_hash))
                logger.info(
                    "Tx queued for confirmation retry: %s (%d in queue)",
                    tx_hash, len(_pending_retries)
                )
        elif status in ("duplicate", "unregistered_sender", "unhandled_role"):
            # Terminal states — do not retry
            logger.info("Payment processor terminal result [%s]: %s", status, tx_hash)
        else:
            logger.info("Payment processor result [%s]: %s", status, tx_hash)
    except Exception as exc:
        logger.exception("Unhandled error dispatching payment for %s: %s", tx_hash, exc)


def _flush_pending_retries() -> None:
    """
    Re-attempt all transactions in _pending_retries that previously lacked
    enough confirmations.  Removes entries that are now processed or terminal.
    """
    if not _pending_retries:
        return

    logger.debug("Retrying %d pending transaction(s)...", len(_pending_retries))
    still_pending = []
    for sender, raw_amount, tx_hash in list(_pending_retries):
        try:
            result = process_cct_payment(
                sender_wallet=sender,
                raw_amount=raw_amount,
                tx_hash=tx_hash,
            )
            status = result.get("status")
            if status == "pending":
                still_pending.append((sender, raw_amount, tx_hash))
            elif status == "success":
                logger.info(
                    "Pending tx now confirmed and processed: %s | %s",
                    tx_hash, result.get("message", "")
                )
            else:
                # duplicate / error / terminal — stop retrying
                logger.info(
                    "Pending tx resolved [%s]: %s", status, tx_hash
                )
        except Exception as exc:
            logger.exception("Error retrying pending tx %s: %s", tx_hash, exc)
            still_pending.append((sender, raw_amount, tx_hash))  # keep and retry later

    removed = len(_pending_retries) - len(still_pending)
    if removed:
        logger.info("Cleared %d resolved pending tx(s); %d still waiting.", removed, len(still_pending))
    _pending_retries.clear()
    _pending_retries.extend(still_pending)


# ════════════════════════════════════════════════════════
#  MODE A — WebSocket subscription (preferred)
# ════════════════════════════════════════════════════════

async def _listen_websocket() -> None:
    """
    Subscribe to Transfer events via WebSocket.
    Reconnects with exponential back-off on failure.
    """
    backoff = 5  # initial seconds
    platform_checksum = Web3.to_checksum_address(PLATFORM_WALLET)

    while True:
        w3 = _build_w3_ws()
        if w3 is None:
            logger.error(
                "Cannot establish WebSocket connection. "
                "Falling back to HTTP polling."
            )
            await _listen_http_poll()
            return

        try:
            contract  = _get_contract(w3)
            event_filter = contract.events.Transfer.create_filter(
                from_block="latest",
                argument_filters={"to": platform_checksum},
            )
            logger.info(
                "WebSocket listener active | contract=%s | to=%s",
                CONTRACT_ADDRESS, platform_checksum,
            )
            backoff = 5   # reset on successful connect

            while True:
                for event in event_filter.get_new_entries():
                    try:
                        args      = event["args"]
                        sender    = args["from"]
                        raw_value = int(args["value"])
                        tx_hash   = event["transactionHash"].hex()

                        # Contract address guard (belt-and-braces)
                        if Web3.to_checksum_address(
                            event.get("address", "")
                        ) != Web3.to_checksum_address(CONTRACT_ADDRESS):
                            logger.debug(
                                "Ignoring event from unexpected contract: %s",
                                event.get("address"),
                            )
                            continue

                        logger.info(
                            "Transfer event detected | from=%s | amount=%s wei | tx=%s",
                            sender, raw_value, tx_hash,
                        )
                        _safe_dispatch(sender, raw_value, tx_hash)

                    except Exception as exc:
                        logger.warning("Error parsing WS event entry: %s", exc)

                # Retry pending low-confirmation transactions every cycle
                _flush_pending_retries()
                await asyncio.sleep(2)  # poll the WS filter every 2 s

        except Exception as exc:
            logger.error(
                "WebSocket listener dropped: %s. Reconnecting in %ds…",
                exc, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)


# ════════════════════════════════════════════════════════
#  MODE B — HTTP block polling (fallback / always available)
# ════════════════════════════════════════════════════════

async def _listen_http_poll() -> None:
    """
    Poll for Transfer events every POLL_INTERVAL_SECONDS using HTTP provider.
    Tracks the last-seen block to avoid re-processing the same logs.
    """
    platform_checksum = Web3.to_checksum_address(PLATFORM_WALLET)
    contract_checksum = Web3.to_checksum_address(CONTRACT_ADDRESS)

    w3       = _build_w3_http()
    last_block: Optional[int] = None

    if w3 is None:
        logger.error("HTTP Web3 unavailable; blockchain listener cannot start.")
        return

    contract = _get_contract(w3)

    # Start from the current block minus a small look-back to avoid missing
    # events that arrived during the startup window.
    try:
        last_block = max(0, w3.eth.block_number - 10)
    except Exception:
        last_block = 0

    logger.info(
        "HTTP poll listener active | contract=%s | to=%s | start_block=%d",
        CONTRACT_ADDRESS, platform_checksum, last_block,
    )

    while True:
        try:
            # Re-connect if needed
            if w3 is None or not w3.is_connected():
                logger.warning("HTTP Web3 disconnected — reconnecting…")
                w3 = _build_w3_http()
                if w3 is None:
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    continue
                contract = _get_contract(w3)

            current_block = w3.eth.block_number

            if current_block <= last_block:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            from_block = last_block + 1
            to_block   = current_block

            # Fetch logs with "to == platform_wallet" filter
            try:
                events = contract.events.Transfer.get_logs(
                    from_block=from_block,
                    to_block=to_block,
                    argument_filters={"to": platform_checksum},
                )
            except Exception as exc:
                logger.warning(
                    "get_logs(%d→%d) failed: %s. Will retry next cycle.",
                    from_block, to_block, exc,
                )
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            for event in events:
                try:
                    # Belt-and-braces: verify emitted from CCT contract
                    if Web3.to_checksum_address(event["address"]) != contract_checksum:
                        continue

                    args      = event["args"]
                    sender    = args["from"]
                    raw_value = int(args["value"])
                    tx_hash   = event["transactionHash"].hex()

                    logger.info(
                        "Transfer event (poll) | from=%s | amount=%s wei | tx=%s",
                        sender, raw_value, tx_hash,
                    )
                    _safe_dispatch(sender, raw_value, tx_hash)

                except Exception as exc:
                    logger.warning("Error parsing poll event: %s", exc)

            last_block = to_block

            # Retry any transactions that previously had insufficient confirmations
            _flush_pending_retries()

        except Exception as exc:
            logger.error("HTTP poll cycle error: %s", exc)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ════════════════════════════════════════════════════════
#  Public entry point — called from FastAPI lifespan
# ════════════════════════════════════════════════════════

async def start_listener() -> None:
    """
    Start the blockchain event listener as a long-running async task.

    Selection logic:
      - If WS_RPC_URL is set   → WebSocket mode (with HTTP fallback on failure)
      - Otherwise              → HTTP polling mode

    This coroutine never returns under normal operation.
    """
    if not _validate_config():
        logger.warning("Blockchain listener NOT started due to missing configuration.")
        return

    if WS_RPC_URL:
        logger.info("Starting blockchain listener in WebSocket mode.")
        await _listen_websocket()
    else:
        logger.info(
            "WS_RPC_URL not set — starting blockchain listener in HTTP polling mode."
        )
        await _listen_http_poll()