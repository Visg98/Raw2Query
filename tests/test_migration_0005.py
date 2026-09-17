"""Coverage for migration 0005's backfill fold.

Pure: the fold is module-level and side-effect free, so it is tested without
a database and without driving alembic. `migrations/versions/` has no
`__init__.py`, so the revision is imported by path.
"""

import importlib.util
import pathlib
import uuid

import pytest

from app.schema_fields import COLUMN_KEY_RE

_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "0005_field_column_keys.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("migration_0005", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m0005 = _load()

V1, V2, V3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def f(name, type="string", column=None):
    out = {"name": name, "type": type, "description": "", "required": False}
    if column:
        out["column"] = column
    return out


class TestRegexIsInStepWithTheApp:
    def test_duplicated_pattern_matches(self):
        """The migration deliberately does not import app code, so this is the
        thing that keeps the two definitions honest."""
        assert m0005.KEY_RE.pattern == COLUMN_KEY_RE.pattern

    def test_minted_keys_satisfy_the_app_regex(self):
        for _ in range(200):
            assert COLUMN_KEY_RE.fullmatch(m0005._mint(set()))


class TestFold:
    def test_assigns_a_key_to_every_field(self):
        updates = m0005.plan_lineage([(V1, [f("Total"), f("Vendor")], None)])
        fields, _ = updates[V1]
        assert all(COLUMN_KEY_RE.fullmatch(x["column"]) for x in fields)
        assert fields[0]["column"] != fields[1]["column"]

    def test_carries_a_key_forward_within_a_lineage(self):
        updates = m0005.plan_lineage(
            [(V1, [f("Total", "number")], None), (V2, [f("Total", "number"), f("Vendor")], None)]
        )
        v1_total = updates[V1][0][0]["column"]
        v2_total = next(x for x in updates[V2][0] if x["name"] == "Total")["column"]
        assert v1_total == v2_total

    def test_accumulates_across_the_whole_lineage_not_per_version(self):
        """`Total` -> `Net` -> `Total`: the third version's `Total` must reclaim
        the original key, or the view gets two half-empty columns."""
        updates = m0005.plan_lineage(
            [
                (V1, [f("Total", "number")], None),
                (V2, [f("Net", "number")], None),
                (V3, [f("Total", "number")], None),
            ]
        )
        assert updates[V1][0][0]["column"] == updates[V3][0][0]["column"]
        assert updates[V2][0][0]["column"] != updates[V1][0][0]["column"]

    def test_field_order_is_preserved(self):
        updates = m0005.plan_lineage([(V1, [f("C"), f("A"), f("B")], None)])
        assert [x["name"] for x in updates[V1][0]] == ["C", "A", "B"]

    def test_other_field_keys_survive(self):
        updates = m0005.plan_lineage(
            [(V1, [{"name": "T", "type": "number", "description": "d", "required": True}], None)]
        )
        field = updates[V1][0][0]
        assert field["description"] == "d" and field["required"] is True

    def test_empty_field_list_needs_no_update(self):
        assert m0005.plan_lineage([(V1, [], None)]) == {}

    def test_null_field_list_needs_no_update(self):
        assert m0005.plan_lineage([(V1, None, None)]) == {}


class TestIdempotence:
    def test_a_second_run_changes_nothing(self):
        first = m0005.plan_lineage([(V1, [f("Total", "number")], None)])
        migrated = first[V1][0]
        assert m0005.plan_lineage([(V1, migrated, None)]) == {}

    def test_an_already_keyed_field_keeps_its_key(self):
        updates = m0005.plan_lineage([(V1, [f("Total", "number", "f_1a2b3c4d")], None)])
        assert updates == {}

    def test_a_mix_of_keyed_and_keyless_only_mints_the_missing_one(self):
        updates = m0005.plan_lineage([(V1, [f("A", column="f_1a2b3c4d"), f("B")], None)])
        fields = updates[V1][0]
        assert fields[0]["column"] == "f_1a2b3c4d"
        assert fields[1]["column"] != "f_1a2b3c4d"

    def test_a_malformed_stored_key_is_replaced(self):
        updates = m0005.plan_lineage([(V1, [f("A", column="not-a-key")], None)])
        assert COLUMN_KEY_RE.fullmatch(updates[V1][0][0]["column"])


class TestIdentityFields:
    def test_names_become_keys(self):
        updates = m0005.plan_lineage([(V1, [f("Invoice No"), f("Vendor")], ["Invoice No"])])
        fields, identity = updates[V1]
        expected = next(x for x in fields if x["name"] == "Invoice No")["column"]
        assert identity == [expected]

    def test_an_orphaned_identity_name_is_dropped(self):
        """It was already a dead no-op: `_find_dedup_match` looked it up in
        `data` and found nothing."""
        updates = m0005.plan_lineage([(V1, [f("Vendor")], ["Gone"])])
        assert updates[V1][1] == []

    def test_already_migrated_identity_keys_pass_through(self):
        updates = m0005.plan_lineage(
            [(V1, [f("A", column="f_1a2b3c4d"), f("B")], ["f_1a2b3c4d"])]
        )
        assert updates[V1][1] == ["f_1a2b3c4d"]

    def test_no_identity_fields_stays_none(self):
        updates = m0005.plan_lineage([(V1, [f("A")], None)])
        assert updates[V1][1] is None

    def test_multiple_identity_fields_keep_their_order(self):
        updates = m0005.plan_lineage([(V1, [f("A"), f("B")], ["B", "A"])])
        fields, identity = updates[V1]
        by_name = {x["name"]: x["column"] for x in fields}
        assert identity == [by_name["B"], by_name["A"]]


class TestKeysAreDistinctAcrossLineages:
    def test_two_lineages_with_the_same_field_names_get_different_keys(self):
        a = m0005.plan_lineage([(V1, [f("Total")], None)])[V1][0][0]["column"]
        b = m0005.plan_lineage([(V2, [f("Total")], None)])[V2][0][0]["column"]
        assert a != b


@pytest.mark.parametrize("bad", ["", "f_", "F_1A2B3C4D", "f_1a2b3c4", "f_1a2b3c4dd", "f_zzzzzzzz"])
def test_regex_rejects_near_misses(bad):
    assert not m0005.KEY_RE.fullmatch(bad)
