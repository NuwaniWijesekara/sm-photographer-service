from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url:       str = "postgresql://postgres:postgres@postgres:5432/scanme_db"
    redis_url:          str = "redis://redis:6379/0"
    # Required — no default, so a missing JWT_SECRET fails at startup instead
    # of silently signing tokens with a publicly known value.
    jwt_secret:         str
    jwt_algorithm:      str = "HS256"
    aws_access_key_id:    str
    aws_secret_access_key: str
    aws_region:           str = "eu-north-1"
    s3_bucket_name:       str
    jwt_expire_minutes: int = 10080
    frontend_origin:    str = "http://localhost:3000"
    # Public base URL of the frontend, used to build the guest gallery link
    # in invitation emails (FRONTEND_ORIGIN above is a CORS allow-list, not
    # necessarily a single public URL).
    frontend_public_url: str = "http://localhost:3000"
    # Redis Stream consumed by sm-notification-service.
    guest_invited_stream: str = "guest.invited"
    api_gateway_url:    str = "http://api-gateway:8000"
    google_client_id:   str = ""
    # Lifetime of presigned photo URLs in the owner gallery (same default as
    # sm-guest-service's PHOTO_URL_TTL_SECONDS).
    photo_url_ttl_seconds: int = 900

settings = Settings()