"""One document, many rows (field `scope`).

Pure: no Postgres, no OpenAI key. The three pieces that decide how many
`extracted_records` rows a document becomes are all separable -
`_build_extraction_json_schema` and `_parse_extraction` are functions of a
field list, `_extract_fields`' chunked fallback needs only a stubbed
`llm_extract`, and `flatten_extraction`/`materialize_record_data` are pure -
so the storage semantics are pinned here without a database or an API key.
The confirm-time insert/delete behaviour that sits on top of them needs a
session and is covered by the db-backed tests.

The governing rule, restated because almost every assertion below is a
consequence of it: a schema declaring NO row-scoped field must still get the
same response schema and the same prompt shape, and must still confirm to
exactly one record per document. What changed is only where the document-level
values are denormalized onto the rows - `flatten_extraction` does it as the
staged payload is built, so `extracted_data` is a flat list of objects rather
than a dict plus a sibling row list.
"""

import uuid

import pytest

from app.pipeline.confirm import is_job_clean, materialize_record_data
from app.pipeline.extract import (
    Extraction,
    _build_extraction_json_schema,
    _extract_fields,
    _identity_values,
    _instructions_for,
    _parse_extraction,
    flatten_extraction,
)
from app.pipeline.views import render_view_sql, spec_fingerprint
from app.schema_fields import DOCUMENT_SCOPE, ROW_SCOPE, field_scope, field_specs, split_by_scope

K_NUMBER = "f_1a2b3c4d"
K_VENDOR = "f_9e8d7c6b"
K_DESC = "f_44556677"
K_AMOUNT = "f_aabbccdd"

V1 = uuid.UUID("a1000000-0000-0000-0000-000000000001")
SCHEMA = uuid.UUID("3f1c0000-0000-0000-0000-00000000000a")


def f(name, type="string", column=None, scope=DOCUMENT_SCOPE):
    return {
        "name": name,
        "type": type,
        "description": "",
        "required": False,
        "column": column,
        "scope": scope,
    }


# A header/lines schema: two document-level fields, two per-row fields.
INVOICE_FIELDS = [
    f("Invoice Number", "string", K_NUMBER),
    f("Vendor", "string", K_VENDOR),
    f("Line Description", "string", K_DESC, scope=ROW_SCOPE),
    f("Line Amount", "number", K_AMOUNT, scope=ROW_SCOPE),
]

# The same schema before anyone opted into rows.
FLAT_FIELDS = [f("Invoice Number", "string", K_NUMBER), f("Vendor", "string", K_VENDOR)]


class TestFieldScope:
    def test_missing_scope_reads_as_document(self):
        """Every field stored before migration 0006 has no `scope` key."""
        assert field_scope({"name": "Vendor"}) == DOCUMENT_SCOPE

    def test_unrecognized_scope_reads_as_document(self):
        """An ad hoc field's scope comes from an LLM and is not validated by
        Pydantic on the promote path, so junk must fall back to the safe
        (pre-existing, one-row) behaviour rather than to row scope."""
        assert field_scope({"name": "Vendor", "scope": "line"}) == DOCUMENT_SCOPE
        assert field_scope({"name": "Vendor", "scope": None}) == DOCUMENT_SCOPE

    def test_split_preserves_declaration_order(self):
        document, rows = split_by_scope(INVOICE_FIELDS)
        assert [x["name"] for x in document] == ["Invoice Number", "Vendor"]
        assert [x["name"] for x in rows] == ["Line Description", "Line Amount"]

    def test_field_specs_carry_scope(self):
        specs = {s.column: s.scope for s in field_specs([(V1, INVOICE_FIELDS)])}
        assert specs == {
            K_NUMBER: DOCUMENT_SCOPE,
            K_VENDOR: DOCUMENT_SCOPE,
            K_DESC: ROW_SCOPE,
            K_AMOUNT: ROW_SCOPE,
        }


class TestExtractionJsonSchema:
    def test_flat_schema_is_unchanged_by_this_feature(self):
        """The exact pre-scopes shape, asserted structurally rather than by
        'no rows key': a schema with no row fields must not even hint at rows,
        or its extraction quality changes for no reason."""
        schema = _build_extraction_json_schema(FLAT_FIELDS)
        assert schema["required"] == ["values", "confidence"]
        assert set(schema["properties"]) == {"values", "confidence"}
        assert list(schema["properties"]["values"]["properties"]) == ["Invoice Number", "Vendor"]

    def test_row_schema_separates_document_from_rows(self):
        schema = _build_extraction_json_schema(INVOICE_FIELDS)
        assert set(schema["properties"]) == {"document", "rows"}
        assert list(schema["properties"]["document"]["properties"]["values"]["properties"]) == [
            "Invoice Number",
            "Vendor",
        ]
        items = schema["properties"]["rows"]["items"]
        assert list(items["properties"]["values"]["properties"]) == [
            "Line Description",
            "Line Amount",
        ]

    def test_row_fields_are_not_offered_at_document_level(self):
        """The whole point of the split: the model is never asked whether
        'Line Amount' is a header value."""
        schema = _build_extraction_json_schema(INVOICE_FIELDS)
        document_names = schema["properties"]["document"]["properties"]["values"]["properties"]
        assert "Line Amount" not in document_names

    def test_flat_instructions_are_unchanged(self):
        assert _instructions_for(FLAT_FIELDS).startswith("Extract the following fields")

    def test_row_instructions_forbid_inventing_and_merging_rows(self):
        text = _instructions_for(INVOICE_FIELDS)
        assert "Do not invent rows" in text
        assert "do not merge two rows into one" in text


class TestParseExtraction:
    def test_flat_response_parses_to_one_set_of_values_and_no_rows(self):
        parsed = _parse_extraction(
            FLAT_FIELDS,
            {"values": {"Invoice Number": "INV-1", "Vendor": "Acme"}, "confidence": {"Vendor": 0.9}},
        )
        assert parsed.values == {"Invoice Number": "INV-1", "Vendor": "Acme"}
        assert parsed.rows == []

    def test_row_response_parses_header_and_rows_separately(self):
        parsed = _parse_extraction(
            INVOICE_FIELDS,
            {
                "document": {
                    "values": {"Invoice Number": "INV-1", "Vendor": "Acme"},
                    "confidence": {"Invoice Number": 0.97, "Vendor": 0.95},
                },
                "rows": [
                    {
                        "values": {"Line Description": "Consulting", "Line Amount": "1,250.00"},
                        "confidence": {"Line Amount": 0.91},
                    },
                    {
                        "values": {"Line Description": "Travel", "Line Amount": "300"},
                        "confidence": {"Line Amount": 0.88},
                    },
                ],
            },
        )
        assert parsed.values == {"Invoice Number": "INV-1", "Vendor": "Acme"}
        assert len(parsed.rows) == 2
        # Coerced against the ROW fields' declared types, not the document
        # ones - "Line Amount" is a number and has to lose its comma or the
        # numeric column reads NULL for that cell.
        assert parsed.rows[0]["Line Amount"] == "1250.00"
        assert parsed.rows[1]["Line Description"] == "Travel"
        assert parsed.row_confidence[0] == {"Line Amount": 0.91}

    def test_rows_stay_free_of_document_values(self):
        """The parser keeps the two halves apart; `flatten_extraction` is what
        merges them. The split has to survive this far because the chunked
        fallback merges the halves by opposite rules."""
        parsed = _parse_extraction(
            INVOICE_FIELDS,
            {
                "document": {"values": {"Vendor": "Acme"}, "confidence": {}},
                "rows": [{"values": {"Line Description": "Consulting"}, "confidence": {}}],
            },
        )
        assert "Vendor" not in parsed.rows[0]

    def test_empty_rows_list_is_not_an_error(self):
        parsed = _parse_extraction(
            INVOICE_FIELDS, {"document": {"values": {"Vendor": "Acme"}, "confidence": {}}, "rows": []}
        )
        assert parsed.rows == []
        assert parsed.values == {"Vendor": "Acme"}


class TestChunkedFallbackMerge:
    """The context-limit path, where the two halves merge by opposite rules."""

    @staticmethod
    def _run(monkeypatch, responses, fields):
        """Drives `_extract_fields` over the chunked branch with canned
        responses, one per chunk."""
        import app.pipeline.extract as extract

        class FakeChunk:
            def __init__(self, text):
                self.text = text

        monkeypatch.setattr(
            extract,
            "chunk_by_title",
            lambda elements, **kwargs: [FakeChunk(f"chunk-{i}") for i in range(len(responses))],
        )
        calls = iter(responses)
        monkeypatch.setattr(extract, "llm_extract", lambda **kwargs: next(calls))
        # Long enough to take the chunked branch rather than the one-shot one.
        return extract._extract_fields([], "x" * (extract._max_extract_chars() + 1), fields)

    def test_rows_concatenate_across_chunks(self, monkeypatch):
        """The regression this feature would otherwise have shipped with: the
        pre-existing merge reconciled field-by-field, which would collapse a
        table split over two chunks into one row holding the last line."""
        result = self._run(
            monkeypatch,
            [
                {
                    "document": {"values": {"Vendor": "Acme"}, "confidence": {"Vendor": 0.9}},
                    "rows": [{"values": {"Line Description": "A", "Line Amount": "1"}, "confidence": {}}],
                },
                {
                    "document": {"values": {"Vendor": "Acme"}, "confidence": {"Vendor": 0.9}},
                    "rows": [
                        {"values": {"Line Description": "B", "Line Amount": "2"}, "confidence": {}},
                        {"values": {"Line Description": "C", "Line Amount": "3"}, "confidence": {}},
                    ],
                },
            ],
            INVOICE_FIELDS,
        )
        assert [r["Line Description"] for r in result.rows] == ["A", "B", "C"]

    def test_all_null_rows_are_dropped(self, monkeypatch):
        """A prose-only chunk answering 'no line items here' must not add a
        blank record, or the row count grows with document length."""
        result = self._run(
            monkeypatch,
            [
                {
                    "document": {"values": {"Vendor": "Acme"}, "confidence": {}},
                    "rows": [{"values": {"Line Description": None, "Line Amount": None}, "confidence": {}}],
                },
                {
                    "document": {"values": {"Vendor": "Acme"}, "confidence": {}},
                    "rows": [{"values": {"Line Description": "A", "Line Amount": "1"}, "confidence": {}}],
                },
            ],
            INVOICE_FIELDS,
        )
        assert [r["Line Description"] for r in result.rows] == ["A"]

    def test_document_fields_still_reconcile_pessimistically(self, monkeypatch):
        """Unchanged behaviour for the document half: last non-null wins, and
        a disagreement keeps the lower confidence."""
        result = self._run(
            monkeypatch,
            [
                {
                    "document": {"values": {"Vendor": "Acme"}, "confidence": {"Vendor": 0.9}},
                    "rows": [],
                },
                {
                    "document": {"values": {"Vendor": "Acme Ltd"}, "confidence": {"Vendor": 0.4}},
                    "rows": [],
                },
            ],
            INVOICE_FIELDS,
        )
        assert result.values["Vendor"] == "Acme Ltd"
        assert result.confidence["Vendor"] == 0.4

    def test_flat_schema_chunked_merge_is_unchanged(self, monkeypatch):
        result = self._run(
            monkeypatch,
            [
                {"values": {"Vendor": "Acme", "Invoice Number": None}, "confidence": {"Vendor": 0.9}},
                {"values": {"Vendor": None, "Invoice Number": "INV-1"}, "confidence": {"Invoice Number": 0.8}},
            ],
            FLAT_FIELDS,
        )
        assert result.values == {"Vendor": "Acme", "Invoice Number": "INV-1"}
        assert result.rows == []


class TestFlattenExtraction:
    """The LLM response -> the staged table. This is where the document-level
    values are denormalized onto every object."""

    def test_no_row_fields_yields_exactly_one_object(self):
        data, confidence = flatten_extraction(
            Extraction(values={"Vendor": "Acme"}, confidence={"Vendor": 0.9}), FLAT_FIELDS
        )
        assert data == [{"Vendor": "Acme"}]
        assert confidence == [{"Vendor": 0.9}]

    def test_header_is_copied_onto_every_object(self):
        data, _ = flatten_extraction(
            Extraction(
                values={"Invoice Number": "INV-1", "Vendor": "Acme"},
                confidence={},
                rows=[
                    {"Line Description": "A", "Line Amount": "1"},
                    {"Line Description": "B", "Line Amount": "2"},
                ],
                row_confidence=[{}, {}],
            ),
            INVOICE_FIELDS,
        )
        assert len(data) == 2
        assert all(o["Invoice Number"] == "INV-1" and o["Vendor"] == "Acme" for o in data)
        assert [o["Line Description"] for o in data] == ["A", "B"]

    def test_a_document_with_no_line_items_still_produces_one_object(self):
        """'No rows found' must not become 'no object' - the document would
        vanish from the review table and from the view, surfacing only as a
        wrong SUM."""
        data, _ = flatten_extraction(Extraction(values={"Vendor": "Acme"}, confidence={}), INVOICE_FIELDS)
        assert data == [{"Vendor": "Acme"}]

    def test_all_null_values_still_produce_one_object(self):
        """The branch keys off `fields`, never off `values` - a malformed
        response with nothing in it must not collapse to no object at all."""
        data, confidence = flatten_extraction(Extraction(values={}, confidence={}), INVOICE_FIELDS)
        assert data == [{}]
        assert confidence == [{}]

    def test_an_empty_field_list_is_the_only_empty_result(self):
        assert flatten_extraction(Extraction(values={}, confidence={}), []) == ([], [])

    def test_a_row_value_wins_over_a_same_named_header_value(self):
        """Scopes are declared per field, so a name cannot be in both groups
        via the API - but the precedence is pinned rather than left to
        dict-merge order."""
        data, _ = flatten_extraction(
            Extraction(values={"Vendor": "Header"}, confidence={}, rows=[{"Vendor": "Row"}]),
            INVOICE_FIELDS,
        )
        assert data[0]["Vendor"] == "Row"

    def test_short_row_confidence_still_scores_every_object(self):
        """A model returning 5 rows and 4 confidence maps is a cosmetic
        mismatch. Failing the job over it would lose a good extraction, and a
        short confidence list would break the parallel-length contract."""
        data, confidence = flatten_extraction(
            Extraction(
                values={"Vendor": "Acme"},
                confidence={"Vendor": 0.9},
                rows=[{"Line Amount": "1"}, {"Line Amount": "2"}],
                row_confidence=[{"Line Amount": 0.7}],
            ),
            INVOICE_FIELDS,
        )
        assert len(confidence) == len(data) == 2
        assert confidence[0] == {"Vendor": 0.9, "Line Amount": 0.7}
        assert confidence[1] == {"Vendor": 0.9}

    def test_a_null_row_entry_degrades_to_the_header(self):
        data, _ = flatten_extraction(
            Extraction(values={"Vendor": "Acme"}, confidence={}, rows=[None]), INVOICE_FIELDS
        )
        assert data == [{"Vendor": "Acme"}]


class TestIdentityValues:
    """Identity fields are document-scoped, so they are denormalized
    identically onto every object and any object is representative."""

    def test_reads_the_value_off_the_first_object(self):
        data = [{"Vendor": "Acme", "Line Amount": "1"}, {"Vendor": "Acme", "Line Amount": "2"}]
        assert _identity_values(data, ["Vendor"]) == {"Vendor": "Acme"}

    def test_skips_a_null_and_reads_the_next_object(self):
        """A reviewer can clear one cell of a column the schema treats as
        per-document; the identity is still recoverable from its siblings."""
        data = [{"Vendor": None}, {"Vendor": "Acme"}]
        assert _identity_values(data, ["Vendor"]) == {"Vendor": "Acme"}

    def test_a_disagreement_keeps_the_first_and_warns(self, caplog):
        """Structurally impossible before flattening, so it is logged rather
        than enforced - enforcing it would mean teaching the flat payload
        about scopes again."""
        data = [{"Vendor": "Acme"}, {"Vendor": "Other"}]
        with caplog.at_level("WARNING"):
            assert _identity_values(data, ["Vendor"]) == {"Vendor": "Acme"}
        assert "disagree on identity field" in caplog.text

    def test_a_field_absent_everywhere_is_simply_missing(self):
        assert _identity_values([{"Vendor": "Acme"}], ["Invoice Number"]) == {}


class TestMaterializeRecordData:
    def test_each_object_becomes_one_record(self):
        data = materialize_record_data(
            INVOICE_FIELDS,
            [
                {"Invoice Number": "INV-1", "Vendor": "Acme", "Line Description": "A"},
                {"Invoice Number": "INV-1", "Vendor": "Acme", "Line Description": "B"},
            ],
        )
        assert [row["Line Description"] for row in data] == ["A", "B"]
        assert all(row["Vendor"] == "Acme" for row in data)

    def test_an_empty_table_still_produces_a_record(self):
        """A confirmed document must contribute at least one row to its view,
        and the replace dedup branch indexes `record_data[0]` outright."""
        assert materialize_record_data(INVOICE_FIELDS, []) == [{}]
        assert materialize_record_data(INVOICE_FIELDS, None) == [{}]

    def test_hand_edited_values_are_coerced_on_every_object(self):
        """A reviewer types '1,250.00' into a numeric cell; it has to be
        normalized on every record, not just validated once."""
        fields = [*INVOICE_FIELDS, f("Tax", "number", "f_deadbeef")]
        data = materialize_record_data(
            fields,
            [
                {"Vendor": "Acme", "Tax": "1,250.00", "Line Amount": "5"},
                {"Vendor": "Acme", "Tax": "1,250.00", "Line Amount": "6"},
            ],
        )
        assert [row["Tax"] for row in data] == ["1250.00", "1250.00"]

    def test_a_null_object_coerces_to_an_empty_record(self):
        assert materialize_record_data(INVOICE_FIELDS, [None]) == [{}]


class TestIsJobClean:
    class FakeJob:
        def __init__(self, result):
            self.result = result

    def test_low_confidence_in_a_later_object_flags_the_job(self):
        """The case bulk-confirm most needs: a clean header over a bad line.
        Scoring only object 0 would call this clean."""
        job = self.FakeJob(
            {
                "data_confidence": [
                    {"Vendor": 0.99, "Line Amount": 0.99},
                    {"Vendor": 0.99, "Line Amount": 0.12},
                ]
            }
        )
        assert is_job_clean(job, 0.8) is False

    def test_all_high_confidence_objects_are_clean(self):
        job = self.FakeJob({"data_confidence": [{"Vendor": 0.99, "Line Amount": 0.9}]})
        assert is_job_clean(job, 0.8) is True

    def test_missing_confidence_is_unchanged_behaviour(self):
        job = self.FakeJob({"extracted_data": [{"Vendor": "Acme"}]})
        assert is_job_clean(job, 0.8) is True

    def test_non_numeric_confidence_does_not_raise(self):
        """`job.result` is an untyped blob a client can PATCH; a string here
        used to be impossible and must not now fail a whole bulk confirm."""
        job = self.FakeJob({"data_confidence": [{"Vendor": "high"}, None]})
        assert is_job_clean(job, 0.8) is True


class TestViewShape:
    def test_row_index_is_projected(self):
        sql = render_view_sql("view_invoice", SCHEMA, field_specs([(V1, INVOICE_FIELDS)]))
        assert "er.row_index" in sql

    def test_scope_does_not_change_the_projection(self):
        """A row-scoped field is read out of `data` exactly like any other -
        the header is already denormalized onto it by confirm time. If this
        ever stops being true, the view generator has grown a second way to
        read a field and `_find_dedup_match` will disagree with it."""
        flat = [f("Line Amount", "number", K_AMOUNT)]
        scoped = [f("Line Amount", "number", K_AMOUNT, scope=ROW_SCOPE)]
        assert render_view_sql("view_invoice", SCHEMA, field_specs([(V1, flat)])) == render_view_sql(
            "view_invoice", SCHEMA, field_specs([(V1, scoped)])
        )

    def test_fingerprint_covers_the_base_columns(self, monkeypatch):
        """Without this, adding `row_index` to `_BASE_COLUMNS` changed every
        view's shape without changing any fingerprint, so
        `reconcile_schema_views` would have skipped every existing view and no
        view would ever have gained the column."""
        import app.pipeline.views as views

        specs = field_specs([(V1, FLAT_FIELDS)])
        before = spec_fingerprint("view_invoice", SCHEMA, specs)
        monkeypatch.setattr(views, "_BASE_COLUMNS", [*views._BASE_COLUMNS, "er.something_new"])
        assert spec_fingerprint("view_invoice", SCHEMA, specs) != before


class TestCatalogAdvertisesMultirow:
    """`build_catalog` needs a session, so the prompt-shaping half is tested
    against hand-built catalog entries - which is also what pins the contract
    between `build_catalog` and `generate_sql`."""

    def test_multirow_note_only_appears_for_multirow_views(self):
        from app.query.nl_to_sql import _MULTIROW_NOTE, VIEW_BASE_COLUMNS

        assert "row_index" in VIEW_BASE_COLUMNS
        assert "COUNT(DISTINCT document_id)" in _MULTIROW_NOTE

    @pytest.mark.parametrize("multirow", [True, False])
    def test_prompt_mentions_rows_only_when_true(self, multirow, monkeypatch):
        import app.query.nl_to_sql as nl

        seen = {}
        monkeypatch.setattr(
            nl,
            "llm_extract",
            lambda **kwargs: seen.update(kwargs) or {"sql": "SELECT 1"},
        )
        catalog = [
            {
                "schema_name": "invoice",
                "view_name": "view_invoice",
                "multirow": multirow,
                "columns": [
                    {
                        "column": K_AMOUNT,
                        "name": "Line Amount",
                        "type": "number",
                        "description": "",
                        "scope": ROW_SCOPE if multirow else DOCUMENT_SCOPE,
                    }
                ],
            }
        ]
        nl.generate_sql("what did we spend?", catalog)
        assert ("one document can contribute several rows" in seen["instructions"]) is multirow
        assert ("[row-level]" in seen["instructions"]) is multirow
