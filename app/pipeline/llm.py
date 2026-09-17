"""Shared LLM call helper.

Section 3 of the plan: every LLM call site (schema matching, extraction,
topic suggestion, dedup-adjacent decisions, query routing, NL-to-SQL) is one
focused LLM call with a JSON-schema response shape, going through exactly
two functions - `llm_classify` and `llm_extract` - so the "list + LLM
classifies" pattern (decisions #10/#11/#20) and the extraction pattern are
each implemented once.

The provider is Groq. The client is still the `openai` SDK because Groq
exposes an OpenAI-compatible /chat/completions surface, so a base_url and a
key are the whole difference - not worth a second SDK and a second code path
for. The one thing that is provider-specific is the model: `GROQ_MODEL` has
to name a model that honours `response_format: json_schema` with
`strict: true`, since both helpers below rely on the response parsing as
JSON that fits the schema they were handed.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from openai import OpenAI

from app.config import get_settings


@lru_cache
def _client() -> OpenAI:
    settings = get_settings()
    return OpenAI(api_key=settings.groq_api_key, base_url=settings.groq_base_url)


def _call_llm_json(
    *,
    system: str,
    user: str,
    json_schema: dict[str, Any],
    schema_name: str,
) -> dict[str, Any]:
    settings = get_settings()
    response = _client().chat.completions.create(
        model=settings.groq_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": json_schema,
                "strict": True,
            },
        },
    )
    content = response.choices[0].message.content
    return json.loads(content)


CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "selected_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "ids of options that clearly apply; empty array if none do",
        },
        "new_proposal": {
            "type": ["object", "null"],
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["name", "description"],
            "additionalProperties": False,
            "description": "a new option to propose when none of the existing ones fit",
        },
    },
    "required": ["selected_ids", "new_proposal"],
    "additionalProperties": False,
}


def llm_classify(
    *,
    instructions: str,
    text: str,
    options: list[dict[str, str]],
    allow_new: bool = False,
) -> dict[str, Any]:
    """"List + LLM classifies" (decisions #10/#11/#20).

    `options` is a list of {"id", "name", "description"}. Returns
    {"selected_ids": [...], "new_proposal": {"name","description"} | None}.
    """
    options_block = "\n".join(f"- id={o['id']} name={o['name']!r} description={o.get('description', '')!r}" for o in options)
    new_hint = (
        "If none of the options clearly apply, propose a new one via `new_proposal`."
        if allow_new
        else "Only choose from the listed options; leave `new_proposal` null."
    )
    user = (
        f"{instructions}\n\nExisting options:\n{options_block or '(none yet)'}\n\n"
        f"{new_hint}\n\nContent to classify:\n{text}"
    )
    return _call_llm_json(
        system="You are a precise document classification assistant. Only select options that clearly apply.",
        user=user,
        json_schema=CLASSIFY_SCHEMA,
        schema_name="classify_result",
    )


def llm_extract(
    *,
    instructions: str,
    text: str,
    json_schema: dict[str, Any],
    schema_name: str = "extract_result",
) -> dict[str, Any]:
    """Structured extraction against an arbitrary caller-supplied JSON schema."""
    user = f"{instructions}\n\nDocument content:\n{text}"
    return _call_llm_json(
        system="You are a precise structured-data extraction assistant. Only report values you can support from the text.",
        user=user,
        json_schema=json_schema,
        schema_name=schema_name,
    )
