"""Runtime settings and the taxonomy contract."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    api_token: str | None = None
    trace_mask: bool = False
    gpu_usd_per_hour: float = 0.80


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
    """

    provider: Literal["off", "openrouter", "typesafe", "local"]
    model: str
    base_url: str
    api_key: str | None
    temperature: float
    accept_threshold: float
    timeout_s: float
    max_retries: int

    @property
    def enabled(self) -> bool:
        """True when a provider other than ``off`` is configured."""
        return self.provider != "off"


def _jev_env(name: str) -> str | None:
    """Return ``MAILROOM_JEV_<NAME>`` then ``JEV_<NAME>`` when set and non-empty."""
    for key in (f"MAILROOM_JEV_{name}", f"JEV_{name}"):
        value = os.environ.get(key)
        if value:
            return value
    return None


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


def _jev_api_key(settings: Settings) -> str | None:
    """Resolve the API key.

    Order: ``MAILROOM_JEV_API_KEY`` -> ``JEV_API_KEY`` -> ``TYPESAFE_API_KEY`` ->
    ``OPENROUTER_API_KEY`` -> ``settings.openrouter_api_key``. It may be ``None``
    for the ``local`` provider (no auth header is then sent).
    """
    value = _jev_env("API_KEY")
    if value:
        return value
    for key in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY"):
        value = os.environ.get(key)
        if value:
            return value
    return settings.openrouter_api_key


def jev_config() -> JevConfig:
    """Resolve the Jev config from env, the taxonomy ``jev:`` block and defaults.

    Not cached on purpose: env changes (tests, CLI overrides) take effect
    immediately. Unknown providers collapse to ``off`` so the default pipeline
    behaviour is unchanged.
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
        api_key=_jev_api_key(get_settings()),
        temperature=_jev_scalar("TEMPERATURE", taxonomy, _JEV_DEFAULTS["temperature"], float),
        accept_threshold=_jev_scalar(
            "ACCEPT_THRESHOLD", taxonomy, _JEV_DEFAULTS["accept_threshold"], float
        ),
        timeout_s=_jev_scalar("TIMEOUT_S", taxonomy, _JEV_DEFAULTS["timeout_s"], float),
        max_retries=_jev_scalar("MAX_RETRIES", taxonomy, _JEV_DEFAULTS["max_retries"], int),
    )
