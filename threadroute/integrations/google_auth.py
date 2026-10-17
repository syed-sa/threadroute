"""OAuth helper for the optional live Gmail + Sheets integrations (installed-app flow).

One-time setup:  python -m threadroute google-auth
Needs GOOGLE_OAUTH_CREDENTIALS=<path to OAuth client json>. The token is cached in .threadroute/token.json.
"""

from __future__ import annotations

from threadroute.config import get_settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/spreadsheets",
]


def get_credentials(interactive: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    s = get_settings()
    token_path = s.home / "token.json"
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not interactive:
            raise RuntimeError("Google OAuth token missing. Run: python -m threadroute google-auth")
        if not s.oauth_credentials:
            raise RuntimeError("Set GOOGLE_OAUTH_CREDENTIALS to your OAuth client JSON path first.")
        flow = InstalledAppFlow.from_client_secrets_file(s.oauth_credentials, SCOPES)
        creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json())
    return creds
