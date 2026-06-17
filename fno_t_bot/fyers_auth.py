"""
FYERS Authentication Module
"""

import os
from fyers_apiv3 import fyersModel
import config
import webbrowser
from datetime import datetime

class FyersAuth:
    def __init__(self):
        self.app_id = config.FYERS_APP_ID
        self.secret_key = config.FYERS_SECRET_KEY
        self.redirect_uri = config.REDIRECT_URI
        self.fyers = None
        self.access_token = None
        
    def generate_auth_url(self):
        """Generate authorization URL"""
        session = fyersModel.SessionModel(
            client_id=self.app_id,
            secret_key=self.secret_key,
            redirect_uri=self.redirect_uri,
            response_type="code",
            grant_type="authorization_code"
        )
        
        auth_url = session.generate_authcode()
        print("\n" + "="*70)
        print("FYERS AUTHENTICATION")
        print("="*70)
        print("\n1. Opening browser for FYERS login...")
        print("2. Login with your FYERS credentials")
        print("3. After login, you'll be redirected")
        print("4. Copy the ENTIRE URL from browser address bar")
        print("\n" + "="*70)
        
        webbrowser.open(auth_url)
        return auth_url
    
    def generate_access_token(self, auth_code):
        """Generate access token from auth code"""
        session = fyersModel.SessionModel(
            client_id=self.app_id,
            secret_key=self.secret_key,
            redirect_uri=self.redirect_uri,
            response_type="code",
            grant_type="authorization_code"
        )
        
        session.set_token(auth_code)
        response = session.generate_token()
        
        if response.get('code') == 200:
            self.access_token = response['access_token']
            print("\n✓ Successfully authenticated!")
            
            # Save token to file
            os.makedirs('logs', exist_ok=True)
            with open('logs/token.txt', 'w') as f:
                f.write(self.access_token + '\n')
                f.write(datetime.now().isoformat())
            print("✓ Token saved to logs/token.txt")
            
            return self.access_token
        else:
            print(f"\n✗ Authentication failed!")
            print(f"Error: {response}")
            return None
    
    def get_fyers_client(self):
        """Get authenticated FYERS client"""
        if not self.access_token:
            print("No access token!")
            return None
        
        self.fyers = fyersModel.FyersModel(
            client_id=self.app_id,
            token=self.access_token,
            log_path="logs/"
        )
        
        return self.fyers
    
    def test_connection(self):
        """Test connection by fetching data"""
        if not self.fyers:
            return False
        
        try:
            data = {
                "symbol": "NSE:NIFTY50-INDEX",
                "resolution": "D",
                "date_format": "1",
                "range_from": "2025-02-25",
                "range_to": "2025-02-26",
                "cont_flag": "1"
            }
            
            response = self.fyers.history(data)
            
            if response.get('s') == 'ok':
                print("\n" + "="*70)
                print("✓ CONNECTION SUCCESSFUL")
                print("="*70)
                print(f"✓ Data fetch working")
                print(f"✓ Candles received: {len(response.get('candles', []))}")
                print("="*70 + "\n")
                return True
            else:
                print(f"Connection test failed: {response}")
                return False
        except Exception as e:
            print(f"Connection error: {e}")
            return False

def authenticate():
    """Main authentication flow"""
    auth = FyersAuth()
    
    # Generate auth URL
    auth.generate_auth_url()
    
    print("\nPaste the redirect URL:")
    redirect_url = input("URL: ").strip()
    
    # Extract auth code
    try:
        if "auth_code=" in redirect_url:
            auth_code = redirect_url.split("auth_code=")[1].split("&")[0]
            print(f"Auth code extracted: {auth_code[:15]}...")
        else:
            print("✗ No auth_code found in URL")
            return None
    except Exception as e:
        print(f"✗ Error extracting auth code: {e}")
        return None
    
    # Generate token
    token = auth.generate_access_token(auth_code)
    if not token:
        return None
    
    # Get client
    client = auth.get_fyers_client()
    
    # Test
    if auth.test_connection():
        return client
    else:
        print("\n⚠️  Token saved but connection test failed")
        print("This might work when fetching live data")
        return client

if __name__ == "__main__":
    print("FYERS Authentication Test\n")
    client = authenticate()
    
    if client:
        print("✓ Authentication complete!")
        print("✓ Token saved - you can now run bot.py")
    else:
        print("\n✗ Authentication failed")