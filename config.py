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
    openai_model: str = (os.getenv("OPENAI_MODEL", "gpt-4o-mini") or "gpt-4o-mini").strip()
    assistant_id: str = os.getenv("OPENAI_ASSISTANT_ID", "").strip()

    # Resume / Links
    resume_path: str = os.getenv("RESUME_PATH", "data/CVTimurAsyaev.pdf").strip()
    linkedin_url: str = os.getenv("LINKEDIN_URL", "").strip()
    contact_info: str = os.getenv("CONTACT_INFO", "@your_tg • email@example.com").strip()

    # (для совместимости — не используются в long-polling)
    base_webhook_url: str = os.getenv("BASE_WEBHOOK_URL", "").strip()
    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "").strip()

settings = Settings()
