"""Coverage for the NL-to-SQL catalog and result relabelling.

The label helpers are pure. `test_catalog_only_advertises_real_columns` needs
Postgres (it compares the catalog against `pg_attribute`) but never an LLM -
it is the direct regression test for the bug that motivated column keys.
"""

import uuid

import pytest
from sqlalchemy import text

from app.models import SchemaDef, SchemaVersion
from app.pipeline.views import regenerate_schema_view, view_name_for
from app.query.nl_to_sql import build_catalog, column_labels, generate_sql, relabel_rows
from app.schema_fields import assign_column_keys
from tests.conftest import requires_db


def f(name, type="string", description=""):
    return {"name": name, "type": type, "description": description, "required": False}


class TestColumnLabels:
    def test_maps_keys_to_human_names(self):
        catalog = [
            {
                "schema_name": "invoice",
                "view_name": "view_invoice",
                "columns": [
                    {"column": "f_11111111", "name": "Invoice Total", "type": "number", "description": ""}
                ],
            }
        ]
        assert column_labels(catalog) == {"f_11111111": "Invoice Total"}

    def test_colliding_labels_are_disambiguated(self):
        """Two fields can legitimately share a display name - one renamed, one
        new field reusing the old name. Rows are collected into a dict, so
        identical labels would collapse and lose a column's values."""
        catalog = [
            {
                "schema_name": "invoice",
                "view_name": "view_invoice",
                "columns": [
                    {"column": "f_11111111", "name": "Total", "type": "number", "description": ""},
                    {"column": "f_22222222", "name": "Total", "type": "integer", "description": ""},
                    {"column": "f_33333333", "name": "Total", "type": "string", "description": ""},
                ],
            }
        ]
        labels = column_labels(catalog)
        assert labels == {"f_11111111": "Total", "f_22222222": "Total (2)", "f_33333333": "Total (3)"}
        assert len(set(labels.values())) == 3


class TestRelabelRows:
    def test_keys_become_labels(self):
        rows = [{"f_11111111": "4200", "document_id": "d"}]
        assert relabel_rows(rows, {"f_11111111": "Invoice Total"}) == [
            {"Invoice Total": "4200", "document_id": "d"}
        ]

    def test_unknown_columns_pass_through(self):
        """An aggregate alias like `sum` has no label and must survive."""
        rows = [{"sum": "100", "count": 3}]
        assert relabel_rows(rows, {"f_11111111": "X"}) == [{"sum": "100", "count": 3}]

    def test_no_labels_is_a_passthrough(self):
        rows = [{"a": 1}]
        assert relabel_rows(rows, {}) is rows

    def test_a_value_shaped_like_a_key_is_not_relabelled(self):
        rows = [{"vendor": "f_deadbeef"}]
        assert relabel_rows(rows, {"f_deadbeef": "Nope"}) == [{"vendor": "f_deadbeef"}]

    def test_colliding_labels_do_not_collapse_columns(self):
        rows = [{"f_11111111": "a", "f_22222222": "b"}]
        labels = {"f_11111111": "Total", "f_22222222": "Total (2)"}
        assert relabel_rows(rows, labels) == [{"Total": "a", "Total (2)": "b"}]


class TestPromptBlock:
    def test_prompt_carries_the_key_the_label_and_the_description(self, monkeypatch):
        captured = {}

        def fake_llm_extract(*, instructions, text, json_schema, schema_name="x"):
            captured["instructions"] = instructions
            return {"sql": "SELECT 1"}

        monkeypatch.setattr("app.query.nl_to_sql.llm_extract", fake_llm_extract)
        catalog = [
            {
                "schema_name": "invoice",
                "view_name": "view_invoice",
                "columns": [
                    {
                        "column": "f_1a2b3c4d",
                        "name": "Invoice Total",
                        "type": "number",
                        "description": "total incl. tax",
                    }
                ],
            }
        ]
        generate_sql("how much?", catalog)
        prompt = captured["instructions"]
        assert 'f_1a2b3c4d (number) - "Invoice Total": total incl. tax' in prompt
        assert "Use the identifier verbatim" in prompt
        # bookkeeping columns stay available for filtering/joining
        assert "document_id (uuid)" in prompt
        # scoping is injected, never requested
        assert "WHERE clause" not in prompt


@requires_db
class TestCatalogAgainstTheRealViews:
    """The regression test for the bug that motivated the whole change."""

    @pytest.fixture
    def invoice_schema(self, db_session):
        name = f"catalog_test_{uuid.uuid4().hex[:8]}"
        schema = SchemaDef(name=name)
        db_session.add(schema)
        db_session.flush()
        fields = assign_column_keys(
            [
                f("Invoice Total", "number", "total incl. tax"),
                f("id"),  # collided with the view's own er.id before keys
                f("Vendor Name"),
            ]
        )
        db_session.add(
            SchemaVersion(schema_id=schema.id, version=1, fields=fields, is_breaking_from_prev=False)
        )
        db_session.flush()
        regenerate_schema_view(db_session, schema.id)
        db_session.commit()
        yield schema
        view = view_name_for(name)
        db_session.execute(text(f"DROP VIEW IF EXISTS {view}"))
        db_session.query(SchemaVersion).filter(SchemaVersion.schema_id == schema.id).delete()
        db_session.query(SchemaDef).filter(SchemaDef.id == schema.id).delete()
        db_session.commit()

    def test_catalog_only_advertises_real_columns(self, db_session, invoice_schema):
        """Every column the model is told about must exist on the view. This
        used to fail for any field name that wasn't already snake_case: the
        catalog said `Invoice Total`, the view had `invoice_total`, and the
        generated SQL died on UndefinedColumn and fell back to RAG."""
        catalog = build_catalog(db_session)
        entry = next(c for c in catalog if c["schema_id"] == invoice_schema.id)
        advertised = {col["column"] for col in entry["columns"]}

        actual = set(
            db_session.execute(
                text(
                    "SELECT attname FROM pg_attribute "
                    "WHERE attrelid = CAST(:v AS regclass) AND attnum > 0 AND NOT attisdropped"
                ),
                {"v": entry["view_name"]},
            ).scalars()
        )
        assert advertised <= actual, f"advertised but missing from the view: {advertised - actual}"

    def test_human_names_and_descriptions_survive(self, db_session, invoice_schema):
        catalog = build_catalog(db_session)
        entry = next(c for c in catalog if c["schema_id"] == invoice_schema.id)
        by_name = {col["name"]: col for col in entry["columns"]}
        assert by_name["Invoice Total"]["description"] == "total incl. tax"
        assert by_name["Invoice Total"]["type"] == "number"

    def test_a_field_named_id_gets_its_own_column(self, db_session, invoice_schema):
        """Collision class 1, end to end: CREATE VIEW succeeded above, and the
        bookkeeping id is still the record's real id."""
        catalog = build_catalog(db_session)
        entry = next(c for c in catalog if c["schema_id"] == invoice_schema.id)
        id_field = next(col for col in entry["columns"] if col["name"] == "id")
        assert id_field["column"] != "id"

        cols = set(
            db_session.execute(
                text(
                    "SELECT attname FROM pg_attribute "
                    "WHERE attrelid = CAST(:v AS regclass) AND attnum > 0 AND NOT attisdropped"
                ),
                {"v": entry["view_name"]},
            ).scalars()
        )
        assert "id" in cols and id_field["column"] in cols
