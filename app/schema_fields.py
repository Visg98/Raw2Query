"""Column-key assignment and historical-name analysis for schema fields.

A schema field's column in `view_<schema>` used to be `safe_ident(field.name)`,
recomputed wherever it was needed and persisted nowhere - so the LLM catalog
and the records grid both guessed it wrong, a field named `id` collided with
the view's own `er.id` and broke `CREATE VIEW` outright, and two names sharing
a 63-character prefix collided only *after* Postgres truncated them, which the
dedup loop structurally could not see.

Each field now carries an opaque `column` key, minted once and stored in
`schema_versions.fields[i]`. It is 10 characters and matches
`^f_[0-9a-f]{8}$`, which makes that pattern a reserved namespace inside a
generated view: it cannot collide with a bookkeeping column, cannot be
truncated, and needs no order-dependent disambiguation suffix.

This module is a pure leaf - it imports nothing from the rest of the app, so
`views.py` (which renders SQL text), `routers/schemas.py`, `pipeline/confirm.py`
and `pipeline/extract.py` (which renders a SQLAlchemy filter) can all share it
without a cycle. Crucially, the historical-name analysis exists here once:
the view generator and the dedup matcher must agree about which JSON key holds
a given field's value for a given row, or they disagree about what a record is.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field as dataclass_field

# The stored key's format. Re-asserted at the point of SQL interpolation in
# `views.py`, not just here: `confirm.py` writes `proposed_schema_fields` into
# JSONB verbatim without passing through Pydantic, so an API-layer check alone
# would not cover every path to the value that becomes a SQL identifier.
COLUMN_KEY_RE = re.compile(r"^f_[0-9a-f]{8}$")

# Names the generated view projects itself; a field key must never look like
# one. The `f_` prefix already guarantees this, and the test suite pins it.
RESERVED_COLUMNS = frozenset(
    {
        "id",
        "document_id",
        "row_index",
        "schema_version_id",
        "is_duplicate_of",
        "created_at",
        "topic_ids",
    }
)

# The two field scopes. Kept here rather than imported from
# `schemas_pydantic` because this module is a pure leaf with no app imports,
# and because `confirm.py`'s ad hoc promote path reads the value straight out
# of JSONB without passing through Pydantic at all.
DOCUMENT_SCOPE = "document"
ROW_SCOPE = "row"


def field_scope(field: dict) -> str:
    """A field's scope, defaulting to document.

    Anything that isn't exactly `"row"` reads as document scope: a stored
    field from before scopes existed has no `scope` key, and an LLM-proposed
    ad hoc field may carry junk. Defaulting the unknown case to document
    scope means the worst outcome is the pre-existing one-row behaviour,
    never a silently dropped or duplicated record.
    """
    return ROW_SCOPE if field.get("scope") == ROW_SCOPE else DOCUMENT_SCOPE


def split_by_scope(fields: list[dict] | None) -> tuple[list[dict], list[dict]]:
    """`(document_scoped, row_scoped)`, preserving declaration order."""
    document, row = [], []
    for f in fields or []:
        (row if field_scope(f) == ROW_SCOPE else document).append(f)
    return document, row


class ColumnKeyError(ValueError):
    """Invalid field/column input. Routers translate this into a 400."""


def mint_column_key(taken: set[str] | None = None) -> str:
    """A fresh key, avoiding anything already spoken for in this lineage."""
    taken = taken or set()
    while True:
        key = f"f_{uuid.uuid4().hex[:8]}"
        if key not in taken:
            return key


def _is_valid(key: object) -> bool:
    return isinstance(key, str) and COLUMN_KEY_RE.fullmatch(key) is not None


def lineage_name_map(lineage_versions: list[list[dict]]) -> tuple[dict[str, str], set[str]]:
    """`(name -> key, all keys)` accumulated over a lineage, oldest version first.

    Accumulated across the whole lineage rather than reset per version: for
    `Total -> Net -> Total`, resetting would mint a second key for the third
    version and produce two half-empty columns. "Same name in this lineage is
    the same field" is also the only reading available when no rename
    information was recorded, and it makes the common delete-then-re-add
    reclaim its original column.
    """
    name_to_key: dict[str, str] = {}
    known: set[str] = set()
    for fields in lineage_versions:
        for f in fields or []:
            key = f.get("column")
            if _is_valid(key):
                known.add(key)
                name_to_key[f["name"]] = key
    return name_to_key, known


def assign_column_keys(
    new_fields: list[dict], *, lineage_versions: list[list[dict]] | None = None
) -> list[dict]:
    """Returns `new_fields` with a `column` on every entry.

    `lineage_versions` is the field list of each existing version of the same
    schema lineage, oldest first. When it is empty the lineage is brand new, so
    there is nothing to preserve and every key is minted - any client-supplied
    `column` is ignored rather than trusted, which is what the ad hoc
    promote path in `confirm.py` needs.

    Resolution order per field:

    1. A valid supplied `column` *is* the rename signal - it is honoured even
       when `name` changed, which is the whole point of persisting it.
    2. Otherwise match by exact name against the lineage, skipping keys already
       claimed in this request. That guard is load-bearing: renaming `A` to `B`
       (echoing its key) while adding a new field also called `A` must not hand
       both fields the same column.
    3. Otherwise mint.
    """
    lineage_versions = lineage_versions or []
    name_to_key, known = lineage_name_map(lineage_versions)
    trust_supplied = bool(lineage_versions)

    seen_names: set[str] = set()
    used: set[str] = set()

    # Pass 1: validate the request as a whole, and claim supplied keys before
    # any name-matching runs so step 2 can see what is already taken.
    for f in new_fields:
        name = (f.get("name") or "").strip()
        if not name:
            raise ColumnKeyError("every field needs a non-empty name")
        if name in seen_names:
            raise ColumnKeyError(f"duplicate field name {name!r}")
        seen_names.add(name)

        supplied = f.get("column")
        if supplied is None or not trust_supplied:
            continue
        if not _is_valid(supplied):
            raise ColumnKeyError(f"{supplied!r} is not a valid column key")
        if supplied in used:
            raise ColumnKeyError(f"column key {supplied!r} is used by more than one field")
        if supplied not in known:
            # Rejected, never silently re-minted: a silently re-minted key is
            # exactly how "the rename didn't keep its column" ships unnoticed,
            # and the split column it leaves behind cannot be repaired without
            # hand-editing JSONB.
            raise ColumnKeyError(
                f"column key {supplied!r} does not belong to this schema"
            )
        used.add(supplied)

    resolved = []
    for f in new_fields:
        supplied = f.get("column") if trust_supplied else None
        if _is_valid(supplied):
            key = supplied
        else:
            candidate = name_to_key.get((f.get("name") or "").strip())
            key = candidate if candidate and candidate not in used else mint_column_key(known | used)
            used.add(key)
        resolved.append({**f, "column": key})
    return resolved


@dataclass(frozen=True)
class FieldSpec:
    """One projected column of a generated view.

    `branches` maps a JSON key to the versions whose rows store the field under
    that key, newest version first. `plain` says the value can be read as a
    bare `data->>'name'` with no version test (see `_plain_is_safe`).
    """

    column: str
    name: str
    type: str
    description: str
    branches: list[tuple[str, list[uuid.UUID]]] = dataclass_field(default_factory=list)
    plain: bool = False
    # Taken from the newest version that declares the field, like name/type.
    # Deliberately NOT part of the view projection: a row-scoped field is read
    # out of `data` exactly like a document-scoped one, because `confirm.py`
    # denormalizes the header onto every row before it is stored. Scope only
    # governs how many records one document produces.
    scope: str = DOCUMENT_SCOPE


def field_specs(versions: list[tuple[uuid.UUID, list[dict]]]) -> list[FieldSpec]:
    """The projection spec for a lineage, given `(version_id, fields)` ascending.

    Column order is first-seen key with versions ascending, and is therefore
    stable across regenerations - `SELECT *` consumers would otherwise see
    columns shuffle whenever a view was rebuilt.

    `name`/`type`/`description` come from the newest version that declares the
    field, so a retype behaves as it did before: the latest declaration wins.
    """
    order: list[str] = []
    latest: dict[str, dict] = {}
    names_for_key: dict[str, dict[str, list[uuid.UUID]]] = {}
    owners_of_name: dict[str, set[str]] = {}
    versions_declaring: dict[str, set[uuid.UUID]] = {}
    all_version_ids: list[uuid.UUID] = []

    for version_id, fields in versions:
        all_version_ids.append(version_id)
        for f in fields or []:
            key = f.get("column")
            if not _is_valid(key):
                # Refused rather than repaired: minting here would mean a write
                # from a read path, and two processes would race to different
                # keys for the same field.
                raise ColumnKeyError(
                    f"field {f.get('name')!r} has no valid column key "
                    f"({key!r}) - run migration 0005_field_column_keys"
                )
            name = f["name"]
            if key not in names_for_key:
                order.append(key)
                names_for_key[key] = {}
            latest[key] = f
            names_for_key[key].setdefault(name, []).append(version_id)
            owners_of_name.setdefault(name, set()).add(key)
            versions_declaring.setdefault(name, set()).add(version_id)

    specs = []
    for key in order:
        by_name = names_for_key[key]
        # Newest first, so the current name is tried before any historical one.
        branches = [
            (name, sorted(set(vids), key=all_version_ids.index))
            for name, vids in sorted(
                by_name.items(), key=lambda kv: all_version_ids.index(kv[1][-1]), reverse=True
            )
        ]
        f = latest[key]
        specs.append(
            FieldSpec(
                column=key,
                name=f["name"],
                type=f.get("type", "string"),
                description=f.get("description", "") or "",
                branches=branches,
                plain=_plain_is_safe(key, by_name, owners_of_name, versions_declaring, all_version_ids),
                scope=field_scope(f),
            )
        )
    return specs


def _plain_is_safe(
    key: str,
    by_name: dict[str, list[uuid.UUID]],
    owners_of_name: dict[str, set[str]],
    versions_declaring: dict[str, set[uuid.UUID]],
    all_version_ids: list[uuid.UUID],
) -> bool:
    """Whether `data->>'name'` can be read without testing the row's version.

    Three conditions, and dropping any one of them reintroduces a silent
    cross-field leak:

    * the key only ever had this one name - otherwise older rows store it
      under a different key and would read NULL;
    * no *other* key ever had this name - otherwise this column would surface
      a different field's value from the versions that other key covers;
    * every version declares the name - otherwise a row from a version that
      didn't declare it could still carry the key (values are hand-editable on
      the review screen) and would leak into this column.

    The second condition is the one that makes the obvious optimisation
    ("only one historical name, so read it directly") unsound.
    """
    if len(by_name) != 1:
        return False
    name = next(iter(by_name))
    if owners_of_name.get(name, set()) != {key}:
        return False
    return versions_declaring.get(name, set()) == set(all_version_ids)


def name_at(spec: FieldSpec, version_id: uuid.UUID) -> str | None:
    """The JSON key holding this field's value for a row of `version_id`."""
    for name, version_ids in spec.branches:
        if version_id in version_ids:
            return name
    return None
