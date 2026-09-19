"""Shared LLM call helper.

Section 3 of the plan: every LLM call site (schema matching, extraction,
topic suggestion, dedup-adjacent decisions, query routing, NL-to-SQL) is one
focused LLM call with a JSON-schema response shape, going through exactly
two functions - `llm_classify` and `llm_extract` - so the "list + LLM
classifies" pattern (decisions #10/#11/#20) and the extraction pattern are
each implemented once.

The provider is Gemini, reached through its OpenAI-compatible
/chat/completions surface, which is why the client is the `openai` SDK: a
base_url and a key are the whole difference, not worth a second SDK and a
second code path for. The one thing that is provider-specific is the model:
`LLM_MODEL` has to name a model that honours `response_format: json_schema`
with `strict: true`, since both helpers below rely on the response parsing as
JSON that fits the schema they were handed. Verified on
gemini-3.5-flash-lite, including the `["object", "null"]` union in
CLASSIFY_SCHEMA that is the least portable thing either schema does.

Because both helpers funnel into one function, this is also the only place
rate limiting has to be installed. Gemini's free tier meters *requests* per
minute rather than tokens, so `_call_llm_json` takes a slot from the pacer in
`ratelimit.py` before sending, and on a 429 hands the error back so the real
quota can be read out of it. Requests being the metered resource is why
`llm_classify_multi` exists: collapsing two classifications into one call is
worth more than any amount of prompt trimming.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from openai import OpenAI, RateLimitError

from app.config import get_settings
from app.pipeline.ratelimit import get_limiter, quota_from_error


@lru_cache
def _client() -> OpenAI:
    settings = get_settings()
    return OpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        # Deliberately low. A retry the SDK performs internally is a request
        # the pacer never counted, so on a request-metered quota each one eats
        # capacity the pacer is already waiting for - retrying harder makes a
        # rate limit worse, not better. The pacer is the primary defence and
        # the worker's requeue (with its own delay) is the fallback; these two
        # only cover a transient blip or a 5xx.
        max_retries=2,
        # Without this a hung connection blocks the worker on one job
        # indefinitely, and the queue silently stops moving.
        timeout=120.0,
    )


def _call_llm_json(
    *,
    system: str,
    user: str,
    json_schema: dict[str, Any],
    schema_name: str,
) -> dict[str, Any]:
    settings = get_settings()
    limiter = get_limiter()
    limiter.acquire()
    try:
        response = _client().chat.completions.create(
            model=settings.llm_model,
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
            # A runaway guard only, set well above what real replies cost.
            # See the `finish_reason` check below for why it is not tighter.
            max_tokens=settings.llm_max_completion_tokens,
        )
    except RateLimitError as exc:
        # The 429 body carries the provider's real per-minute quota, which is
        # better information than anything in our config.
        quota, _ = quota_from_error(exc)
        limiter.note_rate_limited(reported_quota=quota)
        raise

    choice = response.choices[0]
    # Running out of completion budget has to be an error, because it does not
    # look like one. Under `response_format: json_schema` the provider closes
    # the JSON it was part-way through, so the reply still parses and still
    # fits the schema - it is just missing content. A truncated extraction came
    # back as 21 of an invoice's 25 rows, with nothing to distinguish it from a
    # 21-row invoice. Everything downstream (review, confirm, the schema views)
    # would have treated those 21 as the whole document.
    if choice.finish_reason == "length":
        raise RuntimeError(
            f"LLM reply for {schema_name!r} was truncated at the "
            f"{settings.llm_max_completion_tokens}-token completion limit. The JSON still parses "
            "but is incomplete, so it is rejected rather than stored. Raise "
            "LLM_MAX_COMPLETION_TOKENS, or lower MAX_EXTRACT_CHARS so each call has less to report."
        )

    return json.loads(choice.message.content)


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
    options_block = _options_block(options)
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


def _options_block(options: list[dict[str, str]]) -> str:
    return "\n".join(
        f"- id={o['id']} name={o['name']!r} description={o.get('description', '')!r}" for o in options
    )


def _classify_task_schema() -> dict[str, Any]:
    """One task's slot in the multi-classify response - same shape as
    `CLASSIFY_SCHEMA`, so each task's result is interchangeable with a single
    `llm_classify` result and callers need no second parsing path."""
    return {
        "type": "object",
        "properties": dict(CLASSIFY_SCHEMA["properties"]),
        "required": list(CLASSIFY_SCHEMA["required"]),
        "additionalProperties": False,
    }


def llm_classify_multi(*, tasks: list[dict[str, Any]], text: str) -> dict[str, dict[str, Any]]:
    """Several independent classifications of the *same* text, in one call.

    Requests are the metered resource on this provider (see `ratelimit.py`), so
    two classifications of one document cost twice as much as they need to when
    sent separately - and they also send the document twice. Schema matching and
    topic suggestion are both "list + LLM classifies" over identical input, run
    back to back, which makes them the obvious pair to merge: it takes the
    pipeline from three requests per document to two.

    `tasks` is a list of {"key", "instructions", "options", "allow_new"}.
    Returns {key: {"selected_ids": [...], "new_proposal": ... | None}} - each
    value identical in shape to what `llm_classify` returns, so a caller can be
    switched between the two without changing how it reads the result.

    The tasks stay explicitly separate in both prompt and schema rather than
    being blended into one question. Asking "which schema and which topics"
    as a single classification would let one answer contaminate the other,
    which is exactly the failure the one-focused-call-per-decision rule
    (decision #20) exists to avoid.
    """
    if not tasks:
        return {}

    sections = []
    for task in tasks:
        new_hint = (
            "If none of these options clearly apply, propose a new one via `new_proposal`."
            if task.get("allow_new")
            else "Only choose from the listed options; leave `new_proposal` null."
        )
        sections.append(
            f"### Task `{task['key']}`\n{task['instructions']}\n\n"
            f"Options:\n{_options_block(task.get('options') or []) or '(none yet)'}\n\n{new_hint}"
        )

    user = (
        "Answer each task below independently about the same document. A task's answer must not "
        "influence another's; judge each only on the document's content.\n\n"
        + "\n\n".join(sections)
        + f"\n\nContent to classify:\n{text}"
    )
    json_schema = {
        "type": "object",
        "properties": {task["key"]: _classify_task_schema() for task in tasks},
        "required": [task["key"] for task in tasks],
        "additionalProperties": False,
    }
    result = _call_llm_json(
        system="You are a precise document classification assistant. Only select options that clearly apply.",
        user=user,
        json_schema=json_schema,
        schema_name="classify_multi_result",
    )
    # Never let a missing key become a KeyError in the pipeline; an absent task
    # reads as "nothing applied", which is what a single classify would also
    # return for a document nothing matched.
    return {task["key"]: result.get(task["key"]) or {"selected_ids": [], "new_proposal": None} for task in tasks}


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
