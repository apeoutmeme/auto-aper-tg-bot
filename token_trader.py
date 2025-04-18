import time
import asyncio
import logging
from datetime import datetime, timedelta

# Configure logging
logger = logging.getLogger('token_trader')

class TokenTrader:
    """
    Handles token evaluation, buying decisions, and position management
    for automated memecoin trading based on trending criteria.
    """
    
    def __init__(self, telegram_sender=None, swap_function=None):
        """
        Initialize the TokenTrader with required dependencies.
        
        Args:
            telegram_sender: Function to send Telegram messages
            swap_function: Function to execute token swaps
        """
        self.telegram_sender = telegram_sender
        self.swap_function = swap_function
        
        # Trading configuration
        self.auto_trading_enabled = True
        self.trading_budget_per_day = 0.05  # Maximum SOL to spend per day
        self.spent_today = 0  # Track daily spending
        self.last_spending_reset = time.time()  # For daily budget reset
        self.minimum_buy_score = 60  # Minimum score to trigger a buy (0-100)
        
        # Risk management settings
        self.max_investment_per_token = 0.01  # Maximum SOL per token
        self.min_investment_per_token = 0.001  # Minimum SOL per token
        
        # Token evaluation keywords (from memecoin trends)
        self.viral_keywords = [
            'doge', 'pepe', 'shib', 'bonk', 'trump', 'ai', 'elon', 'chad', 'cat', 
            'dog', 'moon', 'giga', 'meme', 'based', 'wojak', 'sigma', 'coin',
            'inu', 'floki', 'biden', 'musk', 'crypto', 'gpt', 'game', 'metaverse',
            'pop', 'viral', 'tiktok', 'bome', 'wif', 'buttcoin', 'titcoin', 'figure'
        ]
        
        # Trading history
        self.trading_history = []
    
    def evaluate_token_potential(self, mint_address, token_info, dexscreener_data):
        """
        Evaluates a token's potential based on multiple factors from the memecoin guidelines.
        Returns a score and decision data.
        
        Args:
            mint_address: Token address
            token_info: Basic token information
            dexscreener_data: Market data from DexScreener
            
        Returns:
            tuple: (score, evaluation_dict)
        """
        score = 0
        evaluation = {
            'buy_decision': False,
            'reasons': [],
            'risk_level': 'high',
            'viral_potential': 'low',
            'confidence': 0
        }
        
        # Skip if price or liquidity data is missing
        if not dexscreener_data:
            evaluation['reasons'].append("Missing market data")
            return score, evaluation
        
        # 1. Check name and symbol for viral potential
        token_name = dexscreener_data.get('baseToken', {}).get('name', '').lower()
        token_symbol = dexscreener_data.get('baseToken', {}).get('symbol', '').lower()
        
        # Check for viral keywords in name or symbol
        keyword_matches = []
        for keyword in self.viral_keywords:
            if keyword in token_name or keyword in token_symbol:
                keyword_matches.append(keyword)
                score += 10
        
        if keyword_matches:
            evaluation['reasons'].append(f"Viral keywords: {', '.join(keyword_matches)}")
            evaluation['viral_potential'] = 'medium' if len(keyword_matches) == 1 else 'high'
        
        # 2. Check liquidity (higher liquidity = less rugpull risk)
        liquidity_usd = dexscreener_data.get('liquidity', {}).get('usd', 0)
        if isinstance(liquidity_usd, (int, float)):
            if liquidity_usd > 10000:  # $10k minimum
                score += 15
                evaluation['reasons'].append(f"Good liquidity: ${liquidity_usd}")
                
                if liquidity_usd > 50000:  # $50k is even better
                    score += 10
                    evaluation['risk_level'] = 'medium'
        else:
            evaluation['reasons'].append("Cannot determine liquidity")
        
        # 3. Check market activity (buys vs sells)
        buys = dexscreener_data.get('txns', {}).get('h24', {}).get('buys', 0)
        sells = dexscreener_data.get('txns', {}).get('h24', {}).get('sells', 0)
        
        if buys > sells and buys > 10:
            score += 20
            evaluation['reasons'].append(f"Positive buy pressure: {buys} buys vs {sells} sells")
        
        # 4. Check price action
        price_change = dexscreener_data.get('priceChange', {}).get('h24', 0)
        if isinstance(price_change, (int, float)) and price_change > 20:
            score += 15
            evaluation['reasons'].append(f"Strong momentum: {price_change}% 24h change")
        
        # 5. Age of token (newer tokens have more potential)
        pair_created_at = dexscreener_data.get('pairCreatedAt', 0)
        if pair_created_at:
            age_hours = (time.time() - (pair_created_at/1000)) / 3600
            if age_hours < 24:  # Less than 24 hours old
                score += 20
                evaluation['reasons'].append(f"Fresh token: {age_hours:.1f} hours old")
        
        # Set confidence level based on score
        evaluation['confidence'] = min(score, 100)
        
        # Make buy decision (score threshold)
        if score >= self.minimum_buy_score:
            evaluation['buy_decision'] = True
            logger.info(f"Token {mint_address} meets buy criteria with score {score}/100")
        
        return score, evaluation
    
    def calculate_investment_amount(self, score, evaluation):
        """
        Calculate the investment amount based on token evaluation and risk management guidelines.
        Uses dollar-cost averaging and risk-based allocation from memecoin strategies.
        
        Args:
            score: Token evaluation score
            evaluation: Evaluation dictionary with risk level
            
        Returns:
            float: Investment amount in SOL
        """
        # Base investment
        max_investment = self.max_investment_per_token
        min_investment = self.min_investment_per_token
        
        # Adjust investment based on score (higher score = more confidence)
        investment_multiplier = score / 100
        
        # Apply risk adjustment
        risk_multiplier = 0.5  # Default for high risk
        if evaluation['risk_level'] == 'medium':
            risk_multiplier = 0.7
        elif evaluation['risk_level'] == 'low':
            risk_multiplier = 1.0
        
        # Calculate final investment
        investment = max_investment * investment_multiplier * risk_multiplier
        
        # Ensure minimum threshold
        investment = max(min_investment, investment)
        
        # Don't exceed maximum
        investment = min(max_investment, investment)
        
        logger.info(f"Calculated investment: {investment} SOL based on score {score}/100")
        return investment
    
    def check_daily_budget(self, amount):
        """
        Check if a purchase fits within the daily budget
        
        Args:
            amount: The investment amount to check
            
        Returns:
            bool: True if within budget, False otherwise
        """
        # If auto-trading is disabled, always return False
        if not self.auto_trading_enabled:
            return False
            
        # Reset budget if it's a new day
        current_time = time.time()
        time_since_last_reset = current_time - self.last_spending_reset
        
        if time_since_last_reset >= 86400:  # 24 hours in seconds
            logger.info(f"Resetting daily trading budget. Previous spending: {self.spent_today} SOL")
            self.spent_today = 0
            self.last_spending_reset = current_time
            
        # If adding this amount would exceed daily budget, return False
        if self.spent_today + amount > self.trading_budget_per_day:
            logger.info(f"Daily budget limit reached. Cannot spend {amount} SOL (spent: {self.spent_today}, limit: {self.trading_budget_per_day})")
            return False
            
        return True
    
    def update_spending(self, amount):
        """Update the daily spending tracker"""
        self.spent_today += amount
        logger.info(f"Updated daily spending: {self.spent_today}/{self.trading_budget_per_day} SOL")
    
    async def process_token_for_trading(self, mint_address, token_data, dexscreener_data, get_price_func):
        """
        Process a token to determine if it should be bought based on trading criteria
        
        Args:
            mint_address: Token address
            token_data: Basic token information
            dexscreener_data: Market data from DexScreener
            get_price_func: Function to get current token price
            
        Returns:
            dict: Trading decision and related information
        """
        if not self.telegram_sender or not self.swap_function:
            logger.error("Cannot process trading - telegram_sender or swap_function not set")
            return {"error": "Trading functionality not fully configured"}
        
        try:
            # Extract token info
            token_name = dexscreener_data.get('baseToken', {}).get('name', 'Unknown')
            token_symbol = dexscreener_data.get('baseToken', {}).get('symbol', 'Unknown')
            price_usd = dexscreener_data.get('priceUsd', 'Unknown')
            
            # Evaluate token potential
            score, evaluation = self.evaluate_token_potential(
                mint_address, token_data, dexscreener_data
            )
            
            # If token doesn't meet buying criteria or auto-trading is disabled
            if not evaluation['buy_decision'] or not self.auto_trading_enabled:
                logger.info(f"Token {token_name} ({token_symbol}) doesn't meet buying criteria. Score: {score}/100")
                return {
                    "decision": "no_buy",
                    "score": score,
                    "evaluation": evaluation
                }
            
            # Calculate investment amount
            investment_amount = self.calculate_investment_amount(score, evaluation)
            
            # Check if within daily budget
            if not self.check_daily_budget(investment_amount):
                logger.info(f"Skipping buy for {mint_address} due to daily budget constraints")
                await self.telegram_sender(f"""<b>⚠️ Trading Limit Reached</b>
                
Found promising token {token_name} ({token_symbol}) but daily trading budget has been reached.
Score: {score}/100
Budget: {self.spent_today}/{self.trading_budget_per_day} SOL used today
""")
                return {
                    "decision": "budget_limit",
                    "score": score,
                    "evaluation": evaluation
                }
            
            # Send buy message
            buy_message = f"""<b>🤖 AutoTrader Decision</b>

<b>Token:</b> {token_name} ({token_symbol})
<b>Decision:</b> BUY
<b>Score:</b> {score}/100
<b>Amount:</b> {investment_amount} SOL
<b>Reasons:</b>
{chr(10).join(['• ' + reason for reason in evaluation['reasons']])}

<b>Processing transaction...</b>
"""
            await self.telegram_sender(buy_message)
            
            # Execute the buy transaction
            swap_result = await self.swap_function(mint_address, "buy", investment_amount)
            
            # Update spending tracker
            self.update_spending(investment_amount)
            
            # Add to trading history
            trade_info = {
                'action': 'buy',
                'token_address': mint_address,
                'token_name': token_name,
                'token_symbol': token_symbol,
                'amount': investment_amount,
                'price': price_usd,
                'score': score,
                'timestamp': time.time()
            }
            self.trading_history.append(trade_info)
            
            # Send a follow-up message with the result
            result_message = f"""<b>🔄 Transaction Result</b>

<b>Token:</b> {token_name} ({token_symbol})
<b>Amount:</b> {investment_amount} SOL
<b>Result:</b> {swap_result}
<b>Token Address:</b> <code>{mint_address}</code>
<b>Daily Budget:</b> {self.spent_today}/{self.trading_budget_per_day} SOL

<i>Starting ROI monitoring...</i>
"""
            await self.telegram_sender(result_message)
            
            # Return success information
            return {
                "decision": "buy",
                "amount": investment_amount,
                "score": score, 
                "token_info": {
                    "name": token_name,
                    "symbol": token_symbol,
                    "address": mint_address
                },
                "trade_info": trade_info
            }
            
        except Exception as e:
            logger.error(f"Error in process_token_for_trading: {e}")
            return {
                "decision": "error",
                "error": str(e)
            }
    
    async def monitor_roi(self, mint_address, initial_investment, get_price_func):
        """
        Monitor return on investment for a token position,
        implementing trailing stop loss and dynamic profit taking
        
        Args:
            mint_address: Token address
            initial_investment: Amount invested in SOL
            get_price_func: Function to get current price
        """
        logger.info(f"Monitoring ROI for {mint_address} with initial investment of {initial_investment}")
        
        initial_price = await get_price_func(mint_address)
        if initial_price is None:
            logger.error("Failed to get initial price. Exiting ROI monitoring.")
            return
        
        # Initial target and stop loss settings
        target_price = initial_price * 2  # Target price for 100% ROI (2x)
        stop_loss_price = initial_price * 0.7  # Stop loss at 70% of initial price (30% loss)
        
        # For trailing stop loss
        highest_price = initial_price
        trailing_stop_pct = 0.25  # 25% trailing stop loss
        
        # Track monitoring status
        monitoring_active = True
        check_interval = 60  # Check every minute initially
        hours_monitored = 0
        
        while monitoring_active:
            current_price = await get_price_func(mint_address)
            if current_price is None:
                logger.error("Failed to get current price. Retrying...")
                await asyncio.sleep(check_interval)  # Wait before retrying
                continue
            
            # Calculate current ROI
            current_roi = (current_price - initial_price) / initial_price * 100
            logger.info(f"Current price for {mint_address}: {current_price}, Current ROI: {current_roi:.2f}%")
            
            # Update highest price and trailing stop if price increases
            if current_price > highest_price:
                highest_price = current_price
                # Update trailing stop loss
                trailing_stop = highest_price * (1 - trailing_stop_pct)
                # Only raise stop loss, never lower it
                if trailing_stop > stop_loss_price:
                    stop_loss_price = trailing_stop
                    logger.info(f"Updated trailing stop loss to {stop_loss_price} ({trailing_stop_pct*100}% below highest price)")
            
            # Check if stop loss is triggered
            if current_price <= stop_loss_price:
                logger.info(f"Stop loss triggered for {mint_address}. Selling...")
                sell_response = await self.swap_function(mint_address, action="sell", amount=initial_investment)
                logger.info(sell_response)
                sell_message = f"""<b>🔄 Sell Transaction (Stop Loss)</b>

Token: <code>{mint_address}</code>
Amount: {initial_investment} SOL
Result: {sell_response}
ROI: {current_roi:.2f}%
Reason: Stop loss triggered at {(stop_loss_price/initial_price - 1)*100:.2f}% of peak price
"""
                await self.telegram_sender(sell_message)
                monitoring_active = False
                
                # Add to trading history
                self.trading_history.append({
                    'action': 'sell',
                    'token_address': mint_address,
                    'amount': initial_investment,
                    'price': current_price,
                    'roi': current_roi,
                    'reason': 'stop_loss',
                    'timestamp': time.time()
                })
                
                break
            
            # Dynamic target adjustment based on ROI
            if current_roi >= 50:  # Over 50% profit
                # Gradually increase target as profit grows
                new_target = initial_price * (2 + (current_roi - 50) / 100)
                if new_target > target_price:
                    target_price = new_target
                    logger.info(f"Adjusted target price to {target_price} ({(target_price/initial_price - 1)*100:.2f}% ROI)")
            
            # Check if target is reached
            if current_price >= target_price:
                logger.info(f"Target ROI reached for {mint_address}. Selling...")
                sell_response = await self.swap_function(mint_address, action="sell", amount=initial_investment)
                logger.info(sell_response)
                sell_message = f"""<b>🔄 Sell Transaction (Target Reached)</b>

Token: <code>{mint_address}</code>
Amount: {initial_investment} SOL
Result: {sell_response}
ROI: {current_roi:.2f}%
Reason: Price target reached
"""
                await self.telegram_sender(sell_message)
                monitoring_active = False
                
                # Add to trading history
                self.trading_history.append({
                    'action': 'sell',
                    'token_address': mint_address,
                    'amount': initial_investment,
                    'price': current_price,
                    'roi': current_roi,
                    'reason': 'target_reached',
                    'timestamp': time.time()
                })
                
                break
            
            # Adapt monitoring frequency based on duration
            hours_monitored += check_interval / 3600
            if hours_monitored > 24:
                # After 24 hours, check less frequently
                check_interval = 300  # Every 5 minutes
            elif hours_monitored > 6:
                # After 6 hours, check less frequently
                check_interval = 180  # Every 3 minutes
            
            # Time limit - stop monitoring after 72 hours
            if hours_monitored > 72:
                logger.info(f"Monitoring time limit reached for {mint_address}. Closing position...")
                sell_response = await self.swap_function(mint_address, action="sell", amount=initial_investment)
                sell_message = f"""<b>🔄 Sell Transaction (Time Limit)</b>

Token: <code>{mint_address}</code>
Amount: {initial_investment} SOL
Result: {sell_response}
ROI: {current_roi:.2f}%
Reason: 72-hour monitoring period ended
"""
                await self.telegram_sender(sell_message)
                monitoring_active = False
                
                # Add to trading history
                self.trading_history.append({
                    'action': 'sell',
                    'token_address': mint_address,
                    'amount': initial_investment,
                    'price': current_price,
                    'roi': current_roi,
                    'reason': 'time_limit',
                    'timestamp': time.time()
                })
                
                break
            
            await asyncio.sleep(check_interval)
    
    async def generate_trading_summary(self):
        """Generate a summary of current trading activity"""
        # Gather trading stats
        total_trades = len(self.trading_history)
        recent_trades = [t for t in self.trading_history if time.time() - t['timestamp'] < 86400]
        
        # Calculate active tokens (bought but not sold)
        bought_tokens = set(t['token_address'] for t in self.trading_history if t['action'] == 'buy')
        sold_tokens = set(t['token_address'] for t in self.trading_history if t['action'] == 'sell')
        active_tokens = bought_tokens - sold_tokens
        
        total_invested = sum(t['amount'] for t in self.trading_history 
                            if t['action'] == 'buy' and t['token_address'] in active_tokens)
        
        # Prepare summary
        summary = f"""<b>📊 Trading Summary</b>

<b>Activity (Last 24 Hours):</b>
• Tokens Bought: {len([t for t in recent_trades if t['action'] == 'buy'])}
• Tokens Sold: {len([t for t in recent_trades if t['action'] == 'sell'])}
• Total Invested: {total_invested:.6f} SOL
• Budget Used: {self.spent_today:.6f}/{self.trading_budget_per_day} SOL

<b>Active Tokens:</b>
"""
        
        # Add active token details
        if active_tokens:
            for token_address in active_tokens:
                # Find the buy transaction for this token
                buy_tx = next((t for t in self.trading_history 
                              if t['token_address'] == token_address and t['action'] == 'buy'), None)
                
                if buy_tx:
                    summary += f"""
• {buy_tx.get('token_name', 'Unknown')} ({buy_tx.get('token_symbol', 'Unknown')}):
  Invested: {buy_tx['amount']:.6f} SOL | <a href="https://dexscreener.com/solana/{token_address}">Chart</a>
"""
        else:
            summary += "• No active tokens\n"
        
        summary += f"""
<b>All-Time Stats:</b>
• Total Trades: {total_trades}
• Trading Since: {datetime.fromtimestamp(self.trading_history[0]['timestamp']).strftime('%Y-%m-%d') if self.trading_history else 'No trades yet'}

<i>Generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>
"""
        
        return summary
    
    async def send_trading_summary(self, telegram_sender=None):
        """Send a trading summary"""
        try:
            summary = await self.generate_trading_summary()
            
            # Use provided sender or default
            sender = telegram_sender or self.telegram_sender
            if sender:
                await sender(summary)
            else:
                logger.error("No telegram sender available to send trading summary")
                
        except Exception as e:
            logger.error(f"Error sending trading summary: {e}")
