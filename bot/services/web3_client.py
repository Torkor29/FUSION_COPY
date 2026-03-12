"""Web3 client for Polygon — USDC transfers, approvals, and wallet operations."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from bot.config import settings

logger = logging.getLogger(__name__)

MAX_UINT256 = 2**256 - 1
APPROVAL_THRESHOLD = 10**12  # re-approve when below 1M USDC worth of allowance

# USDC on Polygon
# Native USDC (Circle) — utilisé par Polymarket et le bot
USDC_POLYGON_ADDRESS = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
USDC_POLYGON_DECIMALS = 6

# Ancien USDC bridgé (USDC.e) — certains bridges l'utilisent encore
USDC_E_POLYGON_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Polymarket exchange contracts on Polygon
CTF_EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE_ADDRESS = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEG_RISK_ADAPTER_ADDRESS = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]

# Polygon RPC endpoints (public, fallback list)
# On privilégie d'abord un RPC dédié configuré dans l'env,
# puis quelques endpoints publics connus. Le premier de la
# liste sera utilisé par défaut.
POLYGON_RPC_URLS = [
    settings.polygon_rpc_url or "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon-mainnet.g.alchemy.com/v2/demo",
]


@dataclass
class TransferResult:
    success: bool
    tx_hash: Optional[str] = None
    error: Optional[str] = None
    gas_used: int = 0


def usdc_to_wei(amount: float) -> int:
    """Convert USDC amount (float) to smallest unit (6 decimals)."""
    return int(amount * 10**USDC_POLYGON_DECIMALS)


def wei_to_usdc(amount_wei: int) -> float:
    """Convert smallest unit back to USDC float."""
    return amount_wei / 10**USDC_POLYGON_DECIMALS


class PolygonClient:
    """Web3 client for Polygon network operations."""

    def __init__(self, rpc_url: Optional[str] = None):
        # Permettre de surcharger l'URL via l'argument ou la config.
        self._rpc_url = rpc_url or POLYGON_RPC_URLS[0]
        self._w3 = None
        self._usdc_contract = None
        self._usdc_e_contract = None

    def _get_web3(self):
        """Lazy-init Web3 connection."""
        if self._w3 is None:
            from web3 import Web3
            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))
            self._usdc_contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(USDC_POLYGON_ADDRESS),
                abi=ERC20_ABI,
            )
            self._usdc_e_contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(USDC_E_POLYGON_ADDRESS),
                abi=ERC20_ABI,
            )
        return self._w3

    async def get_usdc_balance(self, wallet_address: str) -> float:
        """Get native USDC balance for a wallet on Polygon (spendable par le bot)."""
        try:
            w3 = self._get_web3()
            balance_wei = await asyncio.to_thread(
                self._usdc_contract.functions.balanceOf(
                    w3.to_checksum_address(wallet_address)
                ).call
            )
            return wei_to_usdc(balance_wei)
        except Exception as e:
            logger.error(f"Failed to get USDC balance for {wallet_address[:10]}...: {e}")
            return 0.0

    async def get_usdc_balances(
        self, wallet_address: str
    ) -> tuple[float, float]:
        """Return (native USDC, USDC.e) balances for a wallet.

        Utile pour l'affichage utilisateur. Seul le solde natif est utilisable
        pour le trading / les retraits dans le bot.
        """
        try:
            w3 = self._get_web3()
            addr = w3.to_checksum_address(wallet_address)

            async def _balance(contract) -> int:
                return await asyncio.to_thread(contract.functions.balanceOf(addr).call)

            native_wei = await _balance(self._usdc_contract)
            legacy_wei = await _balance(self._usdc_e_contract)
            return wei_to_usdc(native_wei), wei_to_usdc(legacy_wei)
        except Exception as e:
            logger.error(
                f"Failed to get detailed USDC balances for {wallet_address[:10]}...: {e}"
            )
            return 0.0, 0.0

    async def get_matic_balance(self, wallet_address: str) -> float:
        """Get MATIC (POL) balance for gas fees."""
        try:
            w3 = self._get_web3()
            balance = await asyncio.to_thread(
                w3.eth.get_balance,
                w3.to_checksum_address(wallet_address),
            )
            return w3.from_wei(balance, "ether")
        except Exception as e:
            logger.error(f"Failed to get MATIC balance: {e}")
            return 0.0

    async def check_usdc_allowance(self, wallet_address: str, spender: str) -> int:
        """Check current USDC allowance granted to a spender."""
        try:
            w3 = self._get_web3()
            allowance = await asyncio.to_thread(
                self._usdc_contract.functions.allowance(
                    w3.to_checksum_address(wallet_address),
                    w3.to_checksum_address(spender),
                ).call
            )
            return allowance
        except Exception as e:
            logger.error(f"Failed to check allowance: {e}")
            return 0

    async def approve_usdc(
        self,
        wallet_address: str,
        spender: str,
        private_key: str,
        amount: int = MAX_UINT256,
    ) -> TransferResult:
        """Approve a spender to use USDC on behalf of wallet_address."""
        try:
            w3 = self._get_web3()
            from_addr = w3.to_checksum_address(wallet_address)
            spender_addr = w3.to_checksum_address(spender)

            def _send_approval():
                nonce = w3.eth.get_transaction_count(from_addr)
                tx = self._usdc_contract.functions.approve(
                    spender_addr, amount
                ).build_transaction(
                    {
                        "from": from_addr,
                        "nonce": nonce,
                        "gas": 80_000,
                        "maxFeePerGas": w3.eth.gas_price * 2,
                        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
                        "chainId": 137,
                    }
                )
                signed = w3.eth.account.sign_transaction(tx, private_key)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                return tx_hash, receipt

            tx_hash, receipt = await asyncio.to_thread(_send_approval)

            if receipt["status"] == 1:
                tx_hex = tx_hash.hex()
                logger.info(
                    f"USDC approval set for spender {spender[:10]}... "
                    f"by {wallet_address[:10]}... tx: {tx_hex}"
                )
                return TransferResult(
                    success=True, tx_hash=tx_hex, gas_used=receipt["gasUsed"]
                )
            tx_hex = tx_hash.hex()
            return TransferResult(
                success=False, tx_hash=tx_hex, error="Approval tx reverted"
            )
        except Exception as e:
            logger.error(f"USDC approval failed: {e}")
            return TransferResult(success=False, error=str(e))

    async def ensure_polymarket_approvals(
        self, wallet_address: str, private_key: str
    ) -> bool:
        """Approve USDC for all Polymarket exchange contracts if needed.

        Returns True if all approvals are in place, False on failure.
        """
        spenders = [
            CTF_EXCHANGE_ADDRESS,
            NEG_RISK_CTF_EXCHANGE_ADDRESS,
            NEG_RISK_ADAPTER_ADDRESS,
        ]

        for spender in spenders:
            allowance = await self.check_usdc_allowance(wallet_address, spender)
            if allowance >= APPROVAL_THRESHOLD:
                continue

            logger.info(
                f"Setting USDC approval for {spender[:10]}... "
                f"(current allowance: {allowance})"
            )
            result = await self.approve_usdc(wallet_address, spender, private_key)
            if not result.success:
                logger.error(f"Approval failed for {spender}: {result.error}")
                return False

        return True

    async def transfer_usdc(
        self,
        from_address: str,
        to_address: str,
        amount_usdc: float,
        private_key: str,
    ) -> TransferResult:
        """Transfer USDC on Polygon from one address to another.

        Args:
            from_address: Sender wallet address.
            to_address: Recipient wallet address.
            amount_usdc: Amount in USDC.
            private_key: Sender's decrypted private key.

        Returns:
            TransferResult with tx_hash on success.
        """
        try:
            w3 = self._get_web3()
            from_addr = w3.to_checksum_address(from_address)
            to_addr = w3.to_checksum_address(to_address)
            amount_wei = usdc_to_wei(amount_usdc)

            def _send_transfer():
                nonce = w3.eth.get_transaction_count(from_addr)
                tx = self._usdc_contract.functions.transfer(
                    to_addr, amount_wei
                ).build_transaction(
                    {
                        "from": from_addr,
                        "nonce": nonce,
                        "gas": 100_000,
                        "maxFeePerGas": w3.eth.gas_price * 2,
                        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
                        "chainId": 137,  # Polygon mainnet
                    }
                )
                signed = w3.eth.account.sign_transaction(tx, private_key)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                return tx_hash, receipt

            tx_hash, receipt = await asyncio.to_thread(_send_transfer)
            tx_hash_hex = tx_hash.hex()

            if receipt["status"] == 1:
                logger.info(
                    f"USDC transfer successful: {amount_usdc} USDC "
                    f"from {from_address[:10]}... to {to_address[:10]}... "
                    f"tx: {tx_hash_hex}"
                )
                return TransferResult(
                    success=True,
                    tx_hash=tx_hash_hex,
                    gas_used=receipt["gasUsed"],
                )
            else:
                return TransferResult(
                    success=False,
                    tx_hash=tx_hash_hex,
                    error="Transaction reverted on-chain",
                )

        except Exception as e:
            logger.error(f"USDC transfer failed: {e}")
            return TransferResult(success=False, error=str(e))


# Singleton
polygon_client = PolygonClient()
