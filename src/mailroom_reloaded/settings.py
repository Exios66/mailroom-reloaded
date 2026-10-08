"""Runtime settings and the taxonomy contract."""

from __future__ import annotations

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
