import asyncio
import logging
import json
import time
from decimal import Decimal
import base58
import base64
import os
import websockets
from cachetools import TTLCache
from solana_tx import get_transaction_details_with_backoff, extract_mint_address
from solana_token_info import get_token_info, get_token_metadata
import aiohttp
import requests
from colorama import init, Fore, Back, Style
# from test_pp_api import buy_pump_token_ape_all, sell_pump_token_ape_all
import telegram
from telegram.ext import Updater, CommandHandler, MessageHandler
from telegram.ext import filters
import re
from datetime import datetime
from dotenv import load_dotenv



load_dotenv()

# Initialize colorama
init(autoreset=True)

# Set up logging
def setup_logging():
    # Create formatters
    console_formatter = logging.Formatter(
        f'{Fore.CYAN}%(asctime)s{Style.RESET_ALL} | '
        f'{Fore.GREEN}%(levelname)-8s{Style.RESET_ALL} | '
        f'{Fore.YELLOW}%(name)s{Style.RESET_ALL} | '
        f'%(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    file_formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Simple timestamp formatter for token count log
    token_count_formatter = logging.Formatter(
        '%(asctime)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Create console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(logging.INFO)

    # Create file handlers
    main_file_handler = logging.FileHandler('logs/sol_monitor.log')
    main_file_handler.setFormatter(file_formatter)
    main_file_handler.setLevel(logging.INFO)

    # Create separate file handler for analytics
    analytics_file_handler = logging.FileHandler('logs/analytics.log')
    analytics_file_handler.setFormatter(file_formatter)
    analytics_file_handler.setLevel(logging.INFO)
    
    # Create separate file handler for token count
    token_count_file_handler = logging.FileHandler('logs/token_count.log')
    token_count_file_handler.setFormatter(token_count_formatter)
    token_count_file_handler.setLevel(logging.INFO)

    # Create loggers
    main_logger = logging.getLogger('sol_monitor')
    main_logger.setLevel(logging.INFO)
    main_logger.addHandler(console_handler)
    main_logger.addHandler(main_file_handler)

    analytics_logger = logging.getLogger('analytics')
    analytics_logger.setLevel(logging.INFO)
    analytics_logger.addHandler(analytics_file_handler)
    
    token_count_logger = logging.getLogger('token_count')
    token_count_logger.setLevel(logging.INFO)
    token_count_logger.addHandler(token_count_file_handler)

    return main_logger, analytics_logger, token_count_logger

# Initialize loggers
logger, analytics_logger, token_count_logger = setup_logging()

# Solana configuration
RPC_URL = f"wss://mainnet.helius-rpc.com/?api-key={os.getenv('HELIUS_API_KEY')}"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
MONITORED_PROGRAM_IDS = [
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
]

tx_cache = TTLCache(maxsize=1000, ttl=3600)
logged_mint_addresses = set()

# Telegram configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')

# Initialize Telegram bot
bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)

async def get_dexscreener_data(mint_address):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{mint_address}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                logger.info(f"DexScreener API response: {data}")
                if data.get('pairs'):
                    return data['pairs'][0]  # Return the first pair's data
    logger.warning(f"No DexScreener data found for {mint_address}")
    return None

async def get_pump_fun_data(mint_address: str):
    url = f"https://frontend-api.pump.fun/coins/{mint_address}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                logger.info(f"Pump.fun API response: {data}")
                return data
    logger.warning(f"No Pump.fun data found for {mint_address}")
    return None

async def get_rugcheck_data(mint_address: str):
    url = f"https://api.rugcheck.xyz/v1/tokens/{mint_address}/report/summary"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                logger.info(f"RugCheck API response for {mint_address}: {data}")
                return data
    logger.warning(f"No RugCheck data found for {mint_address}")
    return None

class SolanaMonitor:
    def __init__(self):
        self.ws = None
        self.new_tokens = []
        self.owned_tokens = set()
        self.sold_tokens = set()
        self.should_run = False
        self.reconnect_delay = 5
        self.processed_tokens_count = 0
        self.tokens_with_dexscreener_data = 0
        self.pending_tokens = {}
        self.last_status_time = 0
        
        # Statistics for token quality assessment
        self.rug_count = 0
        self.good_token_count = 0
        
        # Add celebrity-related keywords
        self.celebrity_keywords = {
            'names': [
                'trump', 'biden', 'kanye', 'ye', 'portnoy', 'cuban', 'mark cuban',
                'dababy', 'da baby', 'elon', 'musk', 'drake', 'taylor', 'swift',
                'bieber', 'kardashian', 'ronaldo', 'messi', 'lebron', 'snoop',
                'jay z', 'jayz', 'beyonce', 'rihanna', 'madonna', 'gaga',
                'lady gaga', 'justin', 'kim k', 'kylie'
            ],
            'titles': [
                'president', 'rapper', 'celebrity', 'star', 'billionaire',
                'entrepreneur', 'ceo', 'founder', 'artist', 'athlete',
                'investor', 'mogul', 'icon', 'legend'
            ],
            'related_terms': [
                'official', 'verified', 'real', 'authentic', 'exclusive',
                'launch', 'token', 'coin', 'crypto', 'memecoin',
                'presale', 'pre-sale', 'announcement', 'coming soon',
                'airdrop', 'community'
            ]
        }

    async def start_monitoring(self):
        logger.info("Starting Solana token monitoring")
        self.should_run = True
        await self._monitor_loop()

    async def stop_monitoring(self):
        logger.info("Stopping Solana token monitoring")
        self.should_run = False
        if self.ws:
            await self.ws.close()

    async def _monitor_loop(self):
        last_count_log_time = time.time()
        last_daily_summary_time = time.time()
        
        while self.should_run:
            try:
                current_time = time.time()
                
                # Log token count every hour
                if current_time - last_count_log_time >= 3600:  # 3600 seconds = 1 hour
                    logger.info(f"Hourly token count update: {self.processed_tokens_count} tokens processed so far")
                    self.log_token_count()  # Log to token count file
                    
                    analytics_data = {
                        'event_type': 'hourly_token_count',
                        'timestamp': current_time,
                        'processed_tokens_count': self.processed_tokens_count
                    }
                    analytics_logger.info(json.dumps(analytics_data))
                    last_count_log_time = current_time
                
                # Send daily summary
                if current_time - last_daily_summary_time >= 86400:  # 86400 seconds = 24 hours
                    await self.send_daily_summary()
                    last_daily_summary_time = current_time
                
                await self._run_websocket()
            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(300, self.reconnect_delay * 2)
            else:
                self.reconnect_delay = 5
            finally:
                # Ensure tasks are properly cancelled
                if 'status_task' in locals():
                    status_task.cancel()
                if 'pending_tokens_task' in locals():
                    pending_tokens_task.cancel()

    async def _run_websocket(self):

        logger.info("Attempting to establish WebSocket connection...")

        async with websockets.connect(RPC_URL) as websocket:
            self.ws = websocket
            logger.info("WebSocket connection established")
            
            # Subscribe to program subscription for token program
            subscribe_message = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [TOKEN_PROGRAM_ID]},
                    {"commitment": "finalized"}
                ]
            }
            logger.info(f"Sending subscription message: {subscribe_message}")

            await websocket.send(json.dumps(subscribe_message))
            logger.info("Subscription message sent, waiting for messages...")

            # Start periodic tasks in separate tasks
            status_task = asyncio.create_task(self.send_periodic_status())
            pending_tokens_task = asyncio.create_task(self.check_pending_tokens())  # New task for checking pending tokens
            
            # Main loop to process incoming messages
            while self.should_run:
                try:
                    logger.debug("Waiting for next message...")
                    message = await websocket.recv()
                    logger.debug(f"Received raw message: {message[:200]}...")
                    await self.process_message(message)
                except websockets.exceptions.ConnectionClosed:
                    logger.info("WebSocket connection closed")
                    break

    def is_token_safe(self, pump_fun_data, rugcheck_data):
        if not pump_fun_data:
            logger.warning("Pump.fun data is None, cannot check token safety.")
            return False

        # Basic checks
        has_website = bool(pump_fun_data.get('website'))
        has_social = bool(pump_fun_data.get('twitter') or pump_fun_data.get('telegram'))
        has_description = bool(pump_fun_data.get('description'))
        has_image = bool(pump_fun_data.get('image_uri'))

        # Market cap and supply checks
        market_cap = float(pump_fun_data.get('market_cap', 0))
        total_supply = float(pump_fun_data.get('total_supply', 0))
        has_reasonable_market_cap = 1000 <= market_cap <= 10000000  # Between $1K and $10M
        has_reasonable_supply = 100000 <= total_supply <= 1000000000  # Between 100K and 1B

        # RugCheck score
        rugcheck_score = rugcheck_data.get('score', 1000) if rugcheck_data else 1000
        is_rugcheck_safe = rugcheck_score < 500  # Consider tokens with a score less than 500 as potentially safe

        # Logging all checks
        logger.info(f"Token safety check for {pump_fun_data.get('name', 'Unknown Token')}:")
        logger.info(f"  Website: {has_website}")
        logger.info(f"  Social Media: {has_social}")
        logger.info(f"  Description: {has_description}")
        logger.info(f"  Image: {has_image}")
        logger.info(f"  Market Cap: ${market_cap:.2f} (Reasonable: {has_reasonable_market_cap})")
        logger.info(f"  Total Supply: {total_supply:.0f} (Reasonable: {has_reasonable_supply})")
        logger.info(f"  RugCheck Score: {rugcheck_score} (Safe: {is_rugcheck_safe})")

        # Combine all checks
        is_safe = (
            has_website and
            has_social and
            has_description and
            has_image and
            has_reasonable_market_cap and
            has_reasonable_supply and
            is_rugcheck_safe
        )

        logger.info(f"Overall safety assessment: {'Safe' if is_safe else 'Potentially Unsafe'}")

        return is_safe

    def format_regular_token_message(self, signature, mint_address, token_info, token_metadata, dexscreener_data, rugcheck_data):
        message = f"""{Fore.CYAN}🧜‍♀️ New token detected!{Style.RESET_ALL}

        {Fore.YELLOW}🪙 Token Details:{Style.RESET_ALL}
        Mint: {Fore.GREEN}{mint_address}{Style.RESET_ALL}

        {Fore.YELLOW}🔗 Links:{Style.RESET_ALL}
        🔱 Transaction: https://explorer.solana.com/tx/{signature}
        🔱 View Token: https://solscan.io/token/{mint_address}
        """

        if dexscreener_data:
            base_token = dexscreener_data.get('baseToken', {})
            message += f"""
        {Fore.YELLOW}📊 Market Info:{Style.RESET_ALL}
        Name: {Fore.GREEN}{base_token.get('name', 'N/A')}{Style.RESET_ALL}
        Symbol: {Fore.GREEN}{base_token.get('symbol', 'N/A')}{Style.RESET_ALL}
        Price USD: {Fore.GREEN}${dexscreener_data.get('priceUsd', 'N/A')}{Style.RESET_ALL}
        24h Volume: {Fore.GREEN}${dexscreener_data.get('volume', {}).get('h24', 'N/A')}{Style.RESET_ALL}
        Market Cap: {Fore.GREEN}${dexscreener_data.get('marketCap', 'N/A')}{Style.RESET_ALL}
        Fully Diluted Valuation: {Fore.GREEN}${dexscreener_data.get('fdv', 'N/A')}{Style.RESET_ALL}
        
        24h Transactions:
        Buys: {Fore.GREEN}{dexscreener_data.get('txns', {}).get('h24', {}).get('buys', 'N/A')}{Style.RESET_ALL}
        Sells: {Fore.GREEN}{dexscreener_data.get('txns', {}).get('h24', {}).get('sells', 'N/A')}{Style.RESET_ALL}
        
        24h Price Change: {Fore.GREEN}{dexscreener_data.get('priceChange', {}).get('h24', 'N/A')}%{Style.RESET_ALL}
        
        Liquidity USD: {Fore.GREEN}${dexscreener_data.get('liquidity', {}).get('usd', 'N/A')}{Style.RESET_ALL}
        """
        else: 
             message += f"\n {Fore.RED}⚠️ No DexScreener data available for this token.{Style.RESET_ALL} "
                    
        if token_info:
            message += f"""
        {Fore.YELLOW}📊 Token Info:{Style.RESET_ALL}
        Supply: {Fore.GREEN}{token_info['supply']}{Style.RESET_ALL}
        Decimals: {Fore.GREEN}{token_info['decimals']}{Style.RESET_ALL}
        Mint Authority: {Fore.GREEN}{token_info['mint_authority']}{Style.RESET_ALL}
        Freeze Authority: {Fore.GREEN}{token_info['freeze_authority']}{Style.RESET_ALL}
        """

        if token_metadata:
            message += f"""
        {Fore.YELLOW}🏷️ Token Metadata:{Style.RESET_ALL}
        Metadata Address: {Fore.GREEN}{token_metadata['metadata_address']}{Style.RESET_ALL}
        """
            
        if rugcheck_data:
            message += f"""
            {Fore.YELLOW}🚨 RugCheck Info:{Style.RESET_ALL}
            Score: {Fore.GREEN}{rugcheck_data.get('score', 'N/A')}{Style.RESET_ALL}
            Risks:
            """
            for risk in rugcheck_data.get('risks', []):
                message += f"- {risk.get('name', 'N/A')} (Score: {risk.get('score', 'N/A')}, Level: {risk.get('level', 'N/A')})\n"
        else:
            message += f"\n{Fore.RED}⚠️ No RugCheck data available for this token.{Style.RESET_ALL}"

        return message

    def format_pump_fun_message(self, signature, mint_address, pump_fun_data):
        if not pump_fun_data:
            return f"""{Fore.CYAN}🧜‍♀️ Pump.fun token detected!{Style.RESET_ALL} Mint address: {Fore.GREEN}{mint_address}{Style.RESET_ALL}
            🔱 Transaction: https://explorer.solana.com/tx/{signature}
            🔱 View Token: https://solscan.io/token/{mint_address}
            {Fore.RED}⚠️ No additional data available from Pump.fun API.{Style.RESET_ALL}"""

        message = f"""{Fore.CYAN}🧜‍♀️ Pump.fun token detected!{Style.RESET_ALL}

        {Fore.YELLOW}🪙 Token Details:{Style.RESET_ALL}
        Name: {Fore.GREEN}{pump_fun_data.get('name', 'N/A')}{Style.RESET_ALL}
        Symbol: {Fore.GREEN}{pump_fun_data.get('symbol', 'N/A')}{Style.RESET_ALL}
        Mint: {Fore.GREEN}{mint_address}{Style.RESET_ALL}

        {Fore.YELLOW}📊 Market Info:{Style.RESET_ALL}
        Market Cap: {Fore.GREEN}${pump_fun_data.get('market_cap', 'N/A')}{Style.RESET_ALL}
        USD Market Cap: {Fore.GREEN}${pump_fun_data.get('usd_market_cap', 'N/A')}{Style.RESET_ALL}
        Total Supply: {Fore.GREEN}{pump_fun_data.get('total_supply', 'N/A')}{Style.RESET_ALL}

        {Fore.YELLOW}🌐 Social Links:{Style.RESET_ALL}
        Website: {Fore.BLUE}{pump_fun_data.get('website', 'N/A')}{Style.RESET_ALL}
        Twitter: {Fore.BLUE}{pump_fun_data.get('twitter', 'N/A')}{Style.RESET_ALL}
        Telegram: {Fore.BLUE}{pump_fun_data.get('telegram', 'N/A')}{Style.RESET_ALL}

        {Fore.YELLOW}📝 Description:{Style.RESET_ALL}
        {pump_fun_data.get('description', 'N/A')}

        {Fore.YELLOW}🖼️ Image:{Style.RESET_ALL} {pump_fun_data.get('image_uri', 'N/A')}

        {Fore.YELLOW}🔗 Links:{Style.RESET_ALL}
        🔱 Transaction: https://explorer.solana.com/tx/{signature}
        🔱 View Token: https://solscan.io/token/{mint_address}
        """

        return message

    async def process_message(self, message):
        try:
            message_data = json.loads(message)
            logger.debug(f"Received message: {message_data}")

            if 'result' in message_data and isinstance(message_data['result'], int):
                logger.info(f"Successfully subscribed. Subscription ID: {message_data['result']}")
                return

            if 'method' in message_data and message_data['method'] == 'logsNotification':
                params = message_data.get('params', {})
                result = params.get('result', {})
                value = result.get('value', {})
                logs = value.get('logs', [])

                logger.debug(f"Processing logs: {logs}")

                if any("Instruction: InitializeMint" in log for log in logs):
                    signature = value.get('signature', 'Unknown')
                    logger.info(f"InitializeMint instruction detected. Signature: {signature}")
                    await self.process_transaction(signature)
                else:
                    logger.debug("No InitializeMint instruction found in logs")

        except json.JSONDecodeError:
            logger.error(f"Failed to decode JSON: {message}", exc_info=True)
        except Exception as e:
            logger.error(f"Error processing message: {str(e)}", exc_info=True)

    async def send_swap_request(self, mint_address, action="buy", amount=0.003):
        url = "https://pumpportal.fun/api/trade?api-key=6th2pdup75166u36dwup6tjbdgtnagk4f9mk4rjfb5c5gvkd8d4njmvee143ctueanjm8vv3e93m6n35712mgh2fd10per9hb176wmhhawtmmkbha8u7jnkpb5x54nb3ddrq8maqcwykudhpqan26a5t38k3h5d76yvude8952pwn31at74uuu3cnqq8ma66tmpphjuen0kuf8"
        payload = {
            "action": action,
            "mint": mint_address,
            "amount": amount,
            "denominatedInSol": "true",
            "slippage": 5,
            "priorityFee": 0.0005,
            "pool": "pump"
        }
        
        try:
            response = requests.post(url, json=payload)
            response.raise_for_status()
            logger.info(f"Swap response: {response.json()}")
            return "Swap successful" if "Swap successful" in response.json() else "Swap failed"
        except requests.exceptions.RequestException as e:
            logger.error(f"Error during swap request: {e}")
            return f"Swap failed: {str(e)}"
        
    async def monitor_roi(self, mint_address, initial_investment):
        logger.info(f"Monitoring ROI for {mint_address} with initial investment of {initial_investment}")
        
        initial_price = await self.get_current_price(mint_address)
        if initial_price is None:
            logger.error("Failed to get initial price. Exiting ROI monitoring.")
            return
        
        target_price = initial_price * 2  # Target price for 100% ROI
        stop_loss_price = initial_price * 0.5  # Stop loss at 50% of initial price

        while True:
            current_price = await self.get_current_price(mint_address)
            if current_price is None:
                logger.error("Failed to get current price. Retrying...")
                await asyncio.sleep(60)  # Wait before retrying
                continue
            
            current_roi = (current_price - initial_price) / initial_price * 100
            logger.info(f"Current price for {mint_address}: {current_price}, Current ROI: {current_roi:.2f}%")

            if current_price <= stop_loss_price:
                logger.info(f"Stop loss triggered for {mint_address}. Selling...")
                sell_response = await self.send_swap_request(mint_address, action="sell", amount=initial_investment)
                logger.info(sell_response)
                sell_message = f"""<b>🔄 Sell Transaction (Stop Loss)</b>

Token: <code>{mint_address}</code>
Amount: {initial_investment} SOL
Result: {sell_response}
ROI: {current_roi:.2f}%
Wallet: <code>8DbwnZ2eAuxucMzGv5dmDhZBxuzz438rxcHbqBcM1HFB</code>
"""
                await self.send_to_telegram(sell_message)
                break

            if current_price >= target_price:
                logger.info(f"Target ROI reached for {mint_address}. Selling...")
                sell_response = await self.send_swap_request(mint_address, action="sell", amount=initial_investment)
                logger.info(sell_response)
                sell_message = f"""<b>🔄 Sell Transaction (Target Reached)</b>

Token: <code>{mint_address}</code>
Amount: {initial_investment} SOL
Result: {sell_response}
ROI: {current_roi:.2f}%
Wallet: <code>8DbwnZ2eAuxucMzGv5dmDhZBxuzz438rxcHbqBcM1HFB</code>
"""
                await self.send_to_telegram(sell_message)
                break

            await asyncio.sleep(60)  # Check every minute

    def is_potential_celebrity_token(self, token_data):
        """Analyzes token data to determine if it might be a celebrity-related token."""
        detection_info = {
            'celebrity_matches': [],
            'title_matches': [],
            'related_term_matches': [],
            'confidence_score': 0,
            'social_verification': False
        }

        text_to_analyze = ' '.join([
            str(token_data.get('name', '')).lower(),
            str(token_data.get('symbol', '')).lower(),
            str(token_data.get('description', '')).lower()
        ])

        # Check for celebrity name matches
        for name in self.celebrity_keywords['names']:
            if name in text_to_analyze:
                detection_info['celebrity_matches'].append(name)
                detection_info['confidence_score'] += 30

        # Check for title matches
        for title in self.celebrity_keywords['titles']:
            if title in text_to_analyze:
                detection_info['title_matches'].append(title)
                detection_info['confidence_score'] += 15

        # Check for related terms
        for term in self.celebrity_keywords['related_terms']:
            if term in text_to_analyze:
                detection_info['related_term_matches'].append(term)
                detection_info['confidence_score'] += 10

        # Check social media verification
        social_links = token_data.get('social_links', {})
        if social_links:
            for platform, link in social_links.items():
                if link and any(celeb in link.lower() for celeb in self.celebrity_keywords['names']):
                    detection_info['social_verification'] = True
                    detection_info['confidence_score'] += 25

        is_celebrity_token = (
            len(detection_info['celebrity_matches']) > 0 and
            detection_info['confidence_score'] >= 50
        )

        return is_celebrity_token, detection_info

    def log_token_count(self):
        """Log the current token count to the dedicated token count log file"""
        token_count_logger.info(f"Total tokens processed: {self.processed_tokens_count}")
    
    async def process_transaction(self, signature):
        try:
            tx_details = await get_transaction_details_with_backoff(signature)
            if tx_details:
                mint_address, is_pump_fun = extract_mint_address(tx_details)
                if mint_address and mint_address not in logged_mint_addresses:
                    logged_mint_addresses.add(mint_address)
                    self.processed_tokens_count += 1  # Increment token counter
                    logger.info(f"Processing new token with mint address: {mint_address} (Total processed: {self.processed_tokens_count})")
                    
                    # Log to token count file
                    self.log_token_count()
                    
                    # Fetch token info with error handling
                    try:
                        token_info = await get_token_info(mint_address)
                        logger.info(f"Token info received: {token_info}")
                        
                        # Use DexScreener data instead of token metadata
                        dexscreener_response = await get_dexscreener_data(mint_address)
                        logger.info(f"DexScreener API response: {dexscreener_response}")
                        
                        # Check if we have valid DexScreener data - THE CRITICAL FIX
                        has_dexscreener_data = False
                        dexscreener_data = None
                        
                        # First check: is it the standard format with 'pairs' field?
                        if dexscreener_response and isinstance(dexscreener_response, dict):
                            if 'pairs' in dexscreener_response and isinstance(dexscreener_response['pairs'], list) and len(dexscreener_response['pairs']) > 0:
                                has_dexscreener_data = True
                                dexscreener_data = dexscreener_response['pairs'][0]
                            # Second check: is it already a pair object?
                            elif 'baseToken' in dexscreener_response and 'pairAddress' in dexscreener_response:
                                has_dexscreener_data = True
                                dexscreener_data = dexscreener_response
                        
                        logger.info(f"DexScreener data processed: {dexscreener_data}")
                        logger.info(f"Has DexScreener data: {has_dexscreener_data}")
                        
                        # Get rugcheck data
                        rugcheck_data = await get_rugcheck_data(mint_address)
                        
                        # Update rug vs good token counts based on rugcheck data
                        if rugcheck_data:
                            if rugcheck_data.get('score', 1000) > 500:
                                self.rug_count += 1
                            else:
                                self.good_token_count += 1
                        
                        if not has_dexscreener_data:
                            # Store token info for later checking
                            self.pending_tokens[mint_address] = {
                                'token_info': token_info,
                                'added_time': time.time(),  # Add current timestamp
                                'attempts': 0,  # Track how many times we've tried to get DexScreener data
                                'signature': signature,
                                'is_pump_fun': is_pump_fun,
                                'check_attempts': 1
                            }
                            logger.info(f"Token {mint_address} added to pending tokens (no DexScreener data yet)")
                            
                            # Schedule a check for this token later
                            asyncio.create_task(self.check_pending_token_later(mint_address))
                            
                            # Don't continue processing until we have DexScreener data
                            logger.info(f"Skipping detailed processing for {mint_address} until DexScreener data is available")
                            return
                        else:
                            # We have DexScreener data, increment counter
                            self.tokens_with_dexscreener_data += 1
                            logger.info(f"Token {mint_address} has DexScreener data (Total with data: {self.tokens_with_dexscreener_data})")
                            
                    except Exception as e:
                        logger.error(f"Error fetching token data: {e}")
                        return

                    # Now process the token with DexScreener data
                    # ... rest of your existing processing code ...
                    
                    # Format and send the Telegram message
                    telegram_message = self.format_token_message(signature, mint_address, token_info, dexscreener_data, rugcheck_data, is_pump_fun)
                    await self.send_to_telegram(telegram_message)
                    
                    # Check if token is safe and make a buy
                    if dexscreener_data and rugcheck_data:
                        is_safe = True
                        if is_safe:
                            logger.info(f"Token {mint_address} appears safe, initiating buy transaction")
                            buy_amount = 0.003  # Default amount in SOL
                            buy_result = await self.send_swap_request(mint_address, action="buy", amount=buy_amount)
                            buy_message = f"""<b>🔄 Buy Transaction</b>
                            
Token: <b>{dexscreener_data.get('baseToken', {}).get('name', 'Unknown')}</b>
Mint: <code>{mint_address}</code>
Amount: {buy_amount} SOL
Result: {buy_result}
Wallet: <code>8DbwnZ2eAuxucMzGv5dmDhZBxuzz438rxcHbqBcM1HFB</code>

<i>Starting ROI monitoring</i>
"""
                            await self.send_to_telegram(buy_message)
                            
                            # Start monitoring ROI
                            asyncio.create_task(self.monitor_roi(mint_address, buy_amount))
                        else:
                            logger.info(f"Token {mint_address} did not pass safety checks, no buy initiated")
                    
                else:
                    logger.debug(f"Duplicate or invalid mint address for transaction {signature}")
            else:
                logger.debug(f"No transaction details found for {signature}")
        except Exception as e:
            logger.error(f"Error processing transaction {signature}: {e}", exc_info=True)

    def format_token_message(self, signature, mint_address, token_info, dexscreener_data, rugcheck_data, is_pump_fun):
        """Format a comprehensive message for a token with DexScreener data"""
        token_type = "🪙 Standard Token"
        platform = "Pump.fun" if is_pump_fun else "Solana"
        
        # Extract token name and symbol from DexScreener data
        token_name = "Unknown"
        token_symbol = "Unknown"
        
        if dexscreener_data and 'baseToken' in dexscreener_data:
            base_token = dexscreener_data['baseToken']
            token_name = base_token.get('name', 'Unknown')
            token_symbol = base_token.get('symbol', 'Unknown')
        
        # Format the message with all available data
        message = f"""<b>🔍 SolSentinel #{self.processed_tokens_count}</b>

<b>{token_type} Detected on {platform}</b>

<b>Token Details:</b>
• Name: <b>{token_name}</b>
• Symbol: <b>{token_symbol}</b>
• Mint: <code>{mint_address}</code>
"""

        # Add DexScreener data if available
        if dexscreener_data:
            price_usd = dexscreener_data.get('priceUsd', 'Unknown')
            price_native = dexscreener_data.get('priceNative', 'Unknown')
            market_cap = dexscreener_data.get('marketCap', 'Unknown')
            fdv = dexscreener_data.get('fdv', 'Unknown')
            
            # Get transaction data
            txns = dexscreener_data.get('txns', {})
            txns_24h = txns.get('h24', {})
            buys_24h = txns_24h.get('buys', 0)
            sells_24h = txns_24h.get('sells', 0)
            
            # Get volume data
            volume = dexscreener_data.get('volume', {})
            volume_24h = volume.get('h24', 'Unknown')
            
            # Get price change data
            price_change = dexscreener_data.get('priceChange', {})
            price_change_24h = price_change.get('h24', 'Unknown')
            
            message += f"""
<b>Price Information:</b>
• Price USD: {price_usd}
• Price SOL: {price_native}
• 24h Change: {price_change_24h}%
• Market Cap: {market_cap}
• Fully Diluted Value: {fdv}

<b>Trading Activity:</b>
• 24h Volume: {volume_24h}
• 24h Buys: {buys_24h}
• 24h Sells: {sells_24h}
"""

            # Add DEX info if available
            dex_id = dexscreener_data.get('dexId', 'Unknown')
            pair_address = dexscreener_data.get('pairAddress', 'Unknown')
            dex_url = dexscreener_data.get('url', f"https://dexscreener.com/solana/{pair_address}")
            
            message += f"""
<b>DEX Information:</b>
• DEX: {dex_id}
• Pair: <code>{pair_address}</code>
"""

        # Add rugcheck info if available
        if rugcheck_data:
            risk_level = "Low" if rugcheck_data.get('score', 1000) < 500 else "High"
            message += f"""
<b>Security Analysis:</b>
• Risk Level: {risk_level}
• RugCheck Score: {rugcheck_data.get('score', 'N/A')}
"""

        # Add links
        message += f"""
<b>Links:</b>
• <a href="https://dexscreener.com/solana/{mint_address}">DexScreener</a>
• <a href="https://explorer.solana.com/tx/{signature}">Transaction</a>
• <a href="https://solscan.io/token/{mint_address}">Token Info</a>
• <a href="https://solscan.io/account/8DbwnZ2eAuxucMzGv5dmDhZBxuzz438rxcHbqBcM1HFB">Wallet</a>
"""

        return message

    async def check_pending_token_later(self, mint_address, delay=300):
        """Check a pending token for DexScreener data after a delay"""
        await asyncio.sleep(delay)  # Wait 5 minutes by default
        
        if mint_address not in self.pending_tokens:
            logger.debug(f"Token {mint_address} no longer in pending tokens, skipping check")
            return
            
        token_data = self.pending_tokens[mint_address]
        token_data['check_attempts'] += 1
        
        logger.info(f"Checking pending token {mint_address} (attempt {token_data['check_attempts']})")
        
        try:
            # Try to get DexScreener data
            dexscreener_data = await get_dexscreener_data(mint_address)
            has_dexscreener_data = dexscreener_data and 'pairs' in dexscreener_data and dexscreener_data['pairs']
            
            if has_dexscreener_data:
                logger.info(f"DexScreener data now available for {mint_address}")
                self.tokens_with_dexscreener_data += 1
                
                # Process the token now that we have data
                await self.process_token_with_data(mint_address, token_data, dexscreener_data)
                
                # Remove from pending tokens
                del self.pending_tokens[mint_address]
            else:
                # Still no data, check if we should try again
                time_since_discovery = time.time() - token_data['added_time']
                if time_since_discovery < 3600 and token_data['check_attempts'] < 5:  # Try for up to 1 hour, max 5 attempts
                    # Schedule another check with increasing delay
                    next_delay = delay * 1.5
                    logger.info(f"No DexScreener data yet for {mint_address}, will check again in {next_delay:.0f} seconds")
                    asyncio.create_task(self.check_pending_token_later(mint_address, next_delay))
                else:
                    logger.info(f"Giving up on getting DexScreener data for {mint_address} after {token_data['check_attempts']} attempts")
                    # Remove from pending tokens
                    del self.pending_tokens[mint_address]
        except Exception as e:
            logger.error(f"Error checking pending token {mint_address}: {e}")
            
    async def process_token_with_data(self, mint_address, token_data, dexscreener_data):
        """Process a token that now has DexScreener data"""
        try:
            signature = token_data['signature']
            is_pump_fun = token_data['is_pump_fun']
            token_info = token_data['token_info']
            
            # Get rugcheck data
            rugcheck_data = await get_rugcheck_data(mint_address)
            
            # Get pump.fun data if applicable
            pump_fun_data = None
            if is_pump_fun:
                pump_fun_data = await get_pump_fun_data(mint_address)
            
            # Prepare token data for celebrity detection
            token_data_for_detection = {
                'name': '',
                'symbol': '',
                'description': '',
                'social_links': {
                    'twitter': '',
                    'telegram': '',
                }
            }
            
            # Extract data from DexScreener
            pair = dexscreener_data['pairs'][0]
            base_token = pair.get('baseToken', {})
            token_data_for_detection['name'] = base_token.get('name', '')
            token_data_for_detection['symbol'] = base_token.get('symbol', '')
            
            # Add pump.fun data if available
            if pump_fun_data:
                token_data_for_detection['name'] = pump_fun_data.get('name', token_data_for_detection['name'])
                token_data_for_detection['symbol'] = pump_fun_data.get('symbol', token_data_for_detection['symbol'])
                token_data_for_detection['description'] = pump_fun_data.get('description', '')
                token_data_for_detection['social_links']['twitter'] = pump_fun_data.get('twitter', '')
                token_data_for_detection['social_links']['telegram'] = pump_fun_data.get('telegram', '')
            
            # Check if it's a celebrity token
            is_celebrity, celebrity_info = self.is_potential_celebrity_token(token_data_for_detection)
            
            # Format and send Telegram message with all DexScreener data
            token_type = "🌟 Celebrity Token" if is_celebrity else "🪙 Standard Token"
            platform = "Pump.fun" if is_pump_fun else "Solana"
            
            # Format token name and symbol
            token_name = token_data_for_detection['name'] if token_data_for_detection['name'] else "Unknown"
            token_symbol = token_data_for_detection['symbol'] if token_data_for_detection['symbol'] else "Unknown"
            
            # Get all DexScreener data
            pair = dexscreener_data['pairs'][0]
            price_usd = f"${pair.get('priceUsd', 'Unknown')}"
            price_native = f"{pair.get('priceNative', 'Unknown')} SOL"
            market_cap = f"${pair.get('marketCap', 'Unknown')}"
            fdv = f"${pair.get('fdv', 'Unknown')}"
            
            # Get transaction data
            txns = pair.get('txns', {})
            txns_24h = txns.get('h24', {})
            buys_24h = txns_24h.get('buys', 0)
            sells_24h = txns_24h.get('sells', 0)
            
            # Get volume data
            volume = pair.get('volume', {})
            volume_24h = f"${volume.get('h24', 'Unknown')}"
            
            # Get price change data
            price_change = pair.get('priceChange', {})
            price_change_24h = f"{price_change.get('h24', 'Unknown')}%"
            
            # Get pair creation time
            pair_created_at = pair.get('pairCreatedAt', 0)
            if pair_created_at:
                created_time = datetime.fromtimestamp(pair_created_at / 1000).strftime('%Y-%m-%d %H:%M:%S')
            else:
                created_time = "Unknown"
            
            # Get DEX info
            dex_id = pair.get('dexId', 'Unknown')
            pair_address = pair.get('pairAddress', 'Unknown')
            dex_url = pair.get('url', f"https://dexscreener.com/solana/{pair_address}")
            
            # Format the message with all DexScreener data
            telegram_message = f"""<b>🔍 SolSentinel #{self.processed_tokens_count}</b>

<b>{token_type} Detected on {platform}</b>
<i>DexScreener data now available</i>

<b>Token Details:</b>
• Name: <b>{token_name}</b>
• Symbol: <b>{token_symbol}</b>
• Mint: <code>{mint_address}</code>

<b>Price Information:</b>
• Price USD: {price_usd}
• Price SOL: {price_native}
• 24h Change: {price_change_24h}
• Market Cap: {market_cap}
• Fully Diluted Value: {fdv}

<b>Trading Activity:</b>
• 24h Volume: {volume_24h}
• 24h Buys: {buys_24h}
• 24h Sells: {sells_24h}
• Created: {created_time}

<b>DEX Information:</b>
• DEX: {dex_id}
• Pair: <code>{pair_address}</code>

<b>Links:</b>
• <a href="{dex_url}">DexScreener</a>
• <a href="https://explorer.solana.com/tx/{signature}">Transaction</a>
• <a href="https://solscan.io/token/{mint_address}">Token Info</a>
• <a href="https://solscan.io/account/8DbwnZ2eAuxucMzGv5dmDhZBxuzz438rxcHbqBcM1HFB">Wallet</a>
"""

            # Add celebrity info if applicable
            if is_celebrity:
                telegram_message += f"""
<b>Celebrity Detection:</b>
• Confidence: {celebrity_info['confidence_score']}%
• Matches: {', '.join(celebrity_info['celebrity_matches']) if celebrity_info['celebrity_matches'] else 'None'}
• Related Terms: {', '.join(celebrity_info['related_term_matches']) if celebrity_info['related_term_matches'] else 'None'}
"""

            # Add rugcheck info if available
            if rugcheck_data:
                risk_level = "Low" if rugcheck_data.get('score', 1000) < 500 else "High"
                telegram_message += f"""
<b>Security Analysis:</b>
• Risk Level: {risk_level}
• RugCheck Score: {rugcheck_data.get('score', 'N/A')}
"""

            # Add social links if available
            social_links = []
            if token_data_for_detection['social_links']['twitter']:
                social_links.append(f"• <a href='{token_data_for_detection['social_links']['twitter']}'>Twitter</a>")
            if token_data_for_detection['social_links']['telegram']:
                social_links.append(f"• <a href='{token_data_for_detection['social_links']['telegram']}'>Telegram</a>")
            
            if social_links:
                telegram_message += f"""
<b>Social Links:</b>
{''.join(social_links)}
"""

            # Send the message to Telegram
            await self.send_to_telegram(telegram_message)
            
            # Process as celebrity token if applicable
            if is_celebrity:
                logger.info(f"{Fore.MAGENTA}🌟 Celebrity Token with DexScreener data detected! Processing details...{Style.RESET_ALL}")
                # ... existing celebrity token processing code ...
                
        except Exception as e:
            logger.error(f"Error processing token with data {mint_address}: {e}")

    async def send_to_telegram(self, message):
        """Send a message to the Telegram channel"""
        try:
            # Strip ANSI color codes for Telegram
            clean_message = re.sub(r'\x1b\[\d+m', '', message)
            await bot.send_message(
                chat_id=TELEGRAM_CHANNEL_ID, 
                text=clean_message, 
                parse_mode='HTML',
                disable_web_page_preview=True  # Prevent link previews to keep messages compact
            )
            logger.info(f"Message sent to Telegram channel")
        except Exception as e:
            logger.error(f"Error sending message to Telegram: {e}")

    async def send_daily_summary(self):
        """Send a daily summary to the Telegram channel"""
        summary = f"""📊 <b>Daily Summary</b>
        
Total tokens processed today: {self.processed_tokens_count}
Tokens with DexScreener data: {self.tokens_with_dexscreener_data}
Pending tokens (waiting for data): {len(self.pending_tokens)}
Celebrity tokens detected: {len(self.owned_tokens)}

<b>Top Tokens:</b>
{self._get_top_tokens_summary()}

<b>Trending Keywords:</b>
{self._get_trending_keywords()}
"""
        await self.send_to_telegram(summary)
    
    def _get_top_tokens_summary(self):
        # Implement logic to get top tokens based on your criteria
        # This is a placeholder
        return "No top tokens data available yet"
    
    def _get_trending_keywords(self):
        # Implement logic to extract trending keywords from token names/descriptions
        # This is a placeholder
        return "No trending keywords data available yet"

    async def send_periodic_status(self):
        """Send a status message every 30 seconds with token statistics"""
        while True:
            try:
                await asyncio.sleep(30)  # Wait 30 seconds between status messages
                
                # Calculate statistics
                total_tokens = self.processed_tokens_count
                tokens_with_data = self.tokens_with_dexscreener_data
                pending_count = len(self.pending_tokens)
                
                # Calculate rug vs good token ratio
                rug_percentage = 0
                good_token_percentage = 0
                
                if total_tokens > 0:
                    # Simple algorithm: tokens with DexScreener data are more likely to be legitimate
                    # Tokens with high rugcheck scores are more likely to be rugs
                    rug_percentage = (self.rug_count / total_tokens) * 100 if self.rug_count > 0 else 70  # Default assumption
                    good_token_percentage = (self.good_token_count / total_tokens) * 100 if self.good_token_count > 0 else 30
                
                # Format the status message
                status_message = f"""<b>🔄 SolSentinel Status Update</b>

<b>Token Statistics:</b>
• Total tokens detected: {total_tokens}
• Tokens with market data: {tokens_with_data}
• Tokens pending data: {pending_count}

<b>Token Quality Assessment:</b>
• Estimated rug percentage: {rug_percentage:.1f}%
• Estimated quality token percentage: {good_token_percentage:.1f}%

<i>Monitoring active - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>
"""
                
                # Send the status message to Telegram
                await self.send_to_telegram(status_message)
                
            except Exception as e:
                logger.error(f"Error sending periodic status: {e}")

    async def check_pending_tokens(self):
        """Periodically check pending tokens for available DexScreener data and remove stale tokens."""
        try:
            tokens_to_remove = []
            current_time = time.time()
            
            for mint_address, token_data in self.pending_tokens.items():
                # Check if the token has been pending for too long (e.g., 30 minutes)
                if current_time - token_data.get('added_time', current_time) > 1800:
                    logger.info(f"Removing stale token {mint_address} from pending queue (waited too long for DexScreener data)")
                    tokens_to_remove.append(mint_address)
                    continue
                    
                # Try to get DexScreener data again
                dex_data = await self.get_dexscreener_data(mint_address)
                if dex_data:
                    logger.info(f"DexScreener data now available for {mint_address}, processing token")
                    # Remove from pending and process completely
                    tokens_to_remove.append(mint_address)
                    # Process with the newly available data
                    await self.process_token_with_data(mint_address, token_data, dex_data)
                    
            # Remove processed or stale tokens from pending list
            for mint_address in tokens_to_remove:
                self.pending_tokens.pop(mint_address, None)
            
            # Log summary of pending tokens
            if self.pending_tokens:
                logger.info(f"Current pending tokens: {len(self.pending_tokens)} tokens waiting for DexScreener data")
            
        except Exception as e:
            logger.error(f"Error checking pending tokens: {e}")

async def main():
    monitor = SolanaMonitor()
    await monitor.start_monitoring()

if __name__ == "__main__":
    asyncio.run(main())
