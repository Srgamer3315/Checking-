# main.py
"""
Telegram bot: checks BNB/ETH/USDT/BTC/TON addresses.
Robust: handles web3 v5/v6 checksum API differences,
graceful failures (RPC down), and clear logging for Render.
"""

import os
import re
import logging
import asyncio
from functools import partial

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# Try import Web3; will fail if build did not install dependencies.
try:
    from web3 import Web3
    WEB3_AVAILABLE = True
except Exception as e:
    Web3 = None
    WEB3_AVAILABLE = False

# ---------------- CONFIG (via env) ----------------
TOKEN = os.environ.get("TOKEN")
BSC_RPC = os.environ.get("BSC_RPC", "https://bsc-dataseed.binance.org/")
ETH_RPC = os.environ.get("ETH_RPC", "https://rpc.ankr.com/eth")
USDT_BSC_CONTRACT = os.environ.get(
    "USDT_BSC_CONTRACT", "0x55d398326f99059fF775485246999027B3197955"
)
BTC_API_BASE = os.environ.get("BTC_API_BASE", "https://blockstream.info/api/address/")
TON_API_BASE = os.environ.get("TON_API_BASE", "https://toncenter.com/api/v2/getAddressBalance?address=")
# --------------------------------------------------

# Basic checks
if not TOKEN:
    raise SystemExit("Error: TOKEN environment variable is required. Set it in Render secrets.")

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("address-checker-bot")

# Regex address patterns
RE_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
RE_BTC = re.compile(r"^(bc1|[13])[a-zA-HJ-NP-Z0-9]{25,39}$")
RE_TON = re.compile(r"^(EQ|UQ)[A-Za-z0-9_-]{46}$")

# user selection state
user_choice = {}

# ---------------- web3 helper (v5/v6 compatible) ----------------
def to_checksum(addr: str) -> str:
    if not WEB3_AVAILABLE:
        raise RuntimeError("web3 library not available")
    try:
        return Web3.toChecksumAddress(addr)  # v5 naming
    except AttributeError:
        # v6 naming
        return Web3.to_checksum_address(addr)
# ---------------------------------------------------------------

# ---------------- sync blockchain checkers (run in threadpool) ----------------
def sync_check_rpc_native(address: str, rpc_url: str):
    """Check native coin (BNB/ETH): return (ok:bool, message:str)"""
    if not WEB3_AVAILABLE:
        return False, "web3 not installed in environment"
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
    except Exception as e:
        return False, f"web3 provider init error: {e}"

    try:
        connected = False
        try:
            connected = w3.is_connected()
        except Exception:
            # older name fallback
            try:
                connected = w3.isConnected()
            except Exception:
                connected = False
        if not connected:
            return False, "Cannot connect to RPC node"

        cs_addr = to_checksum(address)
        balance = w3.eth.get_balance(cs_addr)
        txcount = w3.eth.get_transaction_count(cs_addr)
        # Format balances nicely
        return True, f"Balance: {balance / 1e18:.6f} (native), txs: {txcount}"
    except Exception as e:
        return False, f"RPC query error: {e}"

def sync_check_rpc_token(address: str, rpc_url: str, token_contract: str):
    """Check ERC/BEP20 token balance (USDT on BSC)"""
    if not WEB3_AVAILABLE:
        return False, "web3 not installed in environment"
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
    except Exception as e:
        return False, f"web3 provider init error: {e}"

    try:
        connected = False
        try:
            connected = w3.is_connected()
        except Exception:
            try:
                connected = w3.isConnected()
            except Exception:
                connected = False
        if not connected:
            return False, "Cannot connect to RPC node"

        cs_addr = to_checksum(address)
        cs_token = to_checksum(token_contract)
        # minimal ABI for balanceOf
        abi = [
            {
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function",
            }
        ]
        token = w3.eth.contract(address=cs_token, abi=abi)
        bal = token.functions.balanceOf(cs_addr).call()
        # USDT on BSC has 18 or 6 decimals depending on token; for our BSC USDT it's 18? (commonly 18)
        # We'll return raw and let user interpret; but format assuming 18 decimals for display.
        return True, f"Token balance (raw): {bal}  (display approx: {bal / 1e18:.6f})"
    except Exception as e:
        return False, f"Token query error: {e}"

def sync_check_btc(address: str):
    try:
        r = requests.get(BTC_API_BASE + address, timeout=10)
        if r.status_code != 200:
            return False, f"BTC API HTTP {r.status_code}"
        j = r.json()
        cs = j.get("chain_stats", {})
        funded = cs.get("funded_txo_sum", 0)
        spent = cs.get("spent_txo_sum", 0)
        bal = funded - spent
        return True, f"Balance: {bal / 1e8:.8f} BTC"
    except Exception as e:
        return False, f"BTC API error: {e}"

def sync_check_ton(address: str):
    try:
        r = requests.get(TON_API_BASE + address, timeout=10)
        if r.status_code != 200:
            return False, f"TON API HTTP {r.status_code}"
        j = r.json()
        # toncenter returns {"ok":true,"result":<nanotons>} or similar for this endpoint
        result = j.get("result")
        if result is None:
            return False, f"TON API result missing"
        nanotons = int(result)
        return True, f"Balance: {nanotons / 1e9:.9f} TON"
    except Exception as e:
        return False, f"TON API error: {e}"
# ---------------------------------------------------------------------------------

# async wrappers (run sync functions on threadpool)
async def run_in_executor(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args))

# ---------------- Telegram handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("BNB", callback_data="BNB")],
        [InlineKeyboardButton("ETH", callback_data="ETH")],
        [InlineKeyboardButton("USDT (BSC)", callback_data="USDT")],
        [InlineKeyboardButton("BTC", callback_data="BTC")],
        [InlineKeyboardButton("TON", callback_data="TON")],
    ]
    await update.message.reply_text("Select the token you want to check:", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    token = query.data
    user_choice[query.from_user.id] = token
    await query.message.reply_text(f"Send me the {token} address for checking.")

async def handle_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid not in user_choice:
        await update.message.reply_text("Please use /start first and select a token.")
        return
    token = user_choice[uid]
    address = update.message.text.strip()
    logger.info("User %s requested %s check for address: %s", uid, token, address)

    # Basic format checks
    if token in ("BNB", "ETH", "USDT") and not RE_EVM.fullmatch(address):
        await update.message.reply_text("❌ Invalid EVM-style address format (must be 0x...).")
        return
    if token == "BTC" and not RE_BTC.fullmatch(address):
        await update.message.reply_text("❌ Invalid BTC address format.")
        return
    if token == "TON" and not RE_TON.fullmatch(address):
        await update.message.reply_text("❌ Invalid TON address format.")
        return

    await update.message.reply_text("🔍 Checking... This may take a second.")

    # Perform checks
    if token == "BNB":
        ok, info = await run_in_executor(sync_check_rpc_native, address, BSC_RPC)
    elif token == "ETH":
        ok, info = await run_in_executor(sync_check_rpc_native, address, ETH_RPC)
    elif token == "USDT":
        ok, info = await run_in_executor(sync_check_rpc_token, address, BSC_RPC, USDT_BSC_CONTRACT)
    elif token == "BTC":
        ok, info = await run_in_executor(sync_check_btc, address)
    elif token == "TON":
        ok, info = await run_in_executor(sync_check_ton, address)
    else:
        ok, info = False, "Unknown token selection."

    await update.message.reply_text(f"{'✅' if ok else '❌'} {info}")

# ---------------- main ----------------
if __name__ == "__main__":
    logger.info("Starting bot. web3 available: %s", WEB3_AVAILABLE)
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address))
    app.run_polling()
