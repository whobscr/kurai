"""The environment: what Kurai needs before its database is reachable (everything else is in the settings table).

Names and meanings are documented in .env.example.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KURAI_", env_file=".env", extra="ignore")

    secret_key: str = Field(min_length=16)
    database_url: str = "postgresql+psycopg://kurai:kurai@postgres:5432/kurai"
    base_url: str = "http://localhost:8095"
    listen: str = "0.0.0.0"
    web_port: int = 8095

    navidrome_url: str = "http://navidrome:4533"

    # the audio analysis service (the `audio` profile); its own port, models folder and CPU threads
    audio_url: str = "http://audio:8097"
    audio_port: int = 8097
    audio_models_dir: str = "/models"
    audio_threads: int = 2

    # first values of the system settings (the settings page wins once saved)
    llm_base_url: str = "http://llama:8080/v1"
    llm_api_key: str = ""
    llm_model: str = "qwen3"
    embed_base_url: str = "http://llama-embed:8080/v1"
    embed_api_key: str = ""
    embed_model: str = "qwen3-embedding"
    outbound_proxy: str = ""
    telegram_bot_token: str = ""


@lru_cache
def get_config() -> Config:
    return Config()
