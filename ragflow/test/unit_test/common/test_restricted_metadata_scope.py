import pytest
from unittest.mock import AsyncMock, Mock, patch

from common.metadata_es_filter import build_meta_filter_query
from common.metadata_utils import apply_meta_data_filter


@pytest.mark.asyncio
@pytest.mark.parametrize("base_doc_ids", [None, [], ["-999"]])
async def test_restrict_empty_scope_is_fail_closed(base_doc_ids):
    result = await apply_meta_data_filter(
        None,
        base_doc_ids=base_doc_ids,
        doc_scope_mode="restrict",
    )

    assert result == []


@pytest.mark.asyncio
async def test_restrict_without_metadata_filter_preserves_valid_scope():
    result = await apply_meta_data_filter(
        None,
        base_doc_ids=["doc-1", "doc-2"],
        doc_scope_mode="restrict",
    )

    assert result == ["doc-1", "doc-2"]


@pytest.mark.asyncio
async def test_restrict_metadata_filter_intersects_eager_metadata():
    result = await apply_meta_data_filter(
        {
            "method": "manual",
            "manual": [{"key": "type", "op": "=", "value": "pump"}],
        },
        {"type": {"pump": ["doc-in", "doc-out"]}},
        base_doc_ids=["doc-in"],
        doc_scope_mode="restrict",
    )

    assert result == ["doc-in"]


@pytest.mark.asyncio
async def test_restrict_metadata_filter_loads_lazy_metadata_when_metas_is_none():
    loader = Mock(return_value={"type": {"pump": ["doc-in"]}})
    result = await apply_meta_data_filter(
        {
            "method": "manual",
            "manual": [{"key": "type", "op": "=", "value": "pump"}],
        },
        metas=None,
        base_doc_ids=["doc-in"],
        metas_loader=loader,
        doc_scope_mode="restrict",
    )

    assert result == ["doc-in"]
    loader.assert_called_once_with()


@pytest.mark.asyncio
async def test_restrict_empty_manual_filter_keeps_valid_scope():
    result = await apply_meta_data_filter(
        {"method": "manual", "manual": []},
        base_doc_ids=["doc-in"],
        doc_scope_mode="restrict",
    )

    assert result == ["doc-in"]


@pytest.mark.asyncio
async def test_restrict_pushdown_never_returns_ids_outside_scope():
    with patch(
        "api.db.services.doc_metadata_service.DocMetadataService.filter_doc_ids_by_meta_pushdown",
        return_value=["doc-out", "doc-in"],
    ):
        result = await apply_meta_data_filter(
            {
                "method": "manual",
                "manual": [{"key": "type", "op": "=", "value": "pump"}],
            },
            base_doc_ids=["doc-in"],
            kb_ids=["kb-1"],
            doc_scope_mode="restrict",
        )

    assert result == ["doc-in"]


@pytest.mark.asyncio
async def test_restrict_auto_zero_result_returns_empty_list():
    with (
        patch(
            "rag.prompts.generator.gen_meta_filter",
            new_callable=AsyncMock,
            return_value={
                "conditions": [{"key": "type", "op": "=", "value": "pump"}],
                "logic": "and",
            },
        ),
        patch(
            "api.db.services.doc_metadata_service.DocMetadataService.filter_doc_ids_by_meta_pushdown",
            return_value=[],
        ),
    ):
        result = await apply_meta_data_filter(
            {"method": "auto"},
            base_doc_ids=["doc-in"],
            kb_ids=["kb-1"],
            doc_scope_mode="restrict",
        )

    assert result == []


@pytest.mark.asyncio
async def test_restrict_manual_zero_result_returns_empty_list():
    with patch(
        "api.db.services.doc_metadata_service.DocMetadataService.filter_doc_ids_by_meta_pushdown",
        return_value=[],
    ):
        result = await apply_meta_data_filter(
            {
                "method": "manual",
                "manual": [{"key": "equipment_id", "op": "in", "value": "missing"}],
            },
            base_doc_ids=["doc-in"],
            kb_ids=["kb-1"],
            doc_scope_mode="restrict",
        )

    assert result == []


@pytest.mark.asyncio
async def test_restrict_auto_without_generated_conditions_keeps_scope():
    with patch(
        "rag.prompts.generator.gen_meta_filter",
        new_callable=AsyncMock,
        return_value={"conditions": [], "logic": "and"},
    ):
        result = await apply_meta_data_filter(
            {"method": "auto"},
            base_doc_ids=["doc-in"],
            kb_ids=["kb-1"],
            doc_scope_mode="restrict",
        )

    assert result == ["doc-in"]


@pytest.mark.asyncio
async def test_default_metadata_filter_keeps_legacy_union_behavior():
    result = await apply_meta_data_filter(
        {
            "method": "manual",
            "manual": [{"key": "type", "op": "=", "value": "pump"}],
        },
        {"type": {"pump": ["doc-other"]}},
        base_doc_ids=["doc-in"],
    )

    assert set(result) == {"doc-in", "doc-other"}


def test_es_metadata_pushdown_adds_scope_as_an_independent_filter():
    query = build_meta_filter_query(
        [{"key": "type", "op": "=", "value": "pump"}],
        "and",
        ["kb-1"],
        doc_ids=["doc-in"],
    )

    filters = query["query"]["bool"]["filter"]
    assert {"terms": {"kb_id": ["kb-1"]}} in filters
    assert {"terms": {"id": ["doc-in"]}} in filters
