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
    google_revoke_url: str = "https://oauth2.googleapis.com/revoke"
    google_calendar_api: str = "https://www.googleapis.com/calendar/v3"

    frontend_url: str = "http://localhost:3000"
    session_cookie_name: str = "meridian_session"
    session_days: int = 30
    cookie_secure: bool = False

    # Comma-separated Google addresses allowed to sign in (e.g. "you@gmail.com,you@firm.com"). Empty means anyone
    # with a Google account can create an account: fine on a private network, required in production.
    # Extra Google accounts connected from Settings by a signed-in user can sign in too.
    allowed_emails: str = ""

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

    # AI assistant. "ollama" (default, runs on your own machine), "claude" (Anthropic API, needs a key) or "none".
    # The scheduler never depends on it.
    ai_provider: str = "ollama"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:8b"
    # Let qwen3 think before answering (slower, sometimes better at multi-step requests).
    ollama_think: bool = False
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-5-5"
    ai_timeout_s: float = 120.0

    # "development" or "production". Production refuses to start with unsafe settings (dev login, insecure cookies).
    env: str = "development"
    # "text" for people, "json" for log collectors.
    log_format: str = "text"
    # Per-user and per-address request limits (see app/ratelimit.py).
    rate_limits_enabled: bool = True
    # Largest request body accepted, in bytes (CSV imports are the biggest).
    max_body_bytes: int = 6_000_000

    # Email (notifications and the daily digest). Gmail: smtp.gmail.com, port 587, an app password.
    # email_backend: "smtp", "file" (writes .eml files to mail_dir, for testing), "log" or "none".
    # Empty means "smtp" when smtp_host is set, otherwise "none".
    email_backend: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    email_from: str = ""
    mail_dir: str = "/tmp/meridian-mail"

    # Browser push (Web Push with VAPID). Generate a pair with `python -m app.cli vapid`. Push needs HTTPS
    # (a Tailscale ts.net address works) or localhost.
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:admin@localhost"


@lru_cache
def get_settings() -> Settings:
    return Settings()
