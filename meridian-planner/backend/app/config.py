from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Server-side settings. Secrets come from the environment only and are never sent to the browser."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="MERIDIAN_", extra="ignore")

    database_url: str = "postgresql+psycopg://meridian:meridian@localhost:5432/meridian"

    # Fernet key used to encrypt OAuth tokens at rest. Several keys may be given, comma separated;
    # the first encrypts, all decrypt (key rotation).
    token_encryption_keys: str = ""

    google_client_id: str = ""
    google_client_secret: str = ""
    # Must match an Authorized redirect URI in the Google Cloud console.
    google_redirect_uri: str = "http://localhost:3000/api/auth/google/callback"
    google_auth_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_token_url: str = "https://oauth2.googleapis.com/token"
    google_userinfo_url: str = "https://openidconnect.googleapis.com/v1/userinfo"
    google_calendar_api: str = "https://www.googleapis.com/calendar/v3"

    frontend_url: str = "http://localhost:3000"
    session_cookie_name: str = "meridian_session"
    session_days: int = 30
    cookie_secure: bool = False

    # Lets you sign in with just an email on a local machine, without Google. Never enable in production.
    dev_login_enabled: bool = False

    # How far back and forward the first calendar sync reads.
    sync_past_days: int = 30
    sync_future_days: int = 180

    # Public HTTPS address of the API (e.g. a Tailscale Funnel URL). When set, the app asks Google for push
    # notifications at {public_url}/webhooks/google; when empty, calendars are polled every 10 minutes.
    public_url: str = ""
    # Most Google Calendar writes per minute (stays well inside Google's per-user quota).
    google_writes_per_min: int = 120


@lru_cache
def get_settings() -> Settings:
    return Settings()
