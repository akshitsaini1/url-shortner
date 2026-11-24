import pytest

from workflow.provider import LiveProvider


def test_live_provider_reads_dotenv_and_honors_shell_override(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKFLOW_API_BASE", raising=False)
    monkeypatch.delenv("WORKFLOW_API_KEY", raising=False)
    monkeypatch.setenv("WORKFLOW_MODEL", "shell-model")
    env = tmp_path / ".env"
    env.write_text(
        '# Project settings\nWORKFLOW_MODEL=file-model\n'
        'WORKFLOW_API_BASE=https://example.test/v1\nWORKFLOW_API_KEY="test=key#value"\n'
    )

    provider = LiveProvider(env)

    assert provider.model == "shell-model"
    assert provider.endpoint == "https://example.test/v1"
    assert provider.key == "test=key#value"


def test_live_provider_reports_missing_key(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFLOW_MODEL", "test-model")
    monkeypatch.delenv("WORKFLOW_API_KEY", raising=False)
    with pytest.raises(ValueError, match="Set WORKFLOW_API_KEY in .env"):
        LiveProvider(tmp_path / ".env")
