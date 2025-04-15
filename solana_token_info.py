import aiohttp
import asyncio
import logging

logger = logging.getLogger(__name__)

async def get_token_info(mint_address):
    url = "https://api.mainnet-beta.solana.com"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [
            mint_address,
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
                            token_info = account_info['data']['parsed']['info']
                            return {
                                'mint_authority': token_info.get('mintAuthority'),
                                'supply': token_info.get('supply'),
                                'decimals': token_info.get('decimals'),
                                'is_initialized': token_info.get('isInitialized'),
                                'freeze_authority': token_info.get('freezeAuthority')
                            }
    except Exception as e:
        logger.error(f"Error fetching token info: {e}")

    return None

async def get_token_metadata(mint_address):
    url = "https://api.mainnet-beta.solana.com"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenLargestAccounts",
        "params": [mint_address]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    if 'result' in result and result['result']['value']:
                        largest_account = result['result']['value'][0]['address']
                        metadata = await get_metadata_pda(mint_address, largest_account)
                        return metadata
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