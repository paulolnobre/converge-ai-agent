"""Configuration that would be wrong but silent is refused at startup."""
import base64

import pytest

from core.config import (
    MEDIA_EVENT_DAYS,
    Settings,
    missing_provider_credentials,
    provider_model_problems,
    validate_startup_config,
)


def test_a_gemini_model_sent_to_openai_is_a_problem():
    config = Settings(llm_complex_provider="openai", llm_complex_model="gemini-2.0-flash")
    assert any("LLM_COMPLEX_MODEL" in problem for problem in provider_model_problems(config))


def test_a_matching_provider_and_model_is_fine():
    config = Settings(llm_complex_provider="openai", llm_complex_model="gpt-4o")
    assert provider_model_problems(config) == []


def test_choosing_gemini_without_a_key_is_reported():
    """Otherwise the gateway serves OpenAI and nobody is told."""
    assert missing_provider_credentials(Settings(llm_simple_provider="gemini", gemini_api_key=None))


def test_startup_refuses_an_incompatible_configuration():
    config = Settings(llm_complex_provider="openai", llm_complex_model="gemini-2.0-flash")
    with pytest.raises(RuntimeError, match="Incompatible provider/model"):
        validate_startup_config(config)


def test_production_requires_a_webhook_secret():
    with pytest.raises(ValueError, match="TELEGRAM_WEBHOOK_SECRET"):
        Settings(
            app_env="production",
            db_password="a-strong-one",
            telegram_bot_token="123:abc",
            telegram_webhook_secret=None,
        )


def test_office_hours_must_be_a_real_window():
    with pytest.raises(ValueError, match="start before they end"):
        Settings(office_start_hour=18, office_end_hour=9)


def test_the_desk_must_be_open_on_some_day():
    with pytest.raises(ValueError, match="OFFICE_WORKDAYS"):
        Settings(office_workdays="")


def test_workdays_are_parsed_and_bounded():
    assert Settings(office_workdays="1,2,3,4,5").workdays() == {1, 2, 3, 4, 5}
    # 8 is not a weekday; it is dropped rather than shifting everything else.
    assert Settings(office_workdays="6,7,8").workdays() == {6, 7}


def test_negative_notice_is_refused():
    with pytest.raises(ValueError, match="MINIMUM_NOTICE_MINUTES"):
        Settings(minimum_notice_minutes=-30)


def test_an_invalid_time_zone_is_refused():
    with pytest.raises(ValueError, match="Invalid time zone"):
        Settings(office_timezone="Mars/Olympus")


def test_trace_content_stays_hidden_unless_deliberately_exposed():
    with pytest.raises(ValueError, match="LANGSMITH_CAPTURE_CONTENT"):
        Settings(langsmith_hide_inputs=False)


def test_unset_role_models_inherit_the_defaults():
    config = Settings(llm_complex_model="gpt-4o", llm_simple_model="gpt-4o-mini")
    assert config.reception_model == "gpt-4o"
    assert config.router_model == "gpt-4o-mini"


_KEY = base64.b64encode(bytes(range(32))).decode()
_SHORT = base64.b64encode(bytes(16)).decode()  # 16 bytes, not 32


@pytest.mark.parametrize("missing", ["data_keys", "data_index_key"])
def test_production_requires_both_data_keys(missing):
    keys = {"data_keys": f"1:{_KEY}", "data_index_key": _KEY, missing: ""}
    with pytest.raises(ValueError, match=missing.upper()):
        Settings(
            app_env="production", db_password="a-strong-one",
            telegram_bot_token="123:abc", telegram_webhook_secret="s",
            data_keys=keys["data_keys"], data_index_key=keys["data_index_key"],
        )


@pytest.mark.parametrize("data_keys", [
    f"1:{_KEY},1:{_KEY}",            # repeated version
    f"0:{_KEY}", f"256:{_KEY}",      # out of range
    "1:not-base64!!",                # not base64
    f"1:{_SHORT}",                   # 16 bytes, not 32
    _KEY,                            # no version
])
def test_malformed_data_keys_are_refused_without_the_value(data_keys):
    with pytest.raises(ValueError, match="DATA_KEYS") as caught:
        Settings(data_keys=data_keys, data_index_key=_KEY)
    assert _KEY not in str(caught.value) and "not-base64" not in str(caught.value)


def test_a_malformed_index_key_is_refused_without_the_value():
    with pytest.raises(ValueError, match="DATA_INDEX_KEY") as caught:
        Settings(data_keys=f"1:{_KEY}", data_index_key=_SHORT)
    assert _SHORT not in str(caught.value)


def test_development_runs_without_data_keys():
    Settings(app_env="development", data_keys="", data_index_key="")


def test_a_startup_error_carries_no_fragment_of_any_key():
    """pydantic adds input_value= (the start and end of the input) to its
    error: a fragment of a key, or of the next secret, in docker logs."""
    index = base64.b64encode(bytes(range(200, 232))).decode()
    data = base64.b64encode(bytes(range(150, 182))).decode()
    with pytest.raises(ValueError) as caught:
        Settings(
            app_env="production", db_password="a-strong-one",
            telegram_bot_token="123:abc", telegram_webhook_secret="s",
            data_keys=f"1:{data},1:{data}", data_index_key=index,
        )
    text = str(caught.value)
    for secret in (index, data, "a-strong-one", "123:abc"):
        assert secret[-12:] not in text and secret[:12] not in text, secret


def test_booking_mode_and_limit_have_safe_defaults():
    config = Settings()
    assert config.booking_mode == "suggest" and config.max_upcoming_appointments == 0


@pytest.mark.parametrize("kwargs", [{"booking_mode": "x"}, {"max_upcoming_appointments": 6},
                                    {"max_upcoming_appointments": -1}])
def test_booking_mode_and_limit_are_validated(kwargs):
    with pytest.raises(ValueError):
        Settings(**kwargs)


@pytest.mark.parametrize("field, bad", [
    ("max_media_bytes", 20971521), ("max_media_bytes", 1048575),
    ("media_download_seconds", 4), ("media_download_seconds", 121),
    ("media_audio_hours", 49), ("media_audio_hours", 0),
    ("media_retention_days", 91), ("media_retention_days", 0),
    ("media_cleanup_minutes", 16), ("media_cleanup_minutes", 0),
    ("media_serve_concurrency", 9), ("media_serve_concurrency", 0),
    ("media_audio_max_hours", 169),
    ("calendar_sync_minutes", 0), ("calendar_sync_minutes", 16),
])
def test_media_settings_out_of_range_refuse_to_start(field, bad):
    with pytest.raises(ValueError):
        Settings(**{field: bad})


def test_audio_ceiling_below_the_basic_hold_refuses_to_start():
    with pytest.raises(ValueError, match="MEDIA_AUDIO_MAX_HOURS"):
        Settings(media_audio_hours=48, media_audio_max_hours=47)


def test_media_capture_without_a_team_group_refuses_to_start():
    with pytest.raises(ValueError, match="TEAM_CHAT_ID"):
        Settings(media_enabled=True, team_chat_id="")


def test_media_defaults():
    config = Settings()
    assert config.media_enabled is False and config.media_dir == "/data/media"
    assert (config.max_media_bytes, config.media_max_total_mb, config.media_min_free_mb) == (
        20971520, 3072, 2048)
    assert (config.media_download_seconds, config.media_audio_hours,
            config.media_audio_max_hours, config.media_retention_days) == (60, 48, 168, 90)
    assert (config.media_cleanup_minutes, config.media_serve_concurrency) == (15, 4)
    assert MEDIA_EVENT_DAYS == 90


def test_payments_off_without_key():
    assert Settings(_env_file=None, asaas_api_key="").payments_configured() is False  # type: ignore[call-arg]


def test_payments_on_with_key():
    assert Settings(_env_file=None, asaas_api_key="$aact_x").payments_configured() is True  # type: ignore[call-arg]


def test_asaas_base_url_per_env():
    assert Settings(_env_file=None).asaas_base_url == "https://api-sandbox.asaas.com/v3"  # type: ignore[call-arg]
    production = Settings(_env_file=None, asaas_env="production")  # type: ignore[call-arg]
    assert production.asaas_base_url == "https://api.asaas.com/v3"


def test_asaas_env_rejects_other_values():
    with pytest.raises(ValueError):
        Settings(_env_file=None, asaas_env="prod")  # type: ignore[call-arg]


def test_single_quoted_asaas_key_keeps_the_dollar(tmp_path):
    """Compose interpolates `$` in env_file; the documented form is single
    quotes, and the app must read that form back unchanged."""
    env = tmp_path / ".env"
    env.write_text("ASAAS_API_KEY='$aact_hmlg_000abc'\n")
    assert Settings(_env_file=env).asaas_api_key == "$aact_hmlg_000abc"  # type: ignore[call-arg]
