"""Hashed citation file tickets: issue, consume, and public projection."""

from __future__ import annotations

from enterprise.gateway.db.dialect import exec_sql, fetchone

from enterprise.gateway.db.ops import gw_read, gw_write

from datetime import datetime, timedelta, timezone

import pytest

from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.query.citation_file import (
    CitationFileError,
    CitationFileTicket,
    claim_citation_file_ticket,
    issue_citation_file_ticket,
    public_citation,
)


def _principal() -> UserPrincipal:
    return UserPrincipal(
        tenant_id="customer-a",
        business_user_id="biz-user-001",
        subject="biz-user-001",
        department_ids=("d10",),
        role_codes=("end_user",),
        group_ids=("maintenance",),
        security_level=2,
        mapping_status="active",
        capabilities=("ask", "view_citations", "list_sessions"),
    )


def _citation(**overrides) -> dict:
    item = {
        "citationId": "cite-1",
        "sourceType": "document",
        "title": "repair.pdf",
        "documentId": "EXT-DOC-1",
        "ragflowDocumentId": "doc-1",
        "chunkId": "chunk-1",
        "versionId": "v1",
        "assetId": "FA-1",
        "pageNo": 2,
        "bbox": None,
        "excerpt": "leak repair",
        "imageId": "ds-1-page-1.png",
        "positions": [],
        "refIndex": 2,
    }
    item.update(overrides)
    return item


@pytest.mark.asyncio
async def test_public_citation_hides_internal_fields_and_adds_download(isolated_gateway_db):
    db, _ = isolated_gateway_db
    ticket = await gw_write(db, issue_citation_file_ticket, citation=_citation(), principal=_principal()
    )
    public = public_citation(
        _citation(),
        ticket,
        download_url=f"http://test/enterprise/api/v2/citations/cite-1/file/{ticket.token}",
    )

    assert public["downloadUrl"].endswith(ticket.token)
    assert public["downloadExpiresAt"] == ticket.expires_at
    assert public["externalDocumentId"] == "EXT-DOC-1"
    assert public["sourceVersionId"] == "v1"
    assert public["refIndex"] == 2
    assert public["fileKind"] == "crop"
    assert "imageId" not in public
    assert "chunkId" not in public
    assert "documentId" not in public
    assert "ragflowDocumentId" not in public
    assert ticket.kind == "crop"
    row = await gw_read(db, fetchone, "SELECT ticket_hash FROM ext_citation_file_ticket")
    assert row["ticket_hash"] != ticket.token


def test_public_citation_includes_ref_index_without_db():
    public = public_citation(
        _citation(refIndex=5),
        CitationFileTicket(
            token="ticket-token",
            expires_at="2026-08-20T12:00:00+00:00",
            kind="crop",
        ),
        download_url="http://test/enterprise/api/v2/citations/cite-1/file/ticket-token",
    )
    assert public["refIndex"] == 5
    assert public.get("refIndex") is not None


@pytest.mark.asyncio
async def test_claim_rejects_expired_wrong_and_overused_tickets(
    isolated_gateway_db,
):
    db, _ = isolated_gateway_db
    citation = _citation(imageId=None)
    ticket = await gw_write(db, issue_citation_file_ticket, citation=citation, principal=_principal()
    )
    assert ticket.kind == "original"

    claimed = await gw_write(db, claim_citation_file_ticket, "cite-1", ticket.token)
    assert claimed["citation_id"] == "cite-1"
    assert claimed["kind"] == "original"

    with pytest.raises(CitationFileError) as wrong:
        await gw_write(db, claim_citation_file_ticket, "other-cite", ticket.token)
    assert wrong.value.status_code == 404

    expired = await gw_write(db, issue_citation_file_ticket, citation=_citation(citationId="cite-exp"), principal=_principal()
    )
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    await gw_write(db, exec_sql, "UPDATE ext_citation_file_ticket SET expires_at=? WHERE citation_id=?", (past, "cite-exp"),
    )
    with pytest.raises(CitationFileError) as expired_exc:
        await gw_write(db, claim_citation_file_ticket, "cite-exp", expired.token)
    assert expired_exc.value.status_code == 404

    limited = await gw_write(db, issue_citation_file_ticket, citation=_citation(citationId="cite-limit"), principal=_principal())
    await gw_write(db, exec_sql, "UPDATE ext_citation_file_ticket SET max_downloads=1, download_count=1 WHERE citation_id=?",
        ("cite-limit",),
    )
    with pytest.raises(CitationFileError) as limited_exc:
        await gw_write(db, claim_citation_file_ticket, "cite-limit", limited.token)
    assert limited_exc.value.status_code == 404
