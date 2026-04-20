"""One-time OAuth consent flow CLI. Prints a refresh token to stdout.

Usage:
    GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=... \
        python -m gsuite_mcp.auth_setup
"""

import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

from gsuite_mcp.auth import SCOPES


def main() -> int:
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        print(
            "ERROR: Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET "
            "before running this command.\n"
            "Create a Desktop OAuth client at "
            "https://console.cloud.google.com/apis/credentials",
            file=sys.stderr,
        )
        return 1

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    print("\n=== SUCCESS ===")
    print("Set this in your environment:")
    print(f"\nexport GOOGLE_OAUTH_REFRESH_TOKEN='{creds.refresh_token}'\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
