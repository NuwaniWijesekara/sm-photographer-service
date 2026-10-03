from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url:       str = "postgresql://postgres:postgres@postgres:5432/scanme_db"
    redis_url:          str = "redis://redis:6379/0"
    jwt_secret:         str = "change_me_in_production"
    jwt_algorithm:      str = "HS256"
    aws_access_key_id:    str
    aws_secret_access_key: str
    aws_region:           str = "eu-north-1"
    s3_bucket_name:       str
    jwt_expire_minutes: int = 10080
    frontend_origin:    str = "http://localhost:3000"
    api_gateway_url:    str = "http://api-gateway:8000"
    google_client_id:   str = ""
    # Lifetime of presigned photo URLs in the owner gallery (same default as
    # sm-guest-service's PHOTO_URL_TTL_SECONDS).
    photo_url_ttl_seconds: int = 900

settings = Settings()