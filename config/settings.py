import os
import sys
from pathlib import Path
from typing import List, Optional
import yaml
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


class SearchConfig(BaseModel):
    role: str
    locations: List[str]
    site_names: List[str]
    hours_old: int = 72
    results_wanted: int = 20
    remote_only: bool = False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    gemini_api_key: str = Field(..., validation_alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", validation_alias="GEMINI_MODEL")
    llm_max_retries: int = Field(default=2, validation_alias="LLM_MAX_RETRIES")
    database_url: str = Field("sqlite:///./jobapplypro.db", validation_alias="DATABASE_URL")
    pinecone_api_key: Optional[str] = Field(None, validation_alias="PINECONE_API_KEY")

    # Scoring (Phase 4) — final score is 1–10; jobs at/above threshold go to tailoring.
    score_threshold: float = Field(default=6.0, validation_alias="SCORE_THRESHOLD")
    score_semantic_weight: float = Field(default=0.4, validation_alias="SCORE_SEMANTIC_WEIGHT")
    score_llm_weight: float = Field(default=0.6, validation_alias="SCORE_LLM_WEIGHT")

    profile_schema_path: Path = CONFIG_DIR / "profile.schema.json"
    profile_output_path: Path = CONFIG_DIR / "profile.json"
    profile_embedding_cache_path: Path = CONFIG_DIR / ".profile_embedding_cache.json"
    tailored_output_dir: Path = PROJECT_ROOT / "output" / "tailored"
    cover_letter_output_dir: Path = PROJECT_ROOT / "output" / "cover_letters"
    screenshot_output_dir: Path = PROJECT_ROOT / "output" / "screenshots"
    ats_config_path: Path = CONFIG_DIR / "ats.yaml"

    # Phase 7 — browser / form fill
    browser_headless: bool = Field(default=True, validation_alias="BROWSER_HEADLESS")
    # Tier 2 defaults to headed (CAPTCHA / bot walls are far worse headless).
    # Set TIER2_HEADLESS=true only when you intentionally want headless agents.
    tier2_headless: bool = Field(default=False, validation_alias="TIER2_HEADLESS")
    browser_user_data_dir: Path | None = Field(
        default=None, validation_alias="BROWSER_USER_DATA_DIR"
    )
    # Isolated Chrome profile for browser-use. Sharing BROWSER_USER_DATA_DIR with
    # Playwright leaves SingletonLock held and Tier 2 dies on BrowserStartEvent (30s).
    tier2_user_data_dir: Path | None = Field(
        default=PROJECT_ROOT / "output" / "browser_tier2_profile",
        validation_alias="TIER2_USER_DATA_DIR",
    )
    browser_state_dir: Path = Field(
        default=PROJECT_ROOT / "output" / "browser_state",
        validation_alias="BROWSER_STATE_DIR",
    )
    # When headed + LinkedIn auth wall: seconds to wait for you to sign in before giving up.
    browser_login_wait_seconds: int = Field(
        default=180, validation_alias="BROWSER_LOGIN_WAIT_SECONDS"
    )
    # Optional job-board passwords — used only when the persistent profile is logged out.
    linkedin_email: str = Field(default="", validation_alias="LINKEDIN_EMAIL")
    linkedin_password: str = Field(
        default="", validation_alias="LINKEDIN_PASSWORD", repr=False
    )
    naukri_email: str = Field(default="", validation_alias="NAUKRI_EMAIL")
    naukri_password: str = Field(
        default="", validation_alias="NAUKRI_PASSWORD", repr=False
    )
    indeed_email: str = Field(default="", validation_alias="INDEED_EMAIL")
    indeed_password: str = Field(
        default="", validation_alias="INDEED_PASSWORD", repr=False
    )
    # e.g. "chrome" to use installed Google Chrome (Workday often rejects bundled Chromium).
    browser_channel: str | None = Field(default=None, validation_alias="BROWSER_CHANNEL")
    form_min_confidence: float = Field(
        default=0.55, validation_alias="FORM_MIN_CONFIDENCE"
    )

    # When true, the scheduled pipeline fills forms and clicks Submit.
    # CAPTCHA, OTP, payment, and ID checks still stop as manual_review.
    # Set AUTO_SUBMIT=false to go back to dry-run (no Submit click).
    auto_submit: bool = Field(default=True, validation_alias="AUTO_SUBMIT")

    # Phase 7 Tier 2 — browser-use agent (costs LLM tokens per attempt)
    tier2_enabled: bool = Field(default=True, validation_alias="TIER2_ENABLED")
    tier2_max_jobs_per_run: int = Field(
        default=15, validation_alias="TIER2_MAX_JOBS_PER_RUN"
    )
    tier2_max_agent_steps: int = Field(
        default=50, validation_alias="TIER2_MAX_AGENT_STEPS"
    )
    # Model for browser-use ChatGoogle; defaults to the same Gemini model as LLMClient.
    tier2_llm_model: str = Field(
        default="", validation_alias="TIER2_LLM_MODEL"
    )

    # Phase 11 — proxy rotation for JobSpy (optional)
    proxy_urls: str = Field(default="", validation_alias="PROXY_URLS")
    proxy_file: str = Field(default="", validation_alias="PROXY_FILE")

    # Phase 11 — alerts on newly scored high-fit jobs (optional)
    notify_slack_webhook_url: str = Field(
        default="", validation_alias="NOTIFY_SLACK_WEBHOOK_URL"
    )
    notify_email_to: str = Field(default="", validation_alias="NOTIFY_EMAIL_TO")
    notify_email_from: str = Field(default="", validation_alias="NOTIFY_EMAIL_FROM")
    notify_smtp_host: str = Field(default="", validation_alias="NOTIFY_SMTP_HOST")
    notify_smtp_port: int = Field(default=587, validation_alias="NOTIFY_SMTP_PORT")
    notify_smtp_user: str = Field(default="", validation_alias="NOTIFY_SMTP_USER")
    notify_smtp_password: str = Field(default="", validation_alias="NOTIFY_SMTP_PASSWORD")

    # Phase 11 — local dashboard
    dashboard_host: str = Field(default="127.0.0.1", validation_alias="DASHBOARD_HOST")
    dashboard_port: int = Field(default=8765, validation_alias="DASHBOARD_PORT")

    searches: List[SearchConfig] = []


def load_settings(
    env_file_path: Optional[str] = None,
    yaml_file_path: Optional[str] = None
) -> Settings:
    """
    Loads configuration settings. First loads environment variables from the
    environment or .env file, then loads and parses the searches.yaml file.
    """
    project_dir = str(PROJECT_ROOT)

    if env_file_path is None:
        env_file_path = os.path.join(project_dir, ".env")

    if yaml_file_path is None:
        yaml_file_path = os.path.join(project_dir, "config", "searches.yaml")

    try:
        # Load environment variables
        settings = Settings(_env_file=env_file_path)
    except ValidationError as e:
        print(f"\n[Configuration Error] Failed to load environment variables.", file=sys.stderr)
        print("Please ensure your `.env` file exists and has all required variables.", file=sys.stderr)
        print(f"Details:\n{e}\n", file=sys.stderr)
        raise SystemExit(1)

    # Load search configs from yaml
    if not os.path.exists(yaml_file_path):
        print(f"\n[Configuration Error] Search configuration file not found at {yaml_file_path}", file=sys.stderr)
        raise SystemExit(1)

    try:
        with open(yaml_file_path, "r") as f:
            yaml_data = yaml.safe_load(f)

        if yaml_data and "searches" in yaml_data:
            settings.searches = [SearchConfig(**s) for s in yaml_data["searches"]]
        else:
            settings.searches = []
    except (yaml.YAMLError, ValidationError) as e:
        print(f"\n[Configuration Error] Failed to parse search config YAML at {yaml_file_path}", file=sys.stderr)
        print(f"Details:\n{e}\n", file=sys.stderr)
        raise SystemExit(1)

    return settings


# Global settings instance loaded on demand or module import
# To prevent failure during initialization tasks (like writing empty config or CLI helper commands),
# we can call load_settings() lazily.
_settings: Optional[Settings] = None

def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings
