from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv
import os

load_dotenv()   


class Settings(BaseSettings):
    DATABASE_URL: str = os.getenv("DATABASE_URL")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY")
    OPENAI_CHAT_MODEL: str = os.getenv("OPENAI_CHAT_MODEL")
    OPENAI_EMBEDDING_MODEL: str = os.getenv("OPENAI_EMBEDDING_MODEL")
    EMBEDDING_DIM: int = os.getenv("EMBEDDING_DIM")
    SITE_MD_DIR: str = os.getenv("SITE_MD_DIR")
    WIDGET_TEST_ORIGIN: str = os.getenv("WIDGET_TEST_ORIGIN", "")
    WIDGET_ADMIN_TOKEN: str = os.getenv("WIDGET_ADMIN_TOKEN", "")

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()
