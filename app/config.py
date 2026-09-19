"""Runtime configuration, loaded from environment variables / .env.

Section 7 of the plan: LLM_API_KEY, DATABASE_URL, embedding model name
etc. are all supplied via .env, loaded through Pydantic settings.
"""

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://raw2query:raw2query@localhost:5432/raw2query"
    # Restricted role, SELECT-only on the view_* views (see migrations) - the
    # DB-enforced backstop for NL-to-SQL (plan section 3, call site 11).
    readonly_database_url: str = "postgresql+psycopg://raw2query_query_runner:raw2query_query_runner@localhost:5432/raw2query"
    # Escape hatch for running generated SQL as the read-write role when no
    # restricted role is available. Off by default and never appropriate
    # outside local debugging: with it on, safety rail (b) is gone and only
    # the parser rail stands between a generated statement and the data.
    allow_readwrite_query_fallback: bool = False

    # `GEMINI_API_KEY` is accepted as well as `LLM_API_KEY`, because that is
    # the name the key arrives under from AI Studio and it is the first thing
    # anyone will try.
    llm_api_key: str = Field(default="", validation_alias=AliasChoices("LLM_API_KEY", "GEMINI_API_KEY"))
    # Gemini serves an OpenAI-compatible surface, which is why the client in
    # app/pipeline/llm.py is the `openai` SDK pointed at this URL.
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    # Must honour `response_format: json_schema` with `strict: true` - every
    # call site depends on it (see llm.py). Verified working on
    # gemini-3.5-flash-lite and gemini-2.5-flash, including the
    # `{"type": ["object", "null"]}` union in CLASSIFY_SCHEMA and the nested
    # {values, confidence} + rows shape the extraction schema builds.
    #
    # flash-lite over flash on purpose: the free tier meters *requests*, and
    # the measured quota is 15/min for gemini-3.5-flash-lite against 5/min for
    # gemini-2.5-flash - 3x the throughput. Extraction accuracy was checked on
    # a 25-line invoice and came back complete and correct.
    llm_model: str = "gemini-3.5-flash-lite"
    # Requests-per-minute ceiling, which on Gemini's free tier - not tokens -
    # is what actually binds. Measured from the provider's own 429 payload
    # (`GenerateRequestsPerMinutePerProjectPerModel-FreeTier`, quotaValue 15).
    # The pacer adopts a different value if a 429 ever reports one, so a tier
    # change needs no edit here. See app/pipeline/ratelimit.py.
    llm_requests_per_minute: int = 15
    # Aim at this fraction of the ceiling, to cover the gap between our count
    # and the provider's.
    llm_rate_limit_margin: float = 0.9
    # A runaway guard, NOT a routine bound - deliberately generous. Capping
    # this tightly is unsafe: with `response_format: json_schema` the provider
    # still returns *parseable* JSON when it runs out of completion budget, so
    # a 25-row invoice came back as 21 rows with no error anywhere. Silent row
    # loss, on the data a human is about to confirm. llm.py treats
    # `finish_reason == "length"` as a hard failure for the same reason.
    llm_max_completion_tokens: int = 8192
    # Threshold for one-shot extraction: above this, extraction falls back to
    # one call per chunk (see _extract_fields). Deliberately high, because each
    # chunk is a separate *request* and requests are the metered resource - a
    # smaller value buys nothing and costs throughput. gemini-3.5-flash-lite
    # accepts far more than this in one prompt.
    max_extract_chars: int = 40_000

    embedding_model_name: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384

    upload_dir: str = "./data/uploads"

    confidence_threshold: float = 0.75

    worker_poll_interval_seconds: float = 1.0
    worker_id: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
