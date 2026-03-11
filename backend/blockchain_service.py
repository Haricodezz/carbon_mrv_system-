import os
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

PRIVATE_KEY = os.getenv("PRIVATE_KEY")
CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS")
RPC_URL = os.getenv("RPC_URL")

# Validate env vars on startup
if not PRIVATE_KEY:
    raise EnvironmentError("PRIVATE_KEY not set in .env")
if not CONTRACT_ADDRESS:
    raise EnvironmentError("CONTRACT_ADDRESS not set in .env")
if not RPC_URL:
    raise EnvironmentError("RPC_URL not set in .env")

w3 = Web3(Web3.HTTPProvider(RPC_URL))

if not w3.is_connected():
    raise ConnectionError(f"Cannot connect to RPC: {RPC_URL}")

account = w3.eth.account.from_key(PRIVATE_KEY)

# Minimal ABI — only the mint function is needed
CONTRACT_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "to",     "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"}
        ],
        "name": "mint",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

contract = w3.eth.contract(
    address=Web3.to_checksum_address(CONTRACT_ADDRESS),
    abi=CONTRACT_ABI
)


def mint_tokens(to_address: str, amount: float) -> str:
    """
    Mint `amount` carbon credit tokens (18-decimal ERC-20) to `to_address`.
    Returns the transaction hash as a hex string.
    """
    # Convert float credits → integer with 18 decimals (ERC-20 standard)
    amount_int = int(amount * (10 ** 18))

    nonce = w3.eth.get_transaction_count(account.address)

    tx = contract.functions.mint(
        Web3.to_checksum_address(to_address),
        amount_int
    ).build_transaction({
        "chainId":  80002,          # Polygon Amoy testnet
        "gas":      200_000,
        "gasPrice": w3.to_wei("30", "gwei"),
        "nonce":    nonce,
    })

    signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash   = w3.eth.send_raw_transaction(signed_tx.raw_transaction)

    return w3.to_hex(tx_hash)


# ── Transfer ABI (ERC-20 standard transfer) ───────────────
TRANSFER_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "to",     "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"}
        ],
        "name":            "transfer",
        "outputs":         [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type":            "function"
    }
]

transfer_contract = w3.eth.contract(
    address=Web3.to_checksum_address(CONTRACT_ADDRESS),
    abi=TRANSFER_ABI
)


def transfer_tokens(from_address: str, to_address: str, amount: float) -> str:
    """
    Transfer CCT tokens from platform escrow wallet to buyer wallet.
    from_address must be the platform wallet (signs with PRIVATE_KEY).
    """
    amount_int = int(amount * (10 ** 18))
    nonce      = w3.eth.get_transaction_count(account.address)

    tx = transfer_contract.functions.transfer(
        Web3.to_checksum_address(to_address),
        amount_int
    ).build_transaction({
        "chainId":  80002,
        "gas":      200_000,
        "gasPrice": w3.to_wei("30", "gwei"),
        "nonce":    nonce,
    })

    signed_tx = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash   = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    return w3.to_hex(tx_hash)


# ── Transfer Event ABI (for decoding on-chain logs) ───────
TRANSFER_EVENT_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True,  "internalType": "address", "name": "from",  "type": "address"},
            {"indexed": True,  "internalType": "address", "name": "to",    "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "value", "type": "uint256"}
        ],
        "name": "Transfer",
        "type": "event"
    }
]

verify_contract = w3.eth.contract(
    address=Web3.to_checksum_address(CONTRACT_ADDRESS),
    abi=TRANSFER_EVENT_ABI
)


def verify_deposit_transaction(
    tx_hash:         str,
    expected_from:   str,
    expected_to:     str,
    expected_amount: float
) -> bool:
    """
    Verify a seller's CCT deposit transaction ON-CHAIN before approving listing.

    Checks (in order):
        1. Transaction exists on blockchain
        2. Transaction receipt status == 1 (not reverted)
        3. Transaction interacted with CCT contract address
        4. A Transfer event was emitted
        5. Transfer.from  == seller wallet address
        6. Transfer.to    == platform escrow wallet
        7. Transfer.value == expected_amount (18 decimal tolerance ±0.001)

    Returns:
        True if all checks pass

    Raises:
        Exception with clear message describing exactly which check failed
    """

    # ── CHECK 1: Transaction exists ───────────────────────────
    try:
        tx = w3.eth.get_transaction(tx_hash)
    except Exception:
        raise Exception(
            f"Transaction {tx_hash} not found on blockchain. "
            f"Ensure the seller submitted a valid transaction hash."
        )

    if tx is None:
        raise Exception(f"Transaction {tx_hash} does not exist on chain.")

    # ── CHECK 2: Receipt status == 1 (not reverted) ───────────
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except Exception:
        raise Exception(
            f"Could not fetch receipt for {tx_hash}. "
            f"Transaction may still be pending — try again shortly."
        )

    if receipt is None:
        raise Exception(
            f"Transaction {tx_hash} receipt not available yet. "
            f"It may still be pending confirmation."
        )

    if receipt.status != 1:
        raise Exception(
            f"Transaction {tx_hash} was REVERTED on-chain (status=0). "
            f"The transfer failed — seller must resubmit a new deposit."
        )

    # ── CHECK 3: Interacted with CCT contract ─────────────────
    contract_addr = Web3.to_checksum_address(CONTRACT_ADDRESS)
    tx_to = receipt.get("to") or receipt.get("contractAddress")
    if tx_to is None or Web3.to_checksum_address(tx_to) != contract_addr:
        raise Exception(
            f"Transaction {tx_hash} did not interact with the CCT contract "
            f"({CONTRACT_ADDRESS}). Seller may have used the wrong contract."
        )

    # ── CHECK 4: Transfer event was emitted ───────────────────
    try:
        logs = verify_contract.events.Transfer().process_receipt(receipt)
    except Exception as e:
        raise Exception(
            f"Could not decode Transfer event from transaction {tx_hash}: {e}"
        )

    if not logs:
        raise Exception(
            f"No Transfer event found in transaction {tx_hash}. "
            f"Seller may have called the wrong function."
        )

    # ── CHECKS 5, 6, 7: Validate from / to / amount ──────────
    norm_expected_from  = Web3.to_checksum_address(expected_from)
    norm_expected_to    = Web3.to_checksum_address(expected_to)
    expected_amount_int = int(expected_amount * (10 ** 18))
    tolerance           = int(0.001 * (10 ** 18))   # ±0.001 CCT float tolerance

    matching_event = None
    for event in logs:
        event_from  = Web3.to_checksum_address(event["args"]["from"])
        event_to    = Web3.to_checksum_address(event["args"]["to"])
        event_value = int(event["args"]["value"])

        from_ok   = event_from  == norm_expected_from
        to_ok     = event_to    == norm_expected_to
        amount_ok = abs(event_value - expected_amount_int) <= tolerance

        if from_ok and to_ok and amount_ok:
            matching_event = event
            break

    if matching_event is None:
        # Build detailed error showing found vs expected
        found_events = [
            f"from={e['args']['from']} "
            f"to={e['args']['to']} "
            f"amount={e['args']['value'] / (10**18):.6f} CCT"
            for e in logs
        ]
        found_str = " | ".join(found_events) if found_events else "none"

        raise Exception(
            f"Transfer event mismatch in transaction {tx_hash}.\n"
            f"Expected: from={expected_from} "
            f"to={expected_to} "
            f"amount={expected_amount:.6f} CCT\n"
            f"Found:    {found_str}"
        )

    return True