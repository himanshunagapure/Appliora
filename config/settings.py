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
        extra="ignore"
    )

    gemini_api_key: str = Field(..., validation_alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", validation_alias="GEMINI_MODEL")
    llm_max_retries: int = Field(default=2, validation_alias="LLM_MAX_RETRIES")
    database_url: str = Field("sqlite:///./jobapplypro.db", validation_alias="DATABASE_URL")
    pinecone_api_key: Optional[str] = Field(None, validation_alias="PINECONE_API_KEY")

    profile_schema_path: Path = CONFIG_DIR / "profile.schema.json"
    profile_output_path: Path = CONFIG_DIR / "profile.json"

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
