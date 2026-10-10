"""Unit tests for identifier history governance and equipment-bound JSON current."""
from __future__ import annotations

import pytest

from enterprise.gateway.db.dialect import exec_sql, fetchall, fetchone
from enterprise.gateway.equipment_identity import upsert_current_identity
from enterprise.gateway.query.v2_router import (
    _explicit_equipment_ids,
    _format_current_identity_block,
    _merge_identity_into_equipment_index,
)
from enterprise.gateway.sync.models import (
    ExtDocumentMap,
    is_equipment_bound_inline_json,
    promote_equipment_inline_json_current,
    promote_version_if_latest,
)


def test_is_equipment_bound_inline_json_conventions():
    base = dict(
        tenant_id="t",
        source_system="EAM",
        external_document_id="EQ-1",
        source_version_id="v1",
        event_id="e1",
        sha256="a" * 64,
        file_name="ledger.json",
        source_kind="INLINE_JSON",
        equipment_id="EQ-1",
        document_type="equipment_master",
    )
    assert is_equipment_bound_inline_json(ExtDocumentMap(**base)) is True
    assert is_equipment_bound_inline_json(
        ExtDocumentMap(**{**base, "external_document_id": "EQ-1:master"})
    ) is True
    assert is_equipment_bound_inline_json(
        ExtDocumentMap(**{**base, "external_document_id": "EQ-MASTER:EQ-1"})
    ) is True
    assert is_equipment_bound_inline_json(
        ExtDocumentMap(**{**base, "external_document_id": "OTHER", "source_kind": "INLINE_JSON"})
    ) is False
    assert is_equipment_bound_inline_json(
        ExtDocumentMap(**{**base, "source_kind": "FILE_SHARE"})
    ) is False


@pytest.mark.asyncio
async def test_promote_keeps_single_equipment_inline_json_current(gateway_db):
    async with gateway_db.transaction(write=True) as conn:
        await exec_sql(
            conn,
            """INSERT INTO ext_document_map
               (tenant_id, source_system, external_document_id, source_version_id,
                event_id, sha256, file_name, source_kind, equipment_id, document_type,
                sync_status, business_status, current_version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "tenant-a", "EAM", "EQ-1", "v1", "evt-old", "a" * 64,
                "old.json", "INLINE_JSON", "EQ-1", "equipment_master",
                "ready", "active", 1, "t0", "t0",
            ),
        )
        await exec_sql(
            conn,
            """INSERT INTO ext_document_map
               (tenant_id, source_system, external_document_id, source_version_id,
                event_id, sha256, file_name, source_kind, equipment_id, document_type,
                sync_status, business_status, current_version, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "tenant-a", "EAM", "EQ-1:master", "v2", "evt-new", "b" * 64,
                "new.json", "INLINE_JSON", "EQ-1", "equipment_master",
                "ready", "active", 0, "t1", "t1",
            ),
        )
        row = await fetchone(
            conn,
            """SELECT * FROM ext_document_map WHERE event_id=?""",
            ("evt-new",),
        )
        from enterprise.gateway.sync.models import _row_to_mapping
        doc = _row_to_mapping(row)
        assert await promote_version_if_latest(conn, doc) is True
        statuses = await fetchall(
            conn,
            """SELECT external_document_id, business_status, current_version
                 FROM ext_document_map
                WHERE tenant_id=? AND equipment_id=? AND document_type=?
                ORDER BY external_document_id""",
            ("tenant-a", "EQ-1", "equipment_master"),
        )
        by_id = {r["external_document_id"]: r for r in statuses}
        assert by_id["EQ-1"]["business_status"] == "superseded"
        assert by_id["EQ-1"]["current_version"] == 0
        assert by_id["EQ-1:master"]["business_status"] == "active"
        assert by_id["EQ-1:master"]["current_version"] == 1


def test_explicit_equipment_ids_uses_identity_resolutions():
    class _R:
        def __init__(self, equipment_id=None, ambiguous=False, ids=()):
            self.identity = type("I", (), {"equipment_id": equipment_id})() if equipment_id else None
            self.ambiguous = ambiguous
            self.matched_equipment_ids = ids

    docs = {
        "d1": ExtDocumentMap(
            tenant_id="t",
            source_system="EAM",
            external_document_id="DOC",
            source_version_id="v1",
            event_id="e",
            sha256="c" * 64,
            file_name="m.pdf",
            equipment_id="EQ-1001",
            fixed_asset_no="FA-NEW",
        )
    }
    # Historical FA-OLD not on document map, but identity resolution knows it.
    resolved, unresolved = _explicit_equipment_ids(
        "请查设备编号 FA-OLD 的保养资料",
        docs,
        identity_resolutions={"FA-OLD": _R(equipment_id="EQ-1001")},
    )
    assert unresolved is False
    assert resolved == ["EQ-1001"]

    resolved, unresolved = _explicit_equipment_ids(
        "请查设备编号 FA-SHARED 的资料",
        docs,
        identity_resolutions={"FA-SHARED": _R(ambiguous=True, ids=("EQ-1", "EQ-2"))},
    )
    assert unresolved is True
    assert resolved == []


def test_format_current_identity_block():
    identity = type(
        "I",
        (),
        {
            "equipment_id": "EQ-1",
            "fixed_asset_no": "FA-1",
            "asset_id": "A-1",
            "identity_version": 3,
        },
    )()
    text = _format_current_identity_block([identity])
    assert "equipment_id=EQ-1" in text
    assert "fixed_asset_no=FA-1" in text
    assert "identity_version=3" in text


def test_merge_identity_index_marks_ambiguous():
    index = {"EQ-1": {"EQ-1"}}
    class _R:
        def __init__(self, ambiguous=False, equipment_id=None):
            self.ambiguous = ambiguous
            self.identity = type("I", (), {"equipment_id": equipment_id})() if equipment_id else None
    merged, amb = _merge_identity_into_equipment_index(
        index, {"FA-X": _R(ambiguous=True)}
    )
    assert "FA-X" in amb
