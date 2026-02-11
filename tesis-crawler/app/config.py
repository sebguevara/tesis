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
    WIDGET_DEV_API_KEY: str = os.getenv("WIDGET_DEV_API_KEY", "")
    CLERK_WEBHOOK_SECRET: str = os.getenv("CLERK_WEBHOOK_SECRET", "")
    RAG_ENABLE_LIVE_FETCH: bool = (
        str(os.getenv("RAG_ENABLE_LIVE_FETCH", "false")).strip().lower()
        in {"1", "true", "yes", "on"}
    )

    # PDF processing
    PDF_STORAGE_DIR: str = os.getenv("PDF_STORAGE_DIR", "./data/pdf")
    MAX_PDF_SIZE_MB: int = int(os.getenv("MAX_PDF_SIZE_MB", "50"))
    PDF_CONCURRENCY: int = int(os.getenv("PDF_CONCURRENCY", "5"))

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()
