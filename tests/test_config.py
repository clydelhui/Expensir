import pytest
from pydantic import ValidationError

from expensir.config import Settings


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Config tests must not read the developer's real .env or exported vars."""
    monkeypatch.chdir(tmp_path)
    for var in (
        "MODE",
        "BOT_TOKEN",
        "TELEGRAM_WEBHOOK_SECRET",
        "PUBLIC_URL",
        "DATABASE_URL",
        "OPERATOR_USER_ID",
        "TELEGRAM_API_BASE",
    ):
        monkeypatch.delenv(var, raising=False)


def test_settings_read_the_env_contract(monkeypatch):
    monkeypatch.setenv("MODE", "poll")
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./expensir.db")
    monkeypatch.setenv("OPERATOR_USER_ID", "42")

    settings = Settings()

    assert settings.mode == "poll"
    assert settings.bot_token == "123:abc"
    assert settings.telegram_webhook_secret == "s3cret"
    assert settings.database_url == "sqlite+aiosqlite:///./expensir.db"
    assert settings.operator_user_id == 42


def test_mode_defaults_to_webhook(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./expensir.db")

    assert Settings().mode == "webhook"


def test_webhook_mode_requires_a_non_empty_secret(monkeypatch):
    monkeypatch.setenv("MODE", "webhook")
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./expensir.db")

    with pytest.raises(ValidationError, match="TELEGRAM_WEBHOOK_SECRET"):
        Settings()


def test_poll_mode_tolerates_an_empty_secret(monkeypatch):
    monkeypatch.setenv("MODE", "poll")
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./expensir.db")

    assert Settings().telegram_webhook_secret == ""


def test_example_env_file_parses_cleanly_when_filled_minimally(tmp_path, monkeypatch):
    """Copying .env.example to .env and filling only the blanks must not crash startup."""
    import shutil
    from pathlib import Path

    example = Path(__file__).parent.parent / ".env.example"
    env_file = tmp_path / ".env"
    shutil.copy(example, env_file)
    text = env_file.read_text()
    text = text.replace("BOT_TOKEN=", "BOT_TOKEN=123:abc", 1)
    text = text.replace("TELEGRAM_WEBHOOK_SECRET=", "TELEGRAM_WEBHOOK_SECRET=s3cret", 1)
    env_file.write_text(text)

    settings = Settings(_env_file=env_file)

    assert settings.telegram_webhook_secret == "s3cret"
    assert settings.public_url is None
    assert settings.operator_user_id is None
