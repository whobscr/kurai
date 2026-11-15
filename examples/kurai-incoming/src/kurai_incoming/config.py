"""The plugin's environment: Kurai's .env, prefix KURAI_ (the paths as the web and worker containers see them)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class IncomingConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KURAI_", env_file=".env", extra="ignore")

    # the library Navidrome reads (mounted into Kurai's containers by docker-compose.incoming.yml)
    music_dir: str = "/data/music"


@lru_cache
def get_config() -> IncomingConfig:
    return IncomingConfig()
