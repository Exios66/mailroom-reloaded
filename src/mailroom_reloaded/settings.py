"""Runtime settings and the taxonomy contract."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, BeforeValidator, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _empty_to_none(value: Any) -> Any:
    """Coerce a blank env value to ``None`` (``.env`` files ship empty lines)."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _anchor_mode(value: Any) -> Any:
    """Blank means ``none``; the value is lower-cased and validated lazily by ``storage/anchor.py``,
    so a typo can never crash startup or the pipeline."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return "none"
    return str(value).strip().lower()


def normalize_trace_keep(value: Any) -> str:
    """Canonical span-retention policy: ``pinned``, ``all`` or ``recent:<N>`` (1 <= N <= 1000).

    Blank or invalid values fall back to ``pinned``, so a typo can never crash startup.
    """
    text = str(value).strip().lower() if value is not None else ""
    if text in ("pinned", "all"):
        return text
    head, _, tail = text.partition(":")
    tail = tail.strip()
    if head == "recent" and tail.isascii() and tail.isdigit() and 1 <= int(tail) <= 1000:
        return f"recent:{int(tail)}"
    return "pinned"


# Optional Jev knobs: a blank ``.env`` line must mean "unset", not a parse error.
def _public_link(value: Any) -> Any:
    """Normalise a configured link base: plain http(s) host, no userinfo/query/fragment.

    ``/links`` is unauthenticated, so a credential-bearing or non-http(s) value must fail
    at startup rather than be served to every caller. A trailing slash is stripped so
    ``f"{base}/path"`` never produces ``//``.
    """
    if not isinstance(value, str):
        return value
    from urllib.parse import urlsplit

    raw = value.strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("must be an http(s) URL with a host")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("must not contain credentials")
    if parts.query or parts.fragment:
        raise ValueError("must not contain a query string or fragment")
    return raw.rstrip("/")


_LinkUrl = Annotated[str, BeforeValidator(_public_link)]
_JevStr = Annotated[str | None, BeforeValidator(_empty_to_none)]
_JevFloat = Annotated[float | None, BeforeValidator(_empty_to_none)]
_JevInt = Annotated[int | None, BeforeValidator(_empty_to_none)]
_OptPath = Annotated[Path | None, BeforeValidator(_empty_to_none)]
_AnchorMode = Annotated[str, BeforeValidator(_anchor_mode)]
_TraceKeep = Annotated[str, BeforeValidator(normalize_trace_keep)]


class DocClass(BaseModel):
    key: str
    label: str
    schema_name: str = Field(alias="schema")
    specialist: str
    description: str
    field_types: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    @property
    def schema(self) -> str:  # type: ignore[override]
        return self.schema_name


class Thresholds(BaseModel):
    low: float
    high: float
    judge_band_high: float
    retry_max: int

    model_config = ConfigDict(frozen=True)


class BertCfg(BaseModel):
    defer_classes: list[str] = Field(default_factory=list)
    max_trusted_windows: int = 1
    enabled: bool = False
    pass_subclass_hint: bool = False


class RunConditions(BaseModel):
    input_cap_chars: int
    output_cap_tokens: int
    temperature: float
    retries: int
    merger_mode: Literal["frozen", "dagger"] = "frozen"

    model_config = ConfigDict(frozen=True)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Positional construction: RunConditions(24000, 8192, 0.7, 2, "frozen")
        if args:
            kwargs.update(zip(type(self).model_fields, args))
        super().__init__(**kwargs)


class AgentCfg(BaseModel):
    provider: str = "openrouter"
    model: str
    tier: str | None = None
    temperature: float = 0.1
    max_tokens: int | None = None
    max_input_chars: int = 25000
    reasoning_effort: str | None = None
    procedural: bool = False


class Taxonomy(BaseModel):
    classes: dict[str, DocClass]
    confidence: dict[str, Any]
    bert: BertCfg
    agents: dict[str, AgentCfg]
    conditions: dict[str, RunConditions]
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    def confidence_for(self, doc_type: str | None) -> Thresholds:
        base = self.confidence
        merged = {k: base[k] for k in ("low", "high", "judge_band_high", "retry_max")}
        if doc_type is not None:
            merged.update(base.get("by_class", {}).get(doc_type, {}))
        return Thresholds(**merged)

    def agent(self, name: str) -> AgentCfg:
        return self.agents[name]

    def specialist_conditions(self, doc_type: str) -> RunConditions:
        return self.conditions[doc_type]


def _build_taxonomy(data: dict[str, Any]) -> Taxonomy:
    classes = {d["key"]: DocClass(**d) for d in data["doc_classes"]}
    return Taxonomy(
        classes=classes,
        confidence=data["confidence"],
        bert=BertCfg(**data.get("bert", {})),
        agents={k: AgentCfg(**v) for k, v in data["agents"].items()},
        conditions={k: RunConditions(**v) for k, v in data["specialist_conditions"].items()},
        raw=data,
    )


@lru_cache(maxsize=1)
def load_taxonomy() -> Taxonomy:
    text = (resources.files("mailroom_reloaded") / "config" / "taxonomy.yaml").read_text("utf-8")
    return _build_taxonomy(yaml.safe_load(text))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MAILROOM_", env_file=".env", extra="ignore", populate_by_name=True
    )

    base_dir: Path = Path("./data")
    provider: str = Field(
        default="mock", validation_alias=AliasChoices("DEFAULT_PROVIDER", "MAILROOM_PROVIDER")
    )
    vllm_base_url: str | None = Field(
        default=None, validation_alias=AliasChoices("VLLM_BASE_URL", "MAILROOM_VLLM_BASE_URL")
    )
    openrouter_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENROUTER_API_KEY", "MAILROOM_OPENROUTER_API_KEY"),
    )
    llamafile_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLAMAFILE_BASE_URL", "MAILROOM_LLAMAFILE_BASE_URL"),
    )
    # Jev (issue #8) knobs: resolved from the environment / ``.env`` here so a
    # ``MAILROOM_JEV_PROVIDER=openrouter`` line in ``.env`` is honoured. Each
    # field accepts both the ``MAILROOM_JEV_<X>`` and bare ``JEV_<X>`` names.
    # Defaults are ``None`` on purpose: an unset field must fall through to the
    # taxonomy ``jev:`` block and then the code default, preserving the
    # documented env -> taxonomy -> default resolution order.
    jev_provider: _JevStr = Field(
        default=None, validation_alias=AliasChoices("MAILROOM_JEV_PROVIDER", "JEV_PROVIDER")
    )
    jev_model: _JevStr = Field(
        default=None, validation_alias=AliasChoices("MAILROOM_JEV_MODEL", "JEV_MODEL")
    )
    jev_base_url: _JevStr = Field(
        default=None, validation_alias=AliasChoices("MAILROOM_JEV_BASE_URL", "JEV_BASE_URL")
    )
    jev_api_key: _JevStr = Field(
        default=None, validation_alias=AliasChoices("MAILROOM_JEV_API_KEY", "JEV_API_KEY")
    )
    jev_temperature: _JevFloat = Field(
        default=None,
        validation_alias=AliasChoices("MAILROOM_JEV_TEMPERATURE", "JEV_TEMPERATURE"),
    )
    jev_accept_threshold: _JevFloat = Field(
        default=None,
        validation_alias=AliasChoices(
            "MAILROOM_JEV_ACCEPT_THRESHOLD", "JEV_ACCEPT_THRESHOLD"
        ),
    )
    jev_timeout_s: _JevFloat = Field(
        default=None,
        validation_alias=AliasChoices("MAILROOM_JEV_TIMEOUT_S", "JEV_TIMEOUT_S"),
    )
    jev_max_retries: _JevInt = Field(
        default=None,
        validation_alias=AliasChoices("MAILROOM_JEV_MAX_RETRIES", "JEV_MAX_RETRIES"),
    )
    api_token: str | None = None
    gmail_push_audience: str | None = None
    gmail_push_service_account: str | None = None
    trace_mask: bool = False
    #: SQLite file for the local span store; ``None`` means ``<base_dir>/traces.db``.
    trace_store_path: _OptPath = None
    #: Which runs' spans survive pruning: ``pinned``, ``all`` or ``recent:<N>`` (storage/retention.py).
    trace_keep: _TraceKeep = "pinned"
    # External anchor of the archive-ledger head (storage/anchor.py). ``none`` is the default;
    # ``supabase`` (HTTPS, no extra dependency) is the recommended backend, ``postgres`` needs the
    # ``anchor`` extra, ``export`` only prints the head for off-host pinning.
    anchor: _AnchorMode = "none"
    anchor_url: _JevStr = Field(default=None, repr=False)  # a DSN may embed a password
    anchor_key: _JevStr = Field(default=None, repr=False)
    anchor_key_file: _OptPath = None
    gpu_usd_per_hour: float = 0.80
    # Public base URLs for the UI's outbound observability links (GET /links). With the
    # ``MAILROOM_`` env prefix these read ``MAILROOM_PUBLIC_URL`` / ``MAILROOM_PHOENIX_URL``
    # / ``MAILROOM_GRAFANA_URL``. No secret is stored here: each must be a plain http(s) URL without credentials.
    public_url: _LinkUrl = "http://localhost:8000"
    phoenix_url: _LinkUrl = "http://localhost:6006"
    grafana_url: _LinkUrl = "http://localhost:3000"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# ------------------------------------------------------------------ Jev (issue #8)

_JEV_PROVIDERS = ("off", "openrouter", "typesafe", "local")

# Per-provider model/base_url used when the env/taxonomy leaves them blank.
_JEV_PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "off": {"model": "", "base_url": ""},
    "openrouter": {
        "model": "typesafe/jev-1.13",
        "base_url": "https://openrouter.ai/api/alpha/decisions",
    },
    "typesafe": {
        "model": "jev-latest",
        "base_url": "https://api.typesafe.ai/v1/systemone",
    },
    "local": {
        "model": "jevk5",
        "base_url": "http://127.0.0.1:8090/v1/systemone",
    },
}

_JEV_DEFAULTS: dict[str, float | int] = {
    "temperature": 1.0,
    "accept_threshold": 0.8,
    "timeout_s": 10.0,
    "max_retries": 2,
}


@dataclass(frozen=True)
class JevConfig:
    """Resolved Jev (TypeSafe System One) decision-model configuration.

    ``provider`` is one of ``off``/``openrouter``/``typesafe``/``local``.
    Resolution order per field is ``MAILROOM_JEV_<FIELD>`` -> ``JEV_<FIELD>`` ->
    the taxonomy ``jev:`` block -> a default. ``model``/``base_url`` default per
    provider; the numeric fields use the module defaults.

    ``temperature`` is reserved for the **local** transport (the local JevK5
    runtime uses 1.22): it is NOT included in hosted OpenRouter/TypeSafe
    requests, whose APIs expose no such field.
    """

    provider: Literal["off", "openrouter", "typesafe", "local"]
    model: str
    base_url: str
    api_key: str | None
    # Reserved for the local JevK5 transport; NOT sent in hosted requests.
    temperature: float
    accept_threshold: float
    timeout_s: float
    max_retries: int

    @property
    def enabled(self) -> bool:
        """True when a provider other than ``off`` is configured."""
        return self.provider != "off"


def _jev_env(name: str, settings: Settings | None = None) -> str | None:
    """Resolve one Jev field.

    ``os.environ`` wins: ``MAILROOM_JEV_<NAME>`` then ``JEV_<NAME>`` when set
    and non-empty. Otherwise fall back to the ``Settings`` value (which reads
    ``.env``) so a ``.env``-style ``MAILROOM_JEV_<NAME>`` is honoured. A blank
    env value is treated as unset, so the taxonomy block can still apply.
    """
    for key in (f"MAILROOM_JEV_{name}", f"JEV_{name}"):
        value = os.environ.get(key)
        if value:
            return value
    resolved = settings if settings is not None else get_settings()
    value = getattr(resolved, f"jev_{name.lower()}", None)
    if value is None:
        return None
    text = str(value)
    return text or None


def _jev_field(name: str, taxonomy: dict[str, Any]) -> str | None:
    """Resolve one string field: env -> taxonomy -> None (caller applies defaults)."""
    value = _jev_env(name)
    if value is None:
        raw = taxonomy.get(name.lower())
        if raw is not None and raw != "":
            return str(raw)
    return value


def _jev_scalar(
    name: str, taxonomy: dict[str, Any], default: float, cast: Callable[[Any], Any]
) -> Any:
    """Resolve a numeric field: env -> taxonomy -> ``default``, then ``cast``."""
    value = _jev_field(name, taxonomy)
    if value is None:
        return cast(default)
    return cast(value)


def _jev_api_key(settings: Settings, provider: str) -> str | None:
    """Resolve the API key with provider-specific precedence.

    ``MAILROOM_JEV_API_KEY`` -> ``JEV_API_KEY`` always win. Otherwise the
    provider's own variable is preferred (``openrouter`` -> ``OPENROUTER_API_KEY``;
    ``typesafe`` -> ``TYPESAFE_API_KEY``), then the other provider's variable,
    then ``settings.openrouter_api_key``. It may be ``None`` for the ``local``
    provider (no auth header is then sent).
    """
    value = _jev_env("API_KEY", settings)
    if value:
        return value
    order = {
        "openrouter": ("OPENROUTER_API_KEY", "TYPESAFE_API_KEY"),
        "typesafe": ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY"),
    }.get(provider, ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY"))
    for key in order:
        value = os.environ.get(key)
        if value:
            return value
    return settings.openrouter_api_key


def jev_config() -> JevConfig:
    """Resolve the Jev config from env, the taxonomy ``jev:`` block and defaults.

    Not cached on purpose: env changes (tests, CLI overrides) take effect
    immediately. Unknown providers collapse to ``off`` so the default pipeline
    behaviour is unchanged. ``temperature`` is reserved for the local transport
    and is not sent to hosted endpoints.
    """
    taxonomy = dict(load_taxonomy().raw.get("jev") or {})
    provider = (
        _jev_env("PROVIDER") or str(taxonomy.get("provider") or "off")
    ).strip().lower()
    if provider not in _JEV_PROVIDERS:
        provider = "off"
    defaults = _JEV_PROVIDER_DEFAULTS[provider]
    return JevConfig(
        provider=provider,  # type: ignore[arg-type]
        model=_jev_field("MODEL", taxonomy) or defaults["model"],
        base_url=_jev_field("BASE_URL", taxonomy) or defaults["base_url"],
        api_key=_jev_api_key(get_settings(), provider),
        temperature=_jev_scalar("TEMPERATURE", taxonomy, _JEV_DEFAULTS["temperature"], float),
        accept_threshold=_jev_scalar(
            "ACCEPT_THRESHOLD", taxonomy, _JEV_DEFAULTS["accept_threshold"], float
        ),
        timeout_s=_jev_scalar("TIMEOUT_S", taxonomy, _JEV_DEFAULTS["timeout_s"], float),
        max_retries=_jev_scalar("MAX_RETRIES", taxonomy, _JEV_DEFAULTS["max_retries"], int),
    )
