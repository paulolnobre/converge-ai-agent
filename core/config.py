"""Settings for the reception and secretarial assistant.

Everything comes from the environment or the project-root `.env`. Two rules
hold throughout: nothing that could hold a secret has a usable default, and
anything that would be wrong-but-silent in production is refused at startup
rather than discovered from a bad daily report three weeks later.
"""
import base64
import binascii
import os
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).parent.parent / ".env"

# How long a media update's mark (media_events) is kept, and the age past which
# such an update is no longer processed: the queue has no age limit of its own.
MEDIA_EVENT_DAYS = 90
_MIB = 1024 * 1024

WorkspaceProvider = Literal["google", "microsoft"]
# An empty string means "inherit WORKSPACE_PROVIDER" — a split tenant only has
# to name the capabilities that differ.
ProviderOverride = Literal["", "google", "microsoft"]


def parse_data_keys(value: str) -> dict[int, bytes]:
    """DATA_KEYS as {version: key}, in the order given (the first encrypts).

    The messages name the setting and the problem, never the value: a startup
    error is logged, and a key in a log is a key on disk.
    """
    keys: dict[int, bytes] = {}
    for position, entry in enumerate(value.split(","), start=1):
        version_text, sep, encoded = entry.strip().partition(":")
        if not sep or not version_text.isdigit():
            raise ValueError(f"DATA_KEYS entry {position} must be <version>:<base64 key>.")
        version = int(version_text)
        if not 1 <= version <= 255:
            raise ValueError(f"DATA_KEYS entry {position}: the version must be 1 to 255.")
        if version in keys:
            raise ValueError(f"DATA_KEYS entry {position}: version {version} is repeated.")
        keys[version] = _decode_key(encoded, f"DATA_KEYS entry {position}")
    return keys


def _decode_key(encoded: str, name: str) -> bytes:
    try:
        key = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError(f"{name} is not valid base64.") from None
    if len(key) != 32:
        raise ValueError(f"{name} must be 32 bytes (base64 of 32 random bytes).")
    return key


def parse_index_key(value: str) -> bytes:
    return _decode_key(value, "DATA_INDEX_KEY")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # A startup error is logged; pydantic would add the start and end of
        # every setting (keys, tokens, passwords) as input_value.
        hide_input_in_errors=True,
    )

    app_env: Literal["development", "production"] = "development"
    app_version: str = "0.1.0"

    # ── Channel ──────────────────────────────────────────────────────────────
    telegram_bot_token: str | None = None
    telegram_webhook_secret: str | None = None
    public_base_url: str | None = None
    # WhatsApp Cloud API, straight with Meta. On only with the four that
    # whatsapp_configured() names; the rest are informative or optional.
    whatsapp_app_id: str | None = None
    whatsapp_app_secret: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_business_account_id: str | None = None
    whatsapp_access_token: str | None = None
    whatsapp_webhook_verify_token: str | None = None
    whatsapp_reminder_template: str | None = None
    # The approved template that resumes a conversation that went quiet (name
    # as in WhatsApp Manager). Without it, automations skip WhatsApp contacts.
    whatsapp_resume_template: str | None = None
    whatsapp_resume_language: str = "pt_BR"
    # Approved templates for what the company starts after the 24h window: a
    # charge, its thanks, and the team asking to reopen the conversation. Each
    # one empty keeps today's behaviour (refused, skipped, refused).
    whatsapp_charge_template: str | None = None
    whatsapp_paid_template: str | None = None
    whatsapp_team_template: str | None = None
    # The language each was approved in, as WhatsApp Manager shows it ("en" for
    # English): Meta refuses a template sent in another one.
    whatsapp_paid_language: str = "pt_BR"
    whatsapp_team_language: str = "pt_BR"
    # The first message to a lead the team added in the panel (e.g. one that
    # came by e-mail). Empty: the lead is saved, nothing is sent.
    whatsapp_lead_template: str | None = None
    # The same first message for any other lead (indicação, Instagram, site...):
    # "você demonstrou interesse nas nossas soluções".
    whatsapp_lead_general_template: str | None = None
    # The proposal PDF past the 24h window: a template with a document header,
    # body {{1}} name and {{2}} company, and the three decision buttons.
    whatsapp_proposal_template: str | None = None
    # Asking for more information to build the proposal, past the 24h window:
    # body {{1}} first name, and two buttons (book a talk, talk to the team).
    whatsapp_info_template: str | None = None
    whatsapp_graph_version: str = "v23.0"

    # ── LLM providers ────────────────────────────────────────────────────────
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    llm_simple_provider: Literal["gemini", "openai"] = "openai"
    llm_complex_provider: Literal["gemini", "openai"] = "openai"
    llm_simple_model: str = "gpt-4o-mini"
    llm_complex_model: str = "gpt-4o"
    llm_fallback_model: str = "gpt-4o-mini"
    llm_temperature_simple: float = 0.2
    llm_temperature_complex: float = 0.2
    router_model: str | None = None
    reception_model: str | None = None
    drafting_model: str | None = None

    embed_model: str = "text-embedding-3-small"
    embed_dim: int = 1536

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str | None = None
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "reception_agent"
    db_user: str = "reception_agent"
    db_password: str = ""
    db_pool_min: int = 1
    # Room for the workers and the panel at once: a reply from the Inbox holds
    # its connection (and the conversation's lock) while Telegram answers.
    db_pool_max: int = 10
    db_sslmode: Literal[
        "disable", "allow", "prefer", "require", "verify-ca", "verify-full"
    ] = "prefer"

    # ── Cache and durable queue (Redis) ──────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    cache_enabled: bool = True
    cache_prefix: str = "reception"
    cache_answer_ttl: int = 3_600
    cache_embed_ttl: int = 604_800
    queue_active_name: str = "reception:queue:active"
    queue_deferred_name: str = "reception:queue:deferred"
    queue_processing_name: str = "reception:queue:processing"
    queue_dead_letter_name: str = "reception:queue:dead-letter"
    # Items that could not be opened (tampered, missing key, broken JSON), kept
    # sealed for a person to look at: python -m cli.queue_quarantine.
    queue_quarantine_name: str = "reception:queue:quarantine"
    queue_dedupe_ttl: int = 604_800
    queue_block_seconds: int = 5
    queue_max_attempts: int = 3

    # ── Workspace suites ─────────────────────────────────────────────────────
    # One default, with a per-capability override for the split tenant: mail on
    # Microsoft 365, cost spreadsheets on Google Sheets is a real combination.
    workspace_provider: WorkspaceProvider = "google"
    calendar_provider: ProviderOverride = ""
    mail_provider: ProviderOverride = ""
    files_provider: ProviderOverride = ""
    sheets_provider: ProviderOverride = ""

    # Google. `service_account` needs domain-wide delegation set up by a
    # Workspace admin; `refresh_token` is the small-office path, where one
    # shared account is authorized by hand with cli/authorize_google.py.
    google_auth_mode: Literal["service_account", "refresh_token"] = "service_account"
    google_credentials_path: str = "google_credentials.json"
    google_delegated_subject: str | None = None
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_refresh_token: str | None = None
    google_scopes: str = (
        "https://www.googleapis.com/auth/calendar,"
        "https://www.googleapis.com/auth/drive,"
        "https://www.googleapis.com/auth/spreadsheets,"
        "https://www.googleapis.com/auth/gmail.modify"
    )

    # Microsoft 365 (Entra ID app registration, application permissions).
    ms_tenant_id: str | None = None
    ms_client_id: str | None = None
    ms_client_secret: str | None = None
    ms_scope: str = "https://graph.microsoft.com/.default"
    ms_default_upn: str | None = None
    ms_drive_id: str | None = None  # a SharePoint library, instead of a OneDrive

    # ── Front desk ───────────────────────────────────────────────────────────
    office_timezone: str = "America/Sao_Paulo"
    office_start_hour: int = 9
    office_start_minute: int = 0
    office_end_hour: int = 18
    office_end_minute: int = 0
    office_workdays: str = "1,2,3,4,5"  # ISO weekdays the desk is open
    # The bot's own hours, separate from the office's. Off (the safe default):
    # is_open() follows OFFICE_START_HOUR/END_HOUR/WORKDAYS like everything
    # else. On: the bot answers every message immediately, any hour, any day —
    # but a booking still only offers real office hours, since offer_times()
    # never reads this flag.
    desk_always_open: bool = False
    organization_name: str = ""
    # Where the organization's own words live: the FAQ, the messages the desk
    # says, the inbox categories, the team. Relative paths resolve from the
    # repository root.
    content_dir: str = "content"
    # Empty means "use content/messages.toml", which is where the organization's
    # own wording lives. Set this only to override the content file.
    out_of_hours_reply: str = ""
    # Telegram chat ids that never see the desk as closed — for staff testing
    # the bot outside real hours. Does not touch is_open() or the times a
    # booking offers: only whether *this* chat's own messages get deferred.
    hours_bypass_chat_ids: str = ""
    # The /painel dashboard: one account per person, password + authenticator
    # code (cli/panel_users.py invites them). This key encrypts the TOTP secrets
    # at rest; empty keeps the panel closed, and losing it means everyone
    # enrolls again. Generate: python -c "from cryptography.fernet import
    # Fernet; print(Fernet.generate_key().decode())"
    panel_secret_key: str = ""
    # The customer fields and the queue are encrypted by the app with these
    # (core/crypto.py). DATA_KEYS is "1:<base64 of 32 bytes>", newest first on a
    # rotation ("2:...,1:..."); DATA_INDEX_KEY keys the phone/email lookups.
    # Required in production, never in a log; losing them loses the data, so a
    # copy lives in the owner's password manager (docs/CRIPTOGRAFIA.md).
    # Generate on the server: deploy/data-keys.sh.
    data_keys: str = ""
    data_index_key: str = ""
    panel_session_hours: int = 12
    panel_idle_minutes: int = 120
    panel_device_idle_days: int = Field(default=90, ge=1, le=365)
    # The team's Telegram group: handoff cards land there, and a reply to a card
    # is relayed to the person through the bot. Empty means no one can be told,
    # and the desk says so instead of promising a person who will never come.
    team_chat_id: str = ""
    # Where the monitor sends system alerts on Telegram: a chat with the admins
    # only. Empty: error alerts go by push and the admin room only, and "bot
    # down" to the team group (TEAM_CHAT_ID).
    admin_alert_chat_id: str = ""
    # Errors of one kind become at most one alert per this many minutes, with
    # how many times they happened.
    error_alert_minutes: int = Field(default=15, ge=1, le=1440)
    # A session with no message on either side for this long closes itself and
    # hands the customer back to the bot.
    handoff_timeout_minutes: int = 60
    # A /teste with no message from the person for this long is ended and
    # wiped, its calendar events cancelled.
    test_idle_minutes: int = 120

    # ── Knowledge (phase 1.1) ────────────────────────────────────────────────
    # The FAQ the desk answers from. A path under the content directory for a
    # pilot; a file id in the client's own drive in production. The drive wins
    # when both are set, because that is the copy the organization edits.
    knowledge_document_path: str = "faq.md"
    knowledge_document_id: str | None = None
    # How many chunks go into the answering prompt. Recall matters more than
    # precision here: the model is told to refuse when the chunks do not contain
    # the answer, so a near-miss chunk costs tokens, while a missing one costs
    # the answer. A broad question about one product ("explain the legal
    # assistant") needs several of its sections at once, hence six.
    knowledge_top_k: int = 6
    # A coarse floor, not the real filter. Needs calibrating against the
    # organization's own questions before a pilot — see docs/ROADMAP.md 1.1.
    knowledge_min_similarity: float = 0.25

    # ── Scheduling (phase 2) ─────────────────────────────────────────────────
    # Which calendars the agent may read and write. Comma-separated: a Google
    # calendar id, or a Microsoft 365 UPN.
    reception_calendar_id: str | None = None
    staff_calendar_ids: str = ""
    appointment_duration_minutes: int = 30
    slot_step_minutes: int = 30
    booking_horizon_days: int = 30
    minimum_notice_minutes: int = 120
    slots_offered: int = 3
    # How the desk offers times: "suggest" puts 3 spread-out times in front of
    # the person (the default); "choose_day" lists the days with free times and
    # then the times of the chosen day (docs: the escolher-dia spec).
    booking_mode: Literal["suggest", "choose_day"] = "suggest"
    # Upcoming confirmed appointments one contact may hold: 0 is no limit.
    max_upcoming_appointments: int = 0
    # The least time between two offered slots. Without it an empty diary
    # offers 09:00, 09:30 and 10:00 — three answers to one question.
    slot_min_gap_minutes: int = 120
    reminder_hours_before: int = 24
    # Create each booking as a video meeting (Google Meet on a Google
    # calendar); the link goes in the confirmation, the reminder and the invite.
    online_meetings: bool = False
    # For a meeting booked inside that window: remind this long before instead.
    late_reminder_minutes: int = 60
    # How long times stay pickable after the desk offers them. The slots are
    # not held during it — this is only how long "a terca" still means the
    # Tuesday that was offered, rather than a fresh request.
    offer_ttl_minutes: int = 60

    # ── Mailbox (phase 3) ────────────────────────────────────────────────────
    reception_mailbox: str | None = None
    inbox_poll_minutes: int = 10
    inbox_batch_size: int = 25
    # A drafted reply is held for a human until this is turned on deliberately.
    autosend_replies: bool = False
    escalation_mailbox: str | None = None

    # ── Spacemail: the mailbox the agent sends from (SMTP). Without a username
    # and a password, off, and the Google/Microsoft mailbox sends as before.
    spacemail_host: str = "mail.spacemail.com"
    spacemail_port: int = 465
    spacemail_username: str = ""
    spacemail_password: str = ""
    # Alert the team when a person's e-mail reaches that mailbox (IMAP, read
    # only). Off until turned on, and needs the login above.
    spacemail_alerts: bool = False
    spacemail_imap_host: str = "mail.spacemail.com"
    spacemail_imap_port: int = 993
    email_alert_minutes: int = 5
    # Charges and payments also by e-mail, to the contact's address on file,
    # from the sending mailbox. Off until turned on.
    billing_emails: bool = False

    # ── Documents (phase 3) ──────────────────────────────────────────────────
    documents_root_folder_id: str | None = None
    # Files the agent may attach without asking. Anything outside this folder
    # needs an explicit human confirmation before it leaves the organization.
    shareable_folder_id: str | None = None
    max_attachment_bytes: int = 15 * 1024 * 1024

    # ── Secretarial work (phase 4) ───────────────────────────────────────────
    minutes_folder_id: str | None = None
    daily_briefing_hour: int = 8
    followup_after_days: int = 3

    # ── Abuse limits ─────────────────────────────────────────────────────────
    rate_limit_max: int = 20
    rate_limit_window: int = 60
    max_message_chars: int = 4096
    max_voice_seconds: int = 300
    max_voice_bytes: int = 20 * 1024 * 1024

    # ── Photos, files and audio in the Inbox ────────────────────────────────
    # Capture only: showing and cleaning up what was kept always run, so that
    # turning capture off never leaves files behind.
    media_enabled: bool = False
    panel_uploads_enabled: bool = False
    media_dir: str = "/data/media"
    max_media_bytes: int = 20 * _MIB
    media_max_total_mb: int = 3072
    media_min_free_mb: int = 2048
    media_download_seconds: int = 60
    media_audio_hours: int = 48
    media_audio_max_hours: int = 168
    media_retention_days: int = 90
    media_cleanup_minutes: int = 15
    media_serve_concurrency: int = 4
    # Manual, local text extraction in the Inbox. A new name on purpose: an image
    # from before the OCR container ignores it, so a rollback turns reading off
    # instead of running it inside the bot.
    inbox_ocr_enabled: bool = False
    # Socket of the OCR container (no network, no secrets). Empty: read in a
    # child process of this server — development only; production refuses.
    panel_ocr_socket: str = ""
    # Web Push for the panel app (deploy/push-keys.sh). Without all three, off.
    push_vapid_public_key: str = ""
    push_vapid_private_key: str = ""
    push_vapid_subject: str = ""
    # Asaas, the Converge's own account (docs/ASAAS.md). Without the key, off.
    # The key starts with `$`: in the .env it goes in single quotes, or Compose
    # reads `$aact_…` as a variable and the container gets a cut key.
    asaas_api_key: str = ""
    asaas_env: Literal["sandbox", "production"] = "sandbox"
    asaas_webhook_token: str = ""
    # Autentique, the contract's e-signature (spec venda-ate-pagamento, inc. 4).
    # Without the key, off. Sandbox documents cost nothing and have no legal
    # value; a contact in /teste always goes to the sandbox.
    autentique_api_key: str = ""
    autentique_sandbox: bool = True
    # Signs its webhook (HMAC-SHA256 of the body); without it, the route is off.
    autentique_webhook_secret: str = ""
    # Who signs for the Converge (the representative in the contract).
    contract_signer_name: str = ""
    contract_signer_email: str = ""
    # How often the panel's agenda mirror reads the team's calendars.
    calendar_sync_minutes: int = 5

    # ── Retention ────────────────────────────────────────────────────────────
    conversation_retention_days: int = 365
    retention_hour: int = 3

    # ── Observability ────────────────────────────────────────────────────────
    metrics_enabled: bool = True
    langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "ai-agents-reception"
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_hide_inputs: bool = True
    langsmith_hide_outputs: bool = True
    langsmith_capture_content: bool = False

    def workdays(self) -> set[int]:
        """ISO weekdays the desk is open, parsed once from OFFICE_WORKDAYS."""
        days = set()
        for token in self.office_workdays.split(","):
            token = token.strip()
            if token.isdigit() and 1 <= int(token) <= 7:
                days.add(int(token))
        return days

    def whatsapp_configured(self) -> bool:
        """The secret signs Meta's calls, the number and token send, the verify
        token answers the subscription check: without any one, stay off."""
        return all((self.whatsapp_app_secret, self.whatsapp_phone_number_id,
                    self.whatsapp_access_token, self.whatsapp_webhook_verify_token))

    def payments_configured(self) -> bool:
        return bool(self.asaas_api_key)

    @property
    def spacemail_enabled(self) -> bool:
        return bool(self.spacemail_username and self.spacemail_password)

    @property
    def email_alerts_enabled(self) -> bool:
        return self.spacemail_alerts and self.spacemail_enabled

    @property
    def asaas_base_url(self) -> str:
        if self.asaas_env == "production":
            return "https://api.asaas.com/v3"
        return "https://api-sandbox.asaas.com/v3"

    def hours_bypass_chats(self) -> set[str]:
        return {
            token.strip()
            for token in self.hours_bypass_chat_ids.split(",")
            if token.strip()
        }

    @field_validator("office_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Invalid time zone: {value}") from exc
        return value

    @model_validator(mode="after")
    def _guardrails(self):
        if self.app_env == "production":
            if not self.database_url and not self.db_password:
                raise ValueError("DATABASE_URL or DB_PASSWORD is required in production.")
            if not self.telegram_bot_token:
                raise ValueError("TELEGRAM_BOT_TOKEN is required in production.")
            # Without it, anyone who learns the URL can post updates as Telegram.
            if not self.telegram_webhook_secret:
                raise ValueError("TELEGRAM_WEBHOOK_SECRET is required in production.")
            # Without them the desk would have to write customers in clear.
            if not self.data_keys:
                raise ValueError("DATA_KEYS is required in production.")
            if not self.data_index_key:
                raise ValueError("DATA_INDEX_KEY is required in production.")
        if self.data_keys:
            parse_data_keys(self.data_keys)
        if self.data_index_key:
            parse_index_key(self.data_index_key)
        start = self.office_start_hour * 60 + self.office_start_minute
        end = self.office_end_hour * 60 + self.office_end_minute
        if not 0 <= start < end <= 24 * 60:
            raise ValueError("Working hours must start before they end.")
        if not 0 <= self.max_upcoming_appointments <= 5:
            raise ValueError("MAX_UPCOMING_APPOINTMENTS must be 0 (no limit) to 5.")
        if self.minimum_notice_minutes < 0:
            raise ValueError("MINIMUM_NOTICE_MINUTES cannot be negative.")
        if not self.workdays():
            raise ValueError("OFFICE_WORKDAYS must list at least one ISO weekday (1-7).")
        self._media_guardrails()
        return self

    def _media_guardrails(self) -> None:
        # The privacy policy promises these bounds: the code never keeps longer.
        ranges = (
            ("MAX_MEDIA_BYTES", self.max_media_bytes, _MIB, 20 * _MIB),
            ("MEDIA_DOWNLOAD_SECONDS", self.media_download_seconds, 5, 120),
            ("MEDIA_AUDIO_HOURS", self.media_audio_hours, 1, 48),
            ("MEDIA_AUDIO_MAX_HOURS", self.media_audio_max_hours, self.media_audio_hours, 168),
            ("MEDIA_RETENTION_DAYS", self.media_retention_days, 1, 90),
            ("MEDIA_CLEANUP_MINUTES", self.media_cleanup_minutes, 1, 15),
            ("MEDIA_SERVE_CONCURRENCY", self.media_serve_concurrency, 1, 8),
            ("CALENDAR_SYNC_MINUTES", self.calendar_sync_minutes, 1, 15),
        )
        for name, value, low, high in ranges:
            if not low <= value <= high:
                raise ValueError(f"{name} must be {low} to {high}.")
        if self.media_max_total_mb < 1 or self.media_min_free_mb < 0:
            raise ValueError("MEDIA_MAX_TOTAL_MB must be positive and MEDIA_MIN_FREE_MB not negative.")
        # A request in the Inbox needs the team group (handoff_sessions.team_chat_id).
        if self.media_enabled and not self.team_chat_id:
            raise ValueError("MEDIA_ENABLED needs TEAM_CHAT_ID.")

    @model_validator(mode="after")
    def _resolve_agent_models(self):
        if not self.router_model:
            self.router_model = self.llm_simple_model
        for role in ("reception_model", "drafting_model"):
            if not getattr(self, role):
                setattr(self, role, self.llm_complex_model)
        return self

    @model_validator(mode="after")
    def _protect_trace_content(self):
        if not self.langsmith_capture_content and (
            not self.langsmith_hide_inputs or not self.langsmith_hide_outputs
        ):
            raise ValueError(
                "LANGSMITH_CAPTURE_CONTENT=true is required before trace content "
                "can be exposed."
            )
        return self


settings = Settings()


def provider_model_problems(config: "Settings") -> list[str]:
    """Configured models that would be sent to a provider they do not belong to.

    Kept out of the validators on purpose: importing the configuration must not
    be fatal, or one wrong line in `.env` stops the test suite from even being
    collected and hides the message that explains it. The application still
    refuses to start — see `validate_startup_config`.
    """
    def provider_of(model: str) -> str:
        return "gemini" if model.lower().startswith("gemini") else "openai"

    simple, complex_ = config.llm_simple_provider, config.llm_complex_provider
    pairs = [
        ("LLM_SIMPLE_MODEL", config.llm_simple_model, simple),
        ("LLM_COMPLEX_MODEL", config.llm_complex_model, complex_),
        ("ROUTER_MODEL", config.router_model, simple),
        ("RECEPTION_MODEL", config.reception_model, complex_),
        ("DRAFTING_MODEL", config.drafting_model, complex_),
    ]
    problems = [
        f"{name}={model!r} is a {provider_of(model)} model but would be sent to {provider}"
        for name, model, provider in pairs
        if model and provider_of(model) != provider
    ]
    if config.llm_fallback_model and provider_of(config.llm_fallback_model) != "openai":
        problems.append(
            f"LLM_FALLBACK_MODEL={config.llm_fallback_model!r} is not an OpenAI model, "
            "but the fallback is only used for OpenAI"
        )
    return problems


def missing_provider_credentials(config: "Settings") -> list[str]:
    """A selected provider whose credential is absent.

    Only Gemini is checked, and the asymmetry is deliberate: the LLM gateway
    answers a missing Gemini key by serving OpenAI instead, so the provider
    silently becomes one nobody chose. A missing OpenAI key degrades the reply
    but never swaps the provider.
    """
    selected = {
        "LLM_SIMPLE_PROVIDER": config.llm_simple_provider,
        "LLM_COMPLEX_PROVIDER": config.llm_complex_provider,
    }
    if "gemini" in selected.values() and not config.gemini_api_key:
        chosen = sorted(name for name, value in selected.items() if value == "gemini")
        return [
            f"{' and '.join(chosen)} selects gemini but GEMINI_API_KEY is empty, "
            "so requests would silently be answered by OpenAI"
        ]
    return []


def validate_startup_config(config: "Settings | None" = None) -> None:
    """Refuse to start on a configuration that cannot do what it claims."""
    config = config or settings
    problems = provider_model_problems(config) + missing_provider_credentials(config)
    if problems:
        raise RuntimeError(
            "Incompatible provider/model configuration in .env:\n  - "
            + "\n  - ".join(problems)
        )
    # Imported here rather than at the top: the content module reads settings,
    # so importing it above would be a cycle.
    from core.content import validate as validate_content

    validate_content()
    if config.media_enabled or config.panel_uploads_enabled:
        from core.services import media_store

        media_store.ensure_writable()


def _configure_langsmith_environment() -> None:
    """Expose validated settings to the SDK, including local `.env` values."""
    values = {
        "LANGSMITH_TRACING": str(settings.langsmith_tracing).lower(),
        "LANGSMITH_PROJECT": settings.langsmith_project,
        "LANGSMITH_ENDPOINT": settings.langsmith_endpoint,
        "LANGSMITH_HIDE_INPUTS": str(settings.langsmith_hide_inputs).lower(),
        "LANGSMITH_HIDE_OUTPUTS": str(settings.langsmith_hide_outputs).lower(),
    }
    if settings.langsmith_api_key:
        values["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    for name, value in values.items():
        os.environ.setdefault(name, value)


_configure_langsmith_environment()

TELEGRAM_TOKEN = settings.telegram_bot_token
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
OPENAI_KEY = settings.openai_api_key
GEMINI_KEY = settings.gemini_api_key
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_EMBED_URL = "https://api.openai.com/v1/embeddings"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"

EMBED_MODEL = settings.embed_model
EMBED_DIM = settings.embed_dim
