#!/usr/bin/env python3
"""Quick test to diagnose Google Sheets connection issues."""
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
CREDENTIALS_PATH = PROJECT_ROOT / "credentials.json"
CONFIG_PATH = PROJECT_ROOT / "api_keys.json"


def test_connection():
    """Test Google Sheets access and report detailed errors."""
    print("1. Loading config...")
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    
    creds_path = PROJECT_ROOT / (config.get("google_credentials") or "credentials.json")
    spreadsheet_id = config.get("spreadsheet_id")
    
    print(f"   Credentials: {creds_path} (exists: {creds_path.exists()})")
    print(f"   Spreadsheet ID: {spreadsheet_id}")
    
    print("\n2. Loading credentials...")
    with open(creds_path) as f:
        creds = json.load(f)
    print(f"   Service account: {creds.get('client_email')}")
    print(f"   Project: {creds.get('project_id')}")
    
    print("\n3. Connecting to Google Sheets...")
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        
        SCOPES = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        gc = gspread.authorize(
            Credentials.from_service_account_file(str(creds_path), scopes=SCOPES)
        )
        print("   ✓ Authenticated")
        
        print("\n4. Opening spreadsheet...")
        spreadsheet = gc.open_by_key(spreadsheet_id)
        print(f"   ✓ Opened: {spreadsheet.title}")
        
        print("\n5. Listing worksheets...")
        for i, ws in enumerate(spreadsheet.worksheets()):
            print(f"   - {ws.title} (gid: {ws.id})")
        
        print("\n✅ All checks passed! The bot should work.")
        
    except Exception as e:
        print(f"\n❌ Error: {type(e).__name__}: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"   Status: {e.response.status_code}")
            try:
                body = e.response.json()
                print(f"   Details: {body.get('error', {}).get('message', body)}")
            except Exception:
                pass
        raise


if __name__ == "__main__":
    test_connection()
