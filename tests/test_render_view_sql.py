"""Golden tests for generated view SQL (app/pipeline/views.py).

Pure: no Postgres, no OpenAI key. `render_view_sql` takes specs and returns
text, so the projection rules - which are the subtle part of the column-key
change - are pinned as exact strings.
"""

import uuid

import pytest

from app.pipeline.views import (
    render_view_sql,
    spec_fingerprint,
    view_name_for,
)
from app.schema_fields import ColumnKeyError, FieldSpec, field_specs

SCHEMA = uuid.UUID("3f1c0000-0000-0000-0000-00000000000a")
V1 = uuid.UUID("a1000000-0000-0000-0000-000000000001")
V2 = uuid.UUID("b2000000-0000-0000-0000-000000000002")
V3 = uuid.UUID("c3000000-0000-0000-0000-000000000003")

K_TOTAL = "f_1a2b3c4d"
K_VENDOR = "f_9e8d7c6b"
K_NEW = "f_44556677"


def f(name, type="string", column=None):
    return {"name": name, "type": type, "description": "", "required": False, "column": column}


def render(versions):
    specs = field_specs(versions)
    return render_view_sql("view_invoice", SCHEMA, specs)


class TestBaseShape:
    def test_bookkeeping_columns_and_topic_join(self):
        sql = render([(V1, [f("Vendor", "string", K_VENDOR)])])
        assert (
            "SELECT er.id, er.document_id, er.row_index, er.schema_version_id, "
            "er.is_duplicate_of, er.created_at" in sql
        )
        assert "COALESCE(dt.topic_ids, '{}'::uuid[]) AS topic_ids" in sql
        assert "LEFT JOIN (SELECT document_id, array_agg(DISTINCT topic_id) AS topic_ids" in sql
        assert f"WHERE er.schema_id = '{SCHEMA}'" in sql

    def test_left_join_not_inner(self):
        """An inner join would drop records for topic-less documents, changing
        the row count for every existing consumer."""
        sql = render([(V1, [f("Vendor", "string", K_VENDOR)])])
        assert "LEFT JOIN" in sql
        assert "INNER JOIN" not in sql


class TestPlainForm:
    def test_single_version_field_reads_directly(self):
        sql = render([(V1, [f("Vendor", "string", K_VENDOR)])])
        assert f"er.data->>'Vendor' AS {K_VENDOR}" in sql
        assert "CASE" not in sql

    def test_typed_field_is_wrapped_in_a_safe_cast(self):
        sql = render([(V1, [f("Total", "number", K_TOTAL)])])
        assert f"safe_numeric(er.data->>'Total') AS {K_TOTAL}" in sql

    @pytest.mark.parametrize(
        "ftype,fn",
        [
            ("number", "safe_numeric"),
            ("integer", "safe_integer"),
            ("boolean", "safe_boolean"),
            ("date", "safe_date"),
            ("datetime", "safe_timestamptz"),
        ],
    )
    def test_every_type_gets_its_helper(self, ftype, fn):
        sql = render([(V1, [f("X", ftype, K_TOTAL)])])
        assert f"{fn}(er.data->>'X') AS {K_TOTAL}" in sql

    def test_string_needs_no_cast(self):
        sql = render([(V1, [f("X", "string", K_TOTAL)])])
        assert f"er.data->>'X' AS {K_TOTAL}" in sql
        assert "safe_" not in sql

    def test_unchanged_field_stays_plain_across_versions(self):
        fields = [f("Vendor", "string", K_VENDOR)]
        sql = render([(V1, fields), (V2, fields), (V3, fields)])
        assert f"er.data->>'Vendor' AS {K_VENDOR}" in sql
        assert "CASE" not in sql


class TestRenameLookup:
    """The construction the whole change turns on."""

    def lineage(self):
        return [
            (V1, [f("Total", "number", K_TOTAL), f("Vendor", "string", K_VENDOR)]),
            (V2, [f("Net", "number", K_TOTAL), f("Vendor", "string", K_VENDOR)]),
            (
                V3,
                [
                    f("Net", "number", K_TOTAL),
                    f("Vendor", "string", K_VENDOR),
                    f("Total", "integer", K_NEW),  # new field reusing the old name
                ],
            ),
        ]

    def test_renamed_field_is_scoped_by_version_newest_branch_first(self):
        sql = render(self.lineage())
        assert (
            "safe_numeric(CASE\n"
            f"           WHEN er.schema_version_id IN ('{V2}', '{V3}') THEN er.data->>'Net'\n"
            f"           WHEN er.schema_version_id IN ('{V1}') THEN er.data->>'Total'\n"
            f"       END) AS {K_TOTAL}"
        ) in sql

    def test_the_reused_name_gets_its_own_scoped_column(self):
        sql = render(self.lineage())
        assert (
            "safe_integer(CASE\n"
            f"           WHEN er.schema_version_id IN ('{V3}') THEN er.data->>'Total'\n"
            f"       END) AS {K_NEW}"
        ) in sql

    def test_no_coalesce_anywhere(self):
        """COALESCE(data->>'Net', data->>'Total') leaks the reused name's value
        into the renamed field's column, because extraction writes absent
        fields as explicit JSON nulls and `->>` cannot tell those from a
        missing key. Verified against Postgres."""
        sql = render(self.lineage())
        assert "COALESCE(er.data" not in sql
        assert "COALESCE(data" not in sql

    def test_the_reused_name_is_never_read_plainly(self):
        """The bijection condition: K_NEW has exactly one historical name, but
        a v1 row's 'Total' belongs to K_TOTAL, so a bare read would surface
        the wrong field's value."""
        sql = render(self.lineage())
        assert f"er.data->>'Total' AS {K_NEW}" not in sql

    def test_untouched_field_in_the_same_lineage_stays_plain(self):
        sql = render(self.lineage())
        assert f"er.data->>'Vendor' AS {K_VENDOR}" in sql

    def test_dropped_field_is_scoped_not_plain(self):
        sql = render([(V1, [f("Gone", "string", "f_55555555")]), (V2, [])])
        assert "CASE" in sql
        assert "er.data->>'Gone' AS f_55555555" not in sql


class TestCollisionClasses:
    def test_field_named_id_does_not_collide_with_the_bookkeeping_column(self):
        """Collision class 1: this used to emit a duplicate `id` output column
        and make CREATE VIEW fail outright."""
        sql = render([(V1, [f("id", "string", K_TOTAL)])])
        assert "er.id," in sql
        assert f"er.data->>'id' AS {K_TOTAL}" in sql
        assert sql.count(" AS id") == 0

    @pytest.mark.parametrize(
        "name", ["document_id", "created_at", "schema_version_id", "is_duplicate_of", "topic_ids"]
    )
    def test_no_bookkeeping_name_can_be_shadowed(self, name):
        sql = render([(V1, [f(name, "string", K_TOTAL)])])
        assert f" AS {name}" not in sql.replace("AS topic_ids", "")

    def test_long_names_sharing_a_63_char_prefix_get_distinct_short_columns(self):
        """Collision class 2: safe_ident never truncated, but Postgres does at
        63 chars, so these two used to collide after truncation."""
        a = "Total amount payable including all applicable taxes and surcharges for period A"
        b = "Total amount payable including all applicable taxes and surcharges for period B"
        sql = render([(V1, [f(a, "number", K_TOTAL), f(b, "number", K_VENDOR)])])
        assert f"AS {K_TOTAL}" in sql and f"AS {K_VENDOR}" in sql
        assert len(K_TOTAL) < 63


class TestSafety:
    def test_a_quote_in_a_field_name_is_escaped(self):
        sql = render([(V1, [f("Client's Ref", "string", K_TOTAL)])])
        assert "er.data->>'Client''s Ref'" in sql

    def test_invalid_stored_key_raises_before_sql_is_built(self):
        """The key becomes a SQL identifier, and confirm.py writes proposed
        fields into JSONB without passing through Pydantic."""
        bad = FieldSpec(
            column="total; DROP TABLE documents",
            name="Total",
            type="string",
            description="",
            branches=[("Total", [V1])],
            plain=True,
        )
        with pytest.raises(ColumnKeyError, match="invalid column key"):
            render_view_sql("view_invoice", SCHEMA, [bad])

    def test_missing_key_raises_naming_the_migration(self):
        with pytest.raises(ColumnKeyError, match="0005_field_column_keys"):
            render([(V1, [{"name": "Total", "type": "number"}])])


class TestDeterminism:
    def test_column_order_is_stable_regardless_of_input_order(self):
        a, b = f("A", "string", "f_aaaaaaaa"), f("B", "string", "f_bbbbbbbb")
        assert render([(V1, [a, b]), (V2, [a, b])]) == render([(V1, [a, b]), (V2, [b, a])])

    def test_fingerprint_changes_when_a_field_is_renamed(self):
        """Why the fingerprint exists: the column name and type are unchanged
        by a rename, so a column-set probe sees no drift at all."""
        before = field_specs([(V1, [f("Total", "number", K_TOTAL)])])
        after = field_specs(
            [(V1, [f("Total", "number", K_TOTAL)]), (V2, [f("Net", "number", K_TOTAL)])]
        )
        assert [s.column for s in before] == [s.column for s in after]
        assert [s.type for s in before] == [s.type for s in after]
        assert spec_fingerprint("view_invoice", SCHEMA, before) != spec_fingerprint(
            "view_invoice", SCHEMA, after
        )

    def test_fingerprint_is_stable_for_an_unchanged_spec(self):
        specs = field_specs([(V1, [f("Total", "number", K_TOTAL)])])
        assert spec_fingerprint("view_invoice", SCHEMA, specs) == spec_fingerprint(
            "view_invoice", SCHEMA, specs
        )


class TestViewName:
    def test_view_name_still_derives_from_the_schema_name(self):
        assert view_name_for("Vendor Invoices") == "view_vendor_invoices"
