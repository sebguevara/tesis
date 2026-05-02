from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv
import os

load_dotenv()   


class Settings(BaseSettings):
    NODE_ENV: str = str(os.getenv("NODE_ENV", os.getenv("node_env", "production"))).strip().lower()
    DATABASE_URL: str = os.getenv("DATABASE_URL")
    DB_URL: str = os.getenv("DB_URL")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY")
    OPENAI_CHAT_MODEL: str = os.getenv("OPENAI_CHAT_MODEL")
    SITE_MD_DIR: str = os.getenv("SITE_MD_DIR")
    WIDGET_TEST_ORIGIN: str = os.getenv("WIDGET_TEST_ORIGIN", "")
    WIDGET_DEV_API_KEY: str = os.getenv("WIDGET_DEV_API_KEY", "")
    CLERK_WEBHOOK_SECRET: str = os.getenv("CLERK_WEBHOOK_SECRET", "")
    RAG_ENABLE_LIVE_FETCH: bool = (
        str(os.getenv("RAG_ENABLE_LIVE_FETCH", "false")).strip().lower()
        in {"1", "true", "yes", "on"}
    )
    RAG_SIMPLE_MODE: bool = (
        str(os.getenv("RAG_SIMPLE_MODE", "false")).strip().lower()
        in {"1", "true", "yes", "on"}
    )
    RAG_LLM_TIMEOUT_SECONDS: float = float(os.getenv("RAG_LLM_TIMEOUT_SECONDS", "18"))
    RAG_LLM_MAX_RETRIES: int = int(os.getenv("RAG_LLM_MAX_RETRIES", "1"))
    RAG_GRAPH_TIMEOUT_SECONDS: float = float(os.getenv("RAG_GRAPH_TIMEOUT_SECONDS", "25"))
    RAG_GRAPH_COMPACT_TIMEOUT_SECONDS: float = float(os.getenv("RAG_GRAPH_COMPACT_TIMEOUT_SECONDS", "14"))

    # PDF processing
    PDF_STORAGE_DIR: str = os.getenv("PDF_STORAGE_DIR", "./data/pdf")
    MAX_PDF_SIZE_MB: int = int(os.getenv("MAX_PDF_SIZE_MB", "50"))
    PDF_CONCURRENCY: int = int(os.getenv("PDF_CONCURRENCY", "5"))
    PDF_LOOKBACK_YEARS: int = int(os.getenv("PDF_LOOKBACK_YEARS", "5"))
    PDF_DOC_MAX_SIZE_MB: int = int(os.getenv("PDF_DOC_MAX_SIZE_MB", "15"))
    PDF_DOC_MAX_PAGES: int = int(os.getenv("PDF_DOC_MAX_PAGES", "120"))

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()
