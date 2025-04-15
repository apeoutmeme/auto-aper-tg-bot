import asyncio
import aiohttp
import requests
import time
import random
import logging

# Initialize logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Cache for transaction data to avoid redundant API calls
tx_cache = {}

logged_mint_addresses = set()


# Function to retrieve transaction details
async def get_transaction_details_with_backoff(signature, max_retries=5):
    if signature in tx_cache:
        return tx_cache[signature]

    url = "https://mainnet.helius-rpc.com/?api-key=03227d28-b6de-4a36-9d90-cd0cc7c2f8eb"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
        ]
    }

    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as response:
                    if response.status == 200:
                        result = await response.json()
                        if 'result' in result and result['result']:
                            return result['result']
                        elif 'error' in result:
                            logger.warning(f"API error: {result['error']}")
                            if result['error'].get('code') == 429:
                                raise Exception("Rate limited")
                    elif response.status == 429:
                        raise Exception("Rate limited")
        except Exception as e:
            if attempt == max_retries - 1:
                logger.error(f"Failed to fetch transaction details after {max_retries} attempts: {str(e)}")
                return None
            sleep_time = (2 ** attempt) + random.uniform(0, 1)
            logger.warning(f"Attempt {attempt + 1} failed. Retrying in {sleep_time:.2f} seconds...")
            await asyncio.sleep(sleep_time)

    return None

def extract_mint_address(transaction_details):
    """
    Extract the mint address from the transaction details.
    If the mint address ends with 'pump', also flag it as a pump.fun token.
    """
    mint_address = None
    pump_fun_token = False

    # Traverse through the parsed instructions and check for mint addresses
    if 'transaction' in transaction_details and 'message' in transaction_details['transaction']:
        message = transaction_details['transaction']['message']
        
        # Check if there's a parsed section with mint info
        if 'accountKeys' in message:
            for account in message['accountKeys']:
                if 'pubkey' in account:
                    if 'pump' in account['pubkey']:
                        pump_fun_token = True
                    if len(account['pubkey']) >= 4 and account['pubkey'].endswith('pump'):
                        pump_fun_token = True
                        mint_address = account['pubkey']
                        break

        # Check if postTokenBalances contain the mint address
        if not mint_address and 'postTokenBalances' in transaction_details['meta']:
            for balance in transaction_details['meta']['postTokenBalances']:
                if 'mint' in balance:
                    mint_address = balance['mint']
                    if mint_address.endswith('pump'):
                        pump_fun_token = True
                    break

    return mint_address, pump_fun_token

# Example usage for testing
if __name__ == "__main__":
    signature = "29ghLDt8nuoei3umKFzcJ9Jt9BvQSA4aDCV7ytrSWZfrH3C75SvvBa9F5cWFDf5FFripie2xEaDNZy2JVdMWgKAB"
    transaction_details = get_transaction_details_with_backoff(signature)

    if transaction_details:
        mint_address, is_pump_fun = extract_mint_address(transaction_details)
        
        if mint_address and not logged_mint_addresses.get(mint_address):
            logged_mint_addresses[mint_address] = True  # Track processed addresses to avoid duplicates
            if is_pump_fun:
                print(f"Pump.fun token detected! Mint address: {mint_address}")
            else:
                print(f"Mint address: {mint_address}")
        else:
            print("No mint address found in the transaction details.")
    else:
        print("Failed to retrieve transaction details.")
