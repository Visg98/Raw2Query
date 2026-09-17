"""Coverage for column-key assignment and historical-name analysis.

Pure: no Postgres, no OpenAI key. These pin the properties that the three
collision classes and the two aliasing bugs depend on, so they are assertions
rather than comments.
"""

import uuid

import pytest

from app.schema_fields import (
    COLUMN_KEY_RE,
    RESERVED_COLUMNS,
    ColumnKeyError,
    assign_column_keys,
    field_specs,
    lineage_name_map,
    mint_column_key,
    name_at,
)

V1 = uuid.UUID("a1000000-0000-0000-0000-000000000001")
V2 = uuid.UUID("b2000000-0000-0000-0000-000000000002")
V3 = uuid.UUID("c3000000-0000-0000-0000-000000000003")


def f(name, type="string", column=None, description=""):
    out = {"name": name, "type": type, "description": description, "required": False}
    if column is not None:
        out["column"] = column
    return out


class TestKeyFormat:
    def test_minted_keys_match_the_stored_format(self):
        for _ in range(200):
            assert COLUMN_KEY_RE.fullmatch(mint_column_key())

    def test_key_is_ten_chars_and_cannot_be_truncated(self):
        """Collision class 2: Postgres truncates identifiers at 63 chars and
        `safe_ident` never did, so two long names collided after truncation.
        Asserted as a property so the guarantee can't rot."""
        assert len(mint_column_key()) == 10
        assert len(mint_column_key()) < 63

    def test_key_can_never_collide_with_a_bookkeeping_column(self):
        """Collision class 1: a field named `id` used to break CREATE VIEW."""
        for _ in range(200):
            assert mint_column_key() not in RESERVED_COLUMNS

    def test_minted_keys_are_distinct(self):
        assert len({mint_column_key() for _ in range(5000)}) == 5000

    def test_minting_avoids_taken_keys(self):
        taken = {"f_00000001", "f_00000002"}
        for _ in range(50):
            assert mint_column_key(taken) not in taken


class TestAssignOnANewLineage:
    def test_every_field_gets_a_key(self):
        out = assign_column_keys([f("Invoice Total"), f("Vendor")])
        assert all(COLUMN_KEY_RE.fullmatch(x["column"]) for x in out)
        assert out[0]["column"] != out[1]["column"]

    def test_supplied_keys_are_ignored_not_trusted(self):
        """A brand-new lineage has nothing to preserve, and the ad hoc promote
        path in confirm.py writes client-supplied JSONB verbatim."""
        out = assign_column_keys([f("A", column="f_deadbeef")])
        assert out[0]["column"] != "f_deadbeef"

    def test_other_field_keys_are_preserved(self):
        out = assign_column_keys([f("A", type="number", description="d")])
        assert out[0]["type"] == "number"
        assert out[0]["description"] == "d"
        assert out[0]["required"] is False


class TestAssignOnAnExistingLineage:
    def setup_method(self):
        self.k_total = "f_1a2b3c4d"
        self.k_vendor = "f_9e8d7c6b"
        self.lineage = [[f("Total", "number", self.k_total), f("Vendor", "string", self.k_vendor)]]

    def test_a_noop_edit_is_key_stable(self):
        out = assign_column_keys(
            [f("Total", "number", self.k_total), f("Vendor", "string", self.k_vendor)],
            lineage_versions=self.lineage,
        )
        assert [x["column"] for x in out] == [self.k_total, self.k_vendor]

    def test_rename_echoing_its_key_preserves_the_column(self):
        out = assign_column_keys(
            [f("Net", "number", self.k_total)], lineage_versions=self.lineage
        )
        assert out[0]["column"] == self.k_total

    def test_rename_without_echoing_its_key_mints(self):
        """Documented degradation: a client that drops the key gets a split
        column. Not corruption - it is exactly today's behaviour."""
        out = assign_column_keys([f("Net", "number")], lineage_versions=self.lineage)
        assert out[0]["column"] not in (self.k_total, self.k_vendor)

    def test_keyless_field_matching_a_known_name_reclaims_its_key(self):
        """Keeps pre-migration clients and JsonSchemaImporter working."""
        out = assign_column_keys([f("Total", "number")], lineage_versions=self.lineage)
        assert out[0]["column"] == self.k_total

    def test_rename_plus_a_new_field_reusing_the_old_name_get_distinct_keys(self):
        """The `used` guard. Without it both fields claim k_total and the view
        emits two columns with the same identifier."""
        out = assign_column_keys(
            [f("Net", "number", self.k_total), f("Total", "integer")],
            lineage_versions=self.lineage,
        )
        assert out[0]["column"] == self.k_total
        assert out[1]["column"] != self.k_total

    def test_a_key_dropped_in_an_earlier_version_can_be_reclaimed(self):
        lineage = [
            [f("Total", "number", self.k_total)],
            [],  # dropped
        ]
        out = assign_column_keys([f("Total", "number", self.k_total)], lineage_versions=lineage)
        assert out[0]["column"] == self.k_total


class TestAssignRejections:
    def setup_method(self):
        self.lineage = [[f("Total", "number", "f_1a2b3c4d")]]

    def test_malformed_key(self):
        with pytest.raises(ColumnKeyError, match="not a valid column key"):
            assign_column_keys([f("A", column="total; DROP TABLE x")], lineage_versions=self.lineage)

    def test_duplicate_key_within_the_request(self):
        with pytest.raises(ColumnKeyError, match="more than one field"):
            assign_column_keys(
                [f("A", column="f_1a2b3c4d"), f("B", column="f_1a2b3c4d")],
                lineage_versions=self.lineage,
            )

    def test_foreign_key_is_rejected_not_reminted(self):
        with pytest.raises(ColumnKeyError, match="does not belong to this schema"):
            assign_column_keys([f("A", column="f_deadbeef")], lineage_versions=self.lineage)

    def test_duplicate_field_name(self):
        with pytest.raises(ColumnKeyError, match="duplicate field name"):
            assign_column_keys([f("A"), f("A")], lineage_versions=self.lineage)

    def test_empty_name(self):
        with pytest.raises(ColumnKeyError, match="non-empty name"):
            assign_column_keys([f("   ")], lineage_versions=self.lineage)


class TestLineageNameMap:
    def test_accumulates_across_the_whole_lineage(self):
        """Rename-and-restore: resetting per version would mint a second key
        for v3's `Total` and produce two half-empty columns."""
        lineage = [
            [f("Total", "number", "f_11111111")],
            [f("Net", "number", "f_11111111")],
        ]
        name_to_key, known = lineage_name_map(lineage)
        assert name_to_key["Total"] == "f_11111111"
        assert name_to_key["Net"] == "f_11111111"
        assert known == {"f_11111111"}

    def test_ignores_malformed_stored_keys(self):
        name_to_key, known = lineage_name_map([[f("A", column="nope")]])
        assert name_to_key == {} and known == set()


class TestFieldSpecs:
    def test_single_version_uses_the_plain_form(self):
        specs = field_specs([(V1, [f("Total", "number", "f_11111111")])])
        assert len(specs) == 1
        assert specs[0].plain is True
        assert specs[0].name == "Total"

    def test_never_renamed_field_uses_the_plain_form_across_versions(self):
        fields = [f("Vendor", "string", "f_22222222")]
        specs = field_specs([(V1, fields), (V2, fields), (V3, fields)])
        assert specs[0].plain is True

    def test_renamed_field_is_not_plain(self):
        specs = field_specs(
            [(V1, [f("Total", "number", "f_11111111")]), (V2, [f("Net", "number", "f_11111111")])]
        )
        assert specs[0].plain is False
        assert specs[0].name == "Net", "newest declaration wins"
        assert specs[0].branches == [("Net", [V2]), ("Total", [V1])]

    def test_a_name_owned_by_two_keys_is_never_plain(self):
        """The condition that makes the obvious optimisation unsound. K2 has
        exactly one historical name, but a v1 row's `Total` belongs to K1, so
        a bare `data->>'Total'` for K2 would surface K1's value."""
        specs = field_specs(
            [
                (V1, [f("Total", "number", "f_11111111")]),
                (V2, [f("Net", "number", "f_11111111")]),
                (V3, [f("Net", "number", "f_11111111"), f("Total", "integer", "f_33333333")]),
            ]
        )
        by_key = {s.column: s for s in specs}
        assert by_key["f_33333333"].branches == [("Total", [V3])]
        assert by_key["f_33333333"].plain is False, "single name, but owned by two keys"
        assert by_key["f_11111111"].plain is False

    def test_a_field_dropped_in_a_later_version_is_not_plain(self):
        """A row could still carry the key - values are hand-editable - and it
        would leak into a column that did not exist at that version."""
        specs = field_specs([(V1, [f("Gone", "string", "f_44444444")]), (V2, [])])
        assert specs[0].plain is False

    def test_column_order_is_first_seen_and_stable(self):
        v1 = [f("A", "string", "f_aaaaaaaa"), f("B", "string", "f_bbbbbbbb")]
        v2 = [f("B", "string", "f_bbbbbbbb"), f("C", "string", "f_cccccccc"), f("A", "string", "f_aaaaaaaa")]
        specs = field_specs([(V1, v1), (V2, v2)])
        assert [s.column for s in specs] == ["f_aaaaaaaa", "f_bbbbbbbb", "f_cccccccc"]

    def test_retype_takes_the_latest_declaration(self):
        specs = field_specs(
            [(V1, [f("N", "number", "f_11111111")]), (V2, [f("N", "integer", "f_11111111")])]
        )
        assert specs[0].type == "integer"

    def test_missing_key_raises_rather_than_minting(self):
        with pytest.raises(ColumnKeyError, match="0005_field_column_keys"):
            field_specs([(V1, [f("Total", "number")])])

    def test_malformed_stored_key_raises_before_any_sql_is_built(self):
        with pytest.raises(ColumnKeyError):
            field_specs([(V1, [f("Total", "number", "f_nothex!")])])


class TestNameAt:
    def test_resolves_the_json_key_per_version(self):
        specs = field_specs(
            [(V1, [f("Total", "number", "f_11111111")]), (V2, [f("Net", "number", "f_11111111")])]
        )
        assert name_at(specs[0], V1) == "Total"
        assert name_at(specs[0], V2) == "Net"

    def test_returns_none_for_a_version_that_did_not_declare_the_field(self):
        specs = field_specs([(V1, [f("Gone", "string", "f_44444444")]), (V2, [])])
        assert name_at(specs[0], V2) is None
