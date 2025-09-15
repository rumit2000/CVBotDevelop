# config.py
import os
from dataclasses import dataclass

@dataclass
class Settings:
    # Telegram
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    owner_id: int = int(os.getenv("OWNER_ID", "0") or 0)

    # OpenAI
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "").strip()
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small").strip()

    # Assistants API (необязательно)
    assistant_id: str = os.getenv("ASSISTANT_ID", "").strip()

    # Резюме
    resume_path: str = os.getenv("RESUME_PATH", "data/CVTimurAsyaev.pdf").strip()
    # NEW: one-pager путь (по умолчанию data/CVTimurAsyaevOnePage.pdf)
    resume_onepage_path: str = os.getenv("RESUME_ONEPAGE_PATH", "data/CVTimurAsyaevOnePage.pdf").strip()

    # Ссылки/контакты
    linkedin_url: str = os.getenv("LINKEDIN_URL", "").strip()
    contact_info: str = os.getenv(
        "CONTACT_INFO",
        "Связаться: @TimurAsyaev • rumit2000@gmail.com"
    ).strip()

def build_settings() -> Settings:
    return Settings()

settings = build_settings()

