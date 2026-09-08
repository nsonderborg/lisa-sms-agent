import os
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from dotenv import load_dotenv
load_dotenv() # Dette læser din .env fil

# De scopes Lisa skal bruge for at læse og skrive i kalenderen
SCOPES = ['https://www.googleapis.com/auth/calendar']

def authenticate():
    creds = None
    # Tjek om vi allerede har en token
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Her bruger vi de info du har i din .env
            client_id = os.getenv("GOOGLE_SERVICE_ACCOUNT_OAUTH2_CLIENT_ID")
            client_secret = os.getenv("GOOGLE_SERVICE_ACCOUNT_OAUTH2_CLIENT_SECRET")

            client_config = {
                "installed": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            creds = flow.run_local_server(port=0)

        # Gemmer din token til næste gang
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
        print("✅ Token genereret og gemt i token.json!")

if __name__ == "__main__":
    authenticate()