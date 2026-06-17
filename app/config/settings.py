from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url:       str = "postgresql://postgres:postgres@postgres:5432/scanme_db"
    redis_url:          str = "redis://redis:6379/0"
    jwt_secret:         str = "change_me_in_production"
    jwt_algorithm:      str = "HS256"
    jwt_expire_minutes: int = 10080
    frontend_origin:    str = "http://localhost:3000"

settings = Settings()