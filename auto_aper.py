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
from unsplash_image_fetcher import UnsplashImageFetcher
import token_trader


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

# Add a global session for Telegram API
telegram_session = None

async def create_telegram_session():
    """Create and return a properly configured aiohttp session for Telegram API"""
    connector = aiohttp.TCPConnector(
        limit=20,          # Maximum number of connections
        limit_per_host=10, # Maximum connections per host
        enable_cleanup_closed=True,
        force_close=True,  # Force close connections when done
        ttl_dns_cache=300  # Cache DNS results for 5 minutes
    )
    timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_connect=10, sock_read=30)
    return aiohttp.ClientSession(connector=connector, timeout=timeout)

# Initialize async resources at startup
async def init_async_resources():
    global telegram_session
    telegram_session = await create_telegram_session()
    logger.info("Initialized global Telegram API session")

# Cleanup resources at shutdown
async def cleanup_async_resources():
    global telegram_session
    if telegram_session:
        await telegram_session.close()
        logger.info("Closed global Telegram API session")

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

# Initialize during your setup
image_fetcher = UnsplashImageFetcher()

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

async def get_rugcheck_data(mint_address):
    """Get rugcheck data for a token, with improved error handling"""
    try:
        url = f"https://api.rugcheck.xyz/v1/tokens/{mint_address}/score"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as response:  # Reduce timeout
                if response.status == 200:
                    data = await response.json()
                    return data
                else:
                    logger.warning(f"RugCheck API returned status {response.status} for {mint_address}")
                    return None
    except aiohttp.ClientConnectorError:
        logger.warning(f"Cannot connect to RugCheck API (service may be down)")
        return None
    except asyncio.TimeoutError:
        logger.warning(f"RugCheck API timeout for {mint_address}")
        return None
    except Exception as e:
        logger.warning(f"Error fetching RugCheck data: {e}")
        return None

async def send_swap_request(self, token_address, action, amount):
    """
    Execute a token swap (buy or sell)
    
    Args:
        token_address: Token address to trade
        action: 'buy' or 'sell'
        amount: Amount in SOL
        
    Returns:
        str: Transaction result message
    """
    try:
        # This is a placeholder - implement your actual swap logic here
        logger.info(f"SIMULATION: {action.upper()} {amount} SOL worth of {token_address}")
        
        # For now, return a simulated success message
        # In a real implementation, this would connect to Jupiter/Raydium/etc.
        return f"Transaction simulated successfully (DEMO MODE)"
    except Exception as e:
        logger.error(f"Error executing {action} swap: {e}")
        return f"Transaction failed: {str(e)}"

async def get_current_price(self, token_address):
    """Get current price for a token"""
    return await token_trader.get_token_price(token_address)

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

        self.token_trader = token_trader.TokenTrader(
        telegram_sender=self.send_to_telegram,
        swap_function=self.send_swap_request
    )
        
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
        try:
            logger.info("Attempting to establish WebSocket connection...")
            
            # Set reasonable timeouts
            connection_timeout = 30  # 30 seconds to connect
            ping_interval = 30       # Send ping every 30 seconds
            ping_timeout = 10        # Wait 10 seconds for pong
            
            async with websockets.connect(
                RPC_URL,
                ping_interval=ping_interval,
                ping_timeout=ping_timeout,
                close_timeout=10,
                max_size=10 * 1024 * 1024,  # 10MB max message size
                max_queue=1000,             # Maximum queue size
            ) as websocket:
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
                logger.info(f"Sending subscription message")

                await websocket.send(json.dumps(subscribe_message))
                logger.info("Subscription message sent, waiting for messages...")

                # Start periodic tasks in separate tasks
                status_task = asyncio.create_task(self.send_periodic_status())
                pending_tokens_task = asyncio.create_task(self.check_pending_tokens())
                
                # Schedule regular ping to keep the connection alive
                ping_task = asyncio.create_task(self._keep_websocket_alive(websocket))
                
                # Main loop to process incoming messages
                while self.should_run:
                    try:
                        # Set timeout for receiving messages
                        message = await asyncio.wait_for(websocket.recv(), timeout=60)
                        await self.process_message(message)
                    except asyncio.TimeoutError:
                        logger.warning("No message received for 60 seconds, checking connection...")
                        try:
                            # Check if connection is still alive
                            pong_waiter = await websocket.ping()
                            await asyncio.wait_for(pong_waiter, timeout=5)
                            logger.info("Connection is still alive")
                        except:
                            logger.warning("Connection may be dead, will reconnect on next loop")
                            break
                    except websockets.exceptions.ConnectionClosed as e:
                        logger.warning(f"WebSocket connection closed: {e}")
                        break
                    
                # Cancel tasks gracefully
                for task in [status_task, pending_tokens_task, ping_task]:
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        
        except Exception as e:
            logger.error(f"Error in WebSocket connection: {e}")
        finally:
            self.ws = None

    async def _keep_websocket_alive(self, websocket):
        """Send periodic pings to keep the WebSocket connection alive"""
        try:
            while True:
                await asyncio.sleep(30)  # Ping every 30 seconds
                if not self.should_run or websocket.closed:
                    break
                try:
                    pong_waiter = await websocket.ping()
                    await asyncio.wait_for(pong_waiter, timeout=5)
                except:
                    logger.warning("Failed to receive pong, connection may be dead")
                    break
        except asyncio.CancelledError:
            pass  # Task was cancelled, exit gracefully

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

        # Get token name and symbol from dexscreener data
        token_name = dexscreener_data.get('baseToken', {}).get('name', 'Unknown')
        token_symbol = dexscreener_data.get('baseToken', {}).get('symbol', 'Unknown')

        # Get a related image
        image_data = None
        try:
            # Try token name first
            image_data = image_fetcher.get_related_image(token_name, token_symbol)
            
            # If no image found and token name is very specific, try more generic search
            if not image_data or 'url' not in image_data:
                logger.info(f"No specific image found for {token_name}, trying generic cryptocurrency image")
                image_data = image_fetcher.get_related_image("cryptocurrency", "token")
            
            # Add image URL to your Telegram message if available
            if image_data and 'url' in image_data:
                logger.info(f"Adding image URL to message: {image_data['url']}")
                message += f"\n\n<a href=\"{image_data['url']}\">&#8205;</a>"  # This creates an invisible link that generates a preview
        except Exception as img_error:
            logger.warning(f"Error fetching image: {img_error}")

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
                        # Remove await here as get_token_info returns a dict, not a coroutine
                        token_info = get_token_info(mint_address)
                        logger.info(f"Token info received: {token_info}")
                        
                        # Get token name and symbol from token_info if available
                        token_name = "Unknown"
                        token_symbol = "Unknown"
                        if token_info and 'mintAuthority' in token_info:
                            token_name = token_info.get('name', 'Unknown')
                            token_symbol = token_info.get('symbol', 'Unknown')
                        
                        # Always send a basic Telegram message for each new token detected
                        basic_message = f"""<b>🔍 SolSentinel #{self.processed_tokens_count}</b>

<b>New Token Detected</b>

<b>Token Details:</b>
• Mint: <code>{mint_address}</code>
• Platform: {("pump.fun" if is_pump_fun else "Solana")}

<b>Links:</b>
• <a href="https://explorer.solana.com/tx/{signature}">Transaction</a>
• <a href="https://solscan.io/token/{mint_address}">Token Info</a>
"""
                        # Send the basic notification immediately
                        await self.send_to_telegram(basic_message)
                        
                        # Use DexScreener data instead of token metadata
                        dexscreener_response = await get_dexscreener_data(mint_address)
                        logger.info(f"DexScreener API response: {dexscreener_response}")
                        
                        # Check if we have valid DexScreener data
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
                        
                        # Get rugcheck data (optional now)
                        rugcheck_data = None
                        try:
                            rugcheck_data = await get_rugcheck_data(mint_address)
                            # Update rug vs good token counts based on rugcheck data
                            if rugcheck_data:
                                if rugcheck_data.get('score', 1000) > 500:
                                    self.rug_count += 1
                                else:
                                    self.good_token_count += 1
                        except Exception as e:
                            logger.warning(f"Error with RugCheck API (continuing without security data): {e}")
                        
                        # If DexScreener data is available, send a more detailed follow-up message
                        if has_dexscreener_data:
                            self.tokens_with_dexscreener_data += 1
                            await self.process_token_with_data(mint_address, {
                                'signature': signature,
                                'is_pump_fun': is_pump_fun,
                                'token_info': token_info,
                                'added_time': time.time(),
                                'social_links': {
                                    'twitter': '',
                                    'telegram': ''
                                }
                            }, dexscreener_data)
                        else:
                            # Store in pending tokens for later processing when DexScreener data becomes available
                            self.pending_tokens[mint_address] = {
                                'signature': signature,
                                'is_pump_fun': is_pump_fun,
                                'token_info': token_info,
                                'added_time': time.time(),
                                'social_links': {
                                    'twitter': '',
                                    'telegram': ''
                                }
                            }
                    
                    except Exception as e:
                        logger.error(f"Error fetching token data: {e}")
                        
                        # Even if there's an error, send a basic Telegram message
                        basic_error_message = f"""<b>🔍 SolSentinel #{self.processed_tokens_count}</b>

<b>New Token Detected (Error in processing details)</b>

<b>Token Details:</b>
• Mint: <code>{mint_address}</code>
• Platform: {("pump.fun" if is_pump_fun else "Solana")}

<b>Links:</b>
• <a href="https://explorer.solana.com/tx/{signature}">Transaction</a>
• <a href="https://solscan.io/token/{mint_address}">Token Info</a>
"""
                        await self.send_to_telegram(basic_error_message)
        
        except Exception as e:
            logger.error(f"Error processing transaction {signature}: {e}")

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
            is_pump_fun = token_data.get('is_pump_fun', False)
            token_info = token_data.get('token_info', {})
            
            # Log that we're processing with DexScreener data
            logger.info(f"Processing token {mint_address} with DexScreener data")
            
            # Get token name and symbol from DexScreener data
            token_name = dexscreener_data.get('baseToken', {}).get('name', 'Unknown')
            token_symbol = dexscreener_data.get('baseToken', {}).get('symbol', 'Unknown')
            
            # Get price info
            price_usd = dexscreener_data.get('priceUsd', 'Unknown')
            price_native = dexscreener_data.get('priceNative', 'Unknown')
            
            # Calculate marketcap
            marketcap = dexscreener_data.get('fdv', 'Unknown')
            if marketcap == 'Unknown' and price_usd != 'Unknown' and token_info.get('supply'):
                supply = float(token_info.get('supply', 0))
                decimals = int(token_info.get('decimals', 0))
                adjusted_supply = supply / (10 ** decimals)
                marketcap = adjusted_supply * float(price_usd)
            
            # Format the detailed message
            detailed_message = f"""<b>🚀 {token_name} ({token_symbol})</b>

💰 <b>Market:</b>
• Price: ${price_usd} ({price_native} SOL)
• Market Cap: ${format(marketcap, ',.2f') if isinstance(marketcap, (int, float)) else 'Unknown'}
• Volume (24h): ${format(dexscreener_data.get('volume', {}).get('h24', 0), ',.2f') if isinstance(dexscreener_data.get('volume', {}).get('h24'), (int, float)) else 'Unknown'}
• Liquidity: ${format(dexscreener_data.get('liquidity', {}).get('usd', 0), ',.2f') if isinstance(dexscreener_data.get('liquidity', {}).get('usd'), (int, float)) else 'Unknown'}

📊 <b>Activity:</b>
• 24h Change: {dexscreener_data.get('priceChange', {}).get('h24', 'Unknown')}%
• Platform: {("pump.fun" if is_pump_fun else "Solana")}
• Age: {((time.time() - (dexscreener_data.get('pairCreatedAt', time.time()*1000)/1000)) / 3600):.1f}h

🔗 <b>Links:</b>
• <a href="{dexscreener_data.get('url', f'https://dexscreener.com/solana/{mint_address}')}">DexScreener</a>
• <a href="https://explorer.solana.com/tx/{signature}">Transaction</a>
• <a href="https://solscan.io/token/{mint_address}">Token Info</a>
• <a href="https://birdeye.so/token/{mint_address}?chain=solana">Birdeye</a>
• <a href="https://dexlab.space/market/{mint_address}">Dexlab</a>
{f'• <a href="{dexscreener_data.get("info", {}).get("socials", [{}])[0].get("url", "")}">Twitter</a>' if dexscreener_data.get("info", {}).get("socials") and dexscreener_data.get("info", {}).get("socials")[0].get("type") == "twitter" else ''}
{f'• <a href="{dexscreener_data.get("info", {}).get("websites", [{}])[0].get("url", "")}">Website</a>' if dexscreener_data.get("info", {}).get("websites") else ''}

<code>{mint_address}</code>

<i>SolSentinel #{self.processed_tokens_count}</i>"""
            
            # Get a related image
            image_data = None
            try:
                # Try token name first
                image_data = image_fetcher.get_related_image(token_name, token_symbol)
                
                # If no image found and token name is very specific, try more generic search
                if not image_data or 'url' not in image_data:
                    logger.info(f"No specific image found for {token_name}, trying generic cryptocurrency image")
                    image_data = image_fetcher.get_related_image("cryptocurrency", "token")
                
                # Add image URL to your Telegram message if available
                if image_data and 'url' in image_data:
                    logger.info(f"Adding image URL to message: {image_data['url']}")
                    detailed_message += f"\n\n<a href=\"{image_data['url']}\">&#8205;</a>"  # This creates an invisible link that generates a preview
            except Exception as img_error:
                logger.warning(f"Error fetching image: {img_error}")
            
            # Send the detailed message
            logger.info(f"Sending detailed message for token {mint_address}")
            await self.send_to_telegram(detailed_message)
            
            # Add to processed tokens with data
            self.tokens_with_dexscreener_data += 1
            
        except Exception as e:
            logger.error(f"Error processing token with DexScreener data {mint_address}: {e}")
            # Try to send a simplified message if detailed processing fails
            error_message = f"""<b>⚠️ Token with DexScreener Data (Error in processing)</b>

<b>Token Details:</b>
• Mint: <code>{mint_address}</code>
• DexScreener: <a href="https://dexscreener.com/solana/{mint_address}">View</a>
"""
            await self.send_to_telegram(error_message)

    async def send_to_telegram(self, message):
        """Send a message to the Telegram channel with proper rate limiting and connection handling"""
        global telegram_session
        
        try:
            # Rate limiting implementation
            current_time = time.time()
            time_since_last_message = current_time - getattr(self, 'last_telegram_message_time', 0)
            
            # If less than 30 seconds since last message, wait
            if time_since_last_message < 30:
                wait_time = 30 - time_since_last_message
                logger.info(f"Rate limiting: Waiting {wait_time:.1f} seconds before sending next message")
                await asyncio.sleep(wait_time)
            
            # Strip ANSI color codes for Telegram
            clean_message = re.sub(r'\x1b\[\d+m', '', message)
            
            # Extract image URL if present
            image_url = None
            if "&#8205;" in clean_message:
                match = re.search(r'<a href="([^"]+)">&#8205;<\/a>', clean_message)
                if match:
                    image_url = match.group(1)
                    clean_message = clean_message.replace(f'<a href="{image_url}">&#8205;</a>', '')
            
            # Ensure we have a session
            if telegram_session is None or telegram_session.closed:
                logger.info("Creating new Telegram session as none exists or it was closed")
                telegram_session = await create_telegram_session()
            
            # Send with exponential backoff retry logic
            max_retries = 5
            retry_delay = 1
            
            for attempt in range(max_retries):
                try:
                    # Use the session directly with the Telegram API
                    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                    payload = {
                        "chat_id": TELEGRAM_CHANNEL_ID,
                        "text": clean_message,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True
                    }
                    
                    async with telegram_session.post(url, json=payload, timeout=30) as response:
                        if response.status == 200:
                            response_data = await response.json()
                            logger.debug(f"Telegram API response: {response_data}")
                            self.last_telegram_message_time = time.time()
                            break
                        elif response.status == 429:
                            # Handle rate limiting
                            response_data = await response.json()
                            retry_after = response_data.get('parameters', {}).get('retry_after', retry_delay * 2)
                            logger.warning(f"Telegram rate limit hit, waiting {retry_after} seconds")
                            await asyncio.sleep(retry_after)
                            continue
                        else:
                            logger.error(f"Telegram API error: {response.status}, {await response.text()}")
                            retry_delay *= 2
                            await asyncio.sleep(retry_delay)
                except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
                    logger.error(f"Connection error on attempt {attempt+1}/{max_retries}: {e}")
                    retry_delay *= 2
                    await asyncio.sleep(retry_delay)
            
            # Send image if we have one using the same session
            if image_url:
                await asyncio.sleep(5)  # Wait before sending image to avoid rate limits
                
                for attempt in range(max_retries):
                    try:
                        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
                        payload = {
                            "chat_id": TELEGRAM_CHANNEL_ID,
                            "photo": image_url,
                            "caption": "Image for token"
                        }
                        
                        async with telegram_session.post(url, json=payload, timeout=30) as response:
                            if response.status == 200:
                                self.last_telegram_message_time = time.time()
                                break
                            elif response.status == 429:
                                # Handle rate limiting
                                response_data = await response.json()
                                retry_after = response_data.get('parameters', {}).get('retry_after', retry_delay * 2)
                                logger.warning(f"Telegram rate limit hit for image, waiting {retry_after} seconds")
                                await asyncio.sleep(retry_after)
                            else:
                                logger.error(f"Telegram API error for image: {response.status}")
                                retry_delay *= 2
                                await asyncio.sleep(retry_delay)
                    except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
                        logger.error(f"Connection error sending image, attempt {attempt+1}/{max_retries}: {e}")
                        retry_delay *= 2
                        await asyncio.sleep(retry_delay)
            
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
                dex_data = await get_dexscreener_data(mint_address)
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

    async def get_current_price(self, mint_address):
        """Get the current price of a token from DexScreener"""
        try:
            dexscreener_data = await get_dexscreener_data(mint_address)
            if dexscreener_data and 'pairs' in dexscreener_data and dexscreener_data['pairs']:
                pair = dexscreener_data['pairs'][0]
                price_usd = float(pair.get('priceUsd', 0))
                return price_usd
            return None
        except Exception as e:
            logger.error(f"Error getting current price for {mint_address}: {e}")
            return None

async def main():
    # Initialize resources
    await init_async_resources()
    
    try:
        # Start the monitor
        monitor = SolanaMonitor()
        monitor_task = asyncio.create_task(monitor.start_monitoring())
        
        # Start a minimal HTTP server for health checks on Render
        from aiohttp import web
        
        # Simple HTTP routes
        routes = web.RouteTableDef()
        
        @routes.get('/')
        async def health_check(request):
            return web.Response(text="SolSentinel is running")
        
        @routes.get('/status')
        async def status(request):
            return web.json_response({
                "status": "running",
                "tokens_processed": monitor.processed_tokens_count,
                "tokens_with_data": monitor.tokens_with_dexscreener_data,
                "pending_tokens": len(monitor.pending_tokens),
                "uptime": time.time() - getattr(monitor, 'start_time', time.time())
            })
        
        # Create app
        app = web.Application()
        app.add_routes(routes)
        
        # Get port from environment or use default
        port = int(os.environ.get('PORT', 8080))
        logger.info(f"Starting HTTP server on port {port}")
        
        # Start the server
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', port)
        server_task = asyncio.create_task(site.start())
        
        # Wait for tasks
        await asyncio.gather(monitor_task, server_task)
        
    except Exception as e:
        logger.error(f"Error in main: {e}")
    finally:
        # Clean up resources
        await cleanup_async_resources()

if __name__ == "__main__":
    # Set start time
    start_time = time.time()
    
    # Run main
    asyncio.run(main())
