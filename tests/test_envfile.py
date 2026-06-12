"""Tests for the stdlib .env loader used by the helper scripts."""

import os

import envfile


def test_loads_values_and_handles_quotes_comments_and_precedence(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "DIALPAD_API_TOKEN=abc123\n"
        "export PUBLIC_WEBHOOK_URL=https://h/dialpad/webhook\n"
        'IT_TARGETS="department:1,callcenter:2,callcenter:3"\n'
        "ATTACH_VOICEMAIL_AUDIO=true # inline comment stripped\n"
        "ALREADY_SET=from_file\n"
    )
    # A real env var must win over the .env file.
    monkeypatch.setenv("ALREADY_SET", "from_shell")
    for k in ("DIALPAD_API_TOKEN", "PUBLIC_WEBHOOK_URL", "IT_TARGETS",
              "ATTACH_VOICEMAIL_AUDIO"):
        monkeypatch.delenv(k, raising=False)

    envfile.load(str(env))

    assert os.environ["DIALPAD_API_TOKEN"] == "abc123"
    assert os.environ["PUBLIC_WEBHOOK_URL"] == "https://h/dialpad/webhook"
    # commas inside quotes preserved, quotes stripped
    assert os.environ["IT_TARGETS"] == "department:1,callcenter:2,callcenter:3"
    assert os.environ["ATTACH_VOICEMAIL_AUDIO"] == "true"
    assert os.environ["ALREADY_SET"] == "from_shell"


def test_missing_file_is_noop():
    envfile.load("/nonexistent/.env")  # should not raise
