import aiohttp
import asyncio
import logging
import requests
import json
import base58
import base64
import time
from cachetools import TTLCache

logger = logging.getLogger(__name__)

# Create a cache to store token info
token_info_cache = TTLCache(maxsize=1000, ttl=3600)  # Cache for 1 hour
token_metadata_cache = TTLCache(maxsize=1000, ttl=3600)  # Cache for 1 hour

def get_token_info(mint_address, rpc_url="https://api.mainnet-beta.solana.com"):
    """Get token information for a given mint address."""
    # Check cache first
    if mint_address in token_info_cache:
        return token_info_cache[mint_address]
    
    try:
        # Prepare RPC request
        headers = {"Content-Type": "application/json"}
        data = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getAccountInfo",
            "params": [
                mint_address,
                {
                    "encoding": "jsonParsed"
                }
            ]
        }
        
        # Make request
        response = requests.post(rpc_url, headers=headers, json=data)
        response_data = response.json()
        
        # Check for errors in response
        if "error" in response_data:
            logger.error(f"RPC error for mint {mint_address}: {response_data['error']}")
            return None
        
        # Check if result is None or if value is None
        if not response_data.get("result") or not response_data["result"].get("value"):
            logger.warning(f"No data returned for mint {mint_address}")
            return None
            
        # Parse token info from response
        account_data = response_data["result"]["value"]
        if not account_data.get("data") or not account_data["data"].get("parsed"):
            logger.warning(f"No parsed data for mint {mint_address}")
            return None
            
        parsed_data = account_data["data"]["parsed"]
        
        # Check if the info property exists
        if not parsed_data.get("info"):
            logger.warning(f"No info data in parsed response for mint {mint_address}")
            return None
            
        # Extract token info
        token_info = parsed_data["info"]
        
        # Store in cache
        token_info_cache[mint_address] = token_info
        return token_info
        
    except Exception as e:
        logger.error(f"Error fetching token info: {e}")
        return None
        
def get_token_metadata(mint_address, rpc_url="https://api.mainnet-beta.solana.com"):
    """Get metadata for a token."""
    # Check cache first
    if mint_address in token_metadata_cache:
        return token_metadata_cache[mint_address]
        
    try:
        # First, find the metadata PDA for this mint
        metadata_program_id = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
        
        # Use getProgramAccounts to find the metadata account
        headers = {"Content-Type": "application/json"}
        data = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getProgramAccounts",
            "params": [
                metadata_program_id,
                {
                    "encoding": "jsonParsed",
                    "filters": [
                        {
                            "memcmp": {
                                "offset": 33,
                                "bytes": mint_address
                            }
                        }
                    ]
                }
            ]
        }
        
        # Make request
        response = requests.post(rpc_url, headers=headers, json=data)
        response_data = response.json()
        
        # Check for errors or empty response
        if "error" in response_data:
            logger.error(f"RPC error for metadata {mint_address}: {response_data['error']}")
            return None
            
        # Check if result exists and is not empty
        if not response_data.get("result") or not response_data["result"]:
            logger.warning(f"No metadata found for mint {mint_address}")
            return None
            
        # We should have the metadata account now
        accounts = response_data["result"]
        
        # If no accounts found, return None
        if not accounts:
            return None
            
        # Get the first account (should be only one matching our filter)
        account = accounts[0]
        
        # Extract and decode the data
        if not account.get("account") or not account["account"].get("data"):
            logger.warning(f"No data in metadata account for mint {mint_address}")
            return None
            
        data = account["account"]["data"]
        
        # Handle different encodings
        if isinstance(data, list) and len(data) >= 2:
            # This is for base64 encoding
            data_bytes = base64.b64decode(data[0])
        else:
            # Not sure what format, return None safely
            logger.warning(f"Unexpected data format in metadata for mint {mint_address}")
            return None
            
        # Parse metadata (this depends on the format of the metadata)
        # This is a simplified example - you may need to adjust based on actual format
        metadata = {}
        try:
            # Skip header bytes and try to extract name and symbol
            offset = 1 + 32 + 32  # Skip metadata prefix, update authority, mint
            
            # Extract name length and name
            name_len = int.from_bytes(data_bytes[offset:offset+4], byteorder='little')
            offset += 4
            name = data_bytes[offset:offset+name_len].decode('utf-8')
            offset += name_len
            
            # Extract symbol length and symbol
            symbol_len = int.from_bytes(data_bytes[offset:offset+4], byteorder='little')
            offset += 4
            symbol = data_bytes[offset:offset+symbol_len].decode('utf-8')
            offset += symbol_len
            
            # Extract URI length and URI
            uri_len = int.from_bytes(data_bytes[offset:offset+4], byteorder='little')
            offset += 4
            uri = data_bytes[offset:offset+uri_len].decode('utf-8')
            
            metadata = {
                "name": name.strip('\x00'),
                "symbol": symbol.strip('\x00'),
                "uri": uri.strip('\x00')
            }
            
            # Store in cache
            token_metadata_cache[mint_address] = metadata
            return metadata
            
        except Exception as e:
            logger.error(f"Error parsing metadata for mint {mint_address}: {e}")
            return None
            
    except Exception as e:
        logger.error(f"Error fetching token metadata: {e}")
        return None

async def get_metadata_pda(mint_address, token_account):
    url = "https://api.mainnet-beta.solana.com"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [
            token_account,
            {"encoding": "jsonParsed"}
        ]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    if 'result' in result and result['result']:
                        account_info = result['result']['value']
                        if 'data' in account_info and 'parsed' in account_info['data']:
                            metadata = account_info['data']['parsed']['info'].get('metadata')
                            if metadata:
                                return await get_metadata(metadata)
    except Exception as e:
        logger.error(f"Error fetching metadata PDA: {e}")

    return None

async def get_metadata(metadata_address):
    url = "https://api.mainnet-beta.solana.com"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [
            metadata_address,
            {"encoding": "base64"}
        ]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    if 'result' in result and result['result']:
                        account_info = result['result']['value']
                        if 'data' in account_info:
                            # Here you would need to parse the base64 data
                            # This is a simplified version
                            return {
                                'metadata_address': metadata_address,
                                'raw_data': account_info['data'][0]
                            }
    except Exception as e:
        logger.error(f"Error fetching metadata: {e}")

    return None
