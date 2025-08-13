import os
import re
import logging
import asyncio
import requests
from functools import partial
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from web3 import Web3

# ---------------- CONFIG ----------------
TOKEN = os.environ.get("TOKEN")  # Telegram bot token from environment
BSC_RPC = os.environ.get("BSC_RPC", "https://bsc-dataseed.binance.org/")
ETH_RPC = os.environ.get("ETH_RPC", "https://rpc.ankr.com/eth")
USDT_BSC_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"  # USDT on BSC
# -----------------------------------------

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Patterns for address validation
RE_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
RE_BTC = re.compile(r"^(bc1|[13])[a-zA-HJ-NP-Z0-9]{25,39}$")
RE_TON = re.compile(r"^(EQ|UQ)[A-Za-z0-9_-]{46}$")

# Store user's last token choice
user_choice = {}

def to_checksum(addr: str):
    try:
        return Web3.toChecksumAddress(addr)  # web3 v5
    except AttributeError:
        return Web3.to_checksum_address(addr)  # web3 v6

# -------- Blockchain Balance Checkers --------
def sync_check_rpc(address: str, rpc_url: str, contract: str = None):
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        if not (w3.is_connected() if hasattr(w3, "is_connected") else w3.isConnected()):
            return False, "❌ Cannot connect to RPC."

        checksum_addr = to_checksum(address)
        if contract:
            contract_addr = to_checksum(contract)
            abi = [{
                "constant": True,
                "inputs": [{"name": "_owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "balance", "type": "uint256"}],
                "type": "function"
            }]
            token_contract = w3.eth.contract(address=contract_addr, abi=abi)
            balance = token_contract.functions.balanceOf(checksum_addr).call()
            return (True, f"Balance: {balance / 1e18} tokens")
        else:
            balance = w3.eth.get_balance(checksum_addr)
            txcount = w3.eth.get_transaction_count(checksum_addr)
            return (True, f"Balance: {balance / 1e18} coins, txs: {txcount}")
    except Exception as e:
        return False, f"Error: {e}"

async def check_rpc_async(address: str, rpc_url: str, contract: str = None):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(sync_check_rpc, address, rpc_url, contract))

def check_btc_balance(address: str):
    try:
        r = requests.get(f"https://blockstream.info/api/address/{address}")
        if r.status_code != 200:
            return False, "❌ Error fetching BTC data."
        data = r.json()
        satoshis = data.get("chain_stats", {}).get("funded_txo_sum", 0) - data.get("chain_stats", {}).get("spent_txo_sum", 0)
        return True, f"Balance: {satoshis / 1e8} BTC"
    except Exception as e:
        return False, f"Error: {e}"

def check_ton_balance(address: str):
    try:
        r = requests.get(f"https://toncenter.com/api/v2/getAddressBalance?address={address}")
        if r.status_code != 200:
            return False, "❌ Error fetching TON data."
        data = r.json()
        balance = int(data.get("result", 0))
        return True, f"Balance: {balance / 1e9} TON"
    except Exception as e:
        return False, f"Error: {e}"
# ---------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("BNB", callback_data="BNB")],
        [InlineKeyboardButton("ETH", callback_data="ETH")],
        [InlineKeyboardButton("USDT (BSC)", callback_data="USDT")],
        [InlineKeyboardButton("BTC", callback_data="BTC")],
        [InlineKeyboardButton("TON", callback_data="TON")],
    ]
    await update.message.reply_text(
        "Select the token you want to check:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    token = query.data
    user_choice[query.from_user.id] = token
    await query.message.reply_text(f"Send me the {token} address for checking.")

async def handle_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid not in user_choice:
        await update.message.reply_text("Please start with /start and select a token first.")
        return

    token = user_choice[uid]
    address = update.message.text.strip()

    if token in ("BNB", "ETH", "USDT") and not RE_EVM.fullmatch(address):
        await update.message.reply_text("❌ Invalid EVM address format.")
        return
    if token == "BTC" and not RE_BTC.fullmatch(address):
        await update.message.reply_text("❌ Invalid BTC address format.")
        return
    if token == "TON" and not RE_TON.fullmatch(address):
        await update.message.reply_text("❌ Invalid TON address format.")
        return

    await update.message.reply_text(f"🔍 Checking {token} address...")

    if token == "BNB":
        ok, info = await check_rpc_async(address, BSC_RPC)
    elif token == "ETH":
        ok, info = await check_rpc_async(address, ETH_RPC)
    elif token == "USDT":
        ok, info = await check_rpc_async(address, BSC_RPC, USDT_BSC_CONTRACT)
    elif token == "BTC":
        ok, info = check_btc_balance(address)
    elif token == "TON":
        ok, info = check_ton_balance(address)

    await update.message.reply_text(f"{'✅' if ok else '❌'} {info}")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_address))
    logger.info("Bot started...")
    app.run_polling()
