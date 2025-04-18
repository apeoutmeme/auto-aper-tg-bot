import requests
import random
import os
import logging
from dotenv import load_dotenv

# Load environment variables - you can put your Unsplash API key in a .env file
load_dotenv()

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class UnsplashImageFetcher:
    def __init__(self, access_key=None):
        # Use provided key or get from environment variables
        self.access_key = access_key or os.getenv('UNSPLASH_ACCESS_KEY')
        if not self.access_key:
            raise ValueError("Unsplash API access key is required")
        
        self.api_base_url = "https://api.unsplash.com"
        self.headers = {
            "Authorization": f"Client-ID {self.access_key}",
            "Accept-Version": "v1"
        }
        
        # Cache to avoid duplicate API calls for the same search terms
        self.image_cache = {}

    def get_related_image(self, token_name, token_symbol=None, fallback_terms=None):
        """
        Get a related image URL based on token name and symbol
        
        Args:
            token_name (str): Name of the token/memecoin
            token_symbol (str, optional): Symbol of the token
            fallback_terms (list, optional): List of fallback search terms to try if main search fails
            
        Returns:
            dict: Contains image data including URLs and attribution
        """
        try:
            # Clean up the token name (remove special characters)
            clean_name = ''.join(c for c in token_name if c.isalnum() or c.isspace()).strip()
            
            # Create search terms from token name and symbol
            search_terms = [clean_name]
            
            # Add token symbol if provided
            if token_symbol:
                clean_symbol = ''.join(c for c in token_symbol if c.isalnum()).strip()
                if clean_symbol and clean_symbol != clean_name:
                    search_terms.append(clean_symbol)
            
            # Try each search term until we find an image
            for term in search_terms:
                # Check cache first
                if term in self.image_cache:
                    logger.info(f"Using cached image for '{term}'")
                    return self.image_cache[term]
                
                # Search for images
                image_data = self._search_unsplash(term)
                if image_data:
                    # Cache the result
                    self.image_cache[term] = image_data
                    return image_data
            
            # If we get here, try fallback terms
            if fallback_terms:
                for term in fallback_terms:
                    if term in self.image_cache:
                        logger.info(f"Using cached fallback image for '{term}'")
                        return self.image_cache[term]
                    
                    image_data = self._search_unsplash(term)
                    if image_data:
                        self.image_cache[term] = image_data
                        return image_data
            
            # If nothing works, use a generic crypto image
            return self._search_unsplash("cryptocurrency")
        
        except Exception as e:
            logger.error(f"Error getting image for {token_name}: {str(e)}")
            # Return a default cryptocurrency image as fallback
            return {
                "url": "https://images.unsplash.com/photo-1621761191319-c6fb62004040",  # Generic crypto image
                "download_url": "https://images.unsplash.com/photo-1621761191319-c6fb62004040",
                "author": "Unsplash",
                "author_url": "https://unsplash.com"
            }

    def _search_unsplash(self, search_term):
        """Search Unsplash for images matching the search term"""
        try:
            logger.info(f"Searching Unsplash for: '{search_term}'")
            
            # Construct the search endpoint
            endpoint = f"{self.api_base_url}/search/photos"
            
            # Define search parameters
            params = {
                "query": search_term,
                "per_page": 30,  # Get multiple results to choose from
                "orientation": "landscape"  # Better for most display contexts
            }
            
            # Make the API request
            response = requests.get(endpoint, headers=self.headers, params=params)
            response.raise_for_status()  # Raise an exception for HTTP errors
            
            # Process the response
            data = response.json()
            results = data.get("results", [])
            
            if not results:
                logger.warning(f"No images found for '{search_term}'")
                return None
            
            # Pick a random image from the results
            random_image = random.choice(results)
            
            # Extract relevant information
            image_data = {
                "url": random_image["urls"]["regular"],
                "download_url": random_image["links"]["download"],
                "author": random_image["user"]["name"],
                "author_url": random_image["user"]["links"]["html"],
                "description": random_image.get("description") or random_image.get("alt_description"),
                "unsplash_url": random_image["links"]["html"]
            }
            
            return image_data
            
        except Exception as e:
            logger.error(f"Error searching Unsplash: {str(e)}")
            return None

    def generate_html_for_image(self, image_data, token_name, token_symbol=None):
        """Generate HTML for displaying the image with proper attribution"""
        if not image_data:
            return ""
        
        html = f"""
        <div class="token-image-container">
            <img src="{image_data['url']}" alt="{token_name} ({token_symbol or ''})" class="token-image" />
            <div class="attribution">
                Photo by <a href="{image_data['author_url']}" target="_blank">{image_data['author']}</a> on <a href="https://unsplash.com/" target="_blank">Unsplash</a>
            </div>
        </div>
        """
        return html


# Test function to demonstrate the script
def test_image_fetcher():
    """Test the image fetcher with some example tokens"""
    # You can set your key directly here for testing (but better to use .env)
    UNSPLASH_KEY = os.getenv('UNSPLASH_ACCESS_KEY')
    
    # Create the image fetcher
    fetcher = UnsplashImageFetcher(access_key=UNSPLASH_KEY)
    
    # Test with some sample token names
    test_tokens = [
        {"name": "Mandela Effect", "symbol": "mandela"},
        {"name": "Doge", "symbol": "DOGE"},
        {"name": "Pepe", "symbol": "PEPE"},
        {"name": "Shiba Inu", "symbol": "SHIB"},
        {"name": "Moon", "symbol": "MOON"}
    ]
    
    # Define some fallback terms
    fallback_terms = ["meme", "cryptocurrency", "trading", "funny animal"]
    
    # Try each token
    for token in test_tokens:
        print(f"\nSearching for image related to {token['name']} ({token['symbol']})...")
        
        # Get an image
        image_data = fetcher.get_related_image(token['name'], token['symbol'], fallback_terms)
        
        if image_data:
            print(f"Found image: {image_data['url']}")
            print(f"By: {image_data['author']}")
            print(f"Description: {image_data.get('description', 'No description')}")
            
            # Generate HTML
            html = fetcher.generate_html_for_image(image_data, token['name'], token['symbol'])
            print("\nHTML for display:")
            print(html)
        else:
            print("No image found!")


if __name__ == "__main__":
    # Run the test function
    test_image_fetcher() 
