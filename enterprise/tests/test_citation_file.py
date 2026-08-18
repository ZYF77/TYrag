"""Hashed citation file tickets: issue, consume, and public projection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from enterprise.gateway.auth.user_principal import UserPrincipal
from enterprise.gateway.query.citation_file import (
    CitationFileError,
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
    }
    item.update(overrides)
    return item


@pytest.mark.asyncio
async def test_public_citation_hides_internal_fields_and_adds_download(isolated_gateway_db):
    db, _ = isolated_gateway_db
    ticket = await issue_citation_file_ticket(
        db, citation=_citation(), principal=_principal()
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
    assert "imageId" not in public
    assert "chunkId" not in public
    assert "documentId" not in public
    assert "ragflowDocumentId" not in public
    assert ticket.kind == "crop"
    async with db.execute(
        "SELECT ticket_hash FROM ext_citation_file_ticket"
    ) as cursor:
        row = await cursor.fetchone()
    assert row["ticket_hash"] != ticket.token


@pytest.mark.asyncio
async def test_claim_rejects_expired_wrong_and_overused_tickets(
    isolated_gateway_db,
):
    db, _ = isolated_gateway_db
    citation = _citation(imageId=None)
    ticket = await issue_citation_file_ticket(
        db, citation=citation, principal=_principal()
    )
    assert ticket.kind == "original"

    claimed = await claim_citation_file_ticket(db, "cite-1", ticket.token)
    assert claimed["citation_id"] == "cite-1"
    assert claimed["kind"] == "original"

    with pytest.raises(CitationFileError) as wrong:
        await claim_citation_file_ticket(db, "other-cite", ticket.token)
    assert wrong.value.status_code == 404

    expired = await issue_citation_file_ticket(
        db, citation=_citation(citationId="cite-exp"), principal=_principal()
    )
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    await db.execute(
        "UPDATE ext_citation_file_ticket SET expires_at=? WHERE citation_id=?",
        (past, "cite-exp"),
    )
    await db.commit()
    with pytest.raises(CitationFileError) as expired_exc:
        await claim_citation_file_ticket(db, "cite-exp", expired.token)
    assert expired_exc.value.status_code == 404

    limited = await issue_citation_file_ticket(
        db, citation=_citation(citationId="cite-limit"), principal=_principal()
    )
    await db.execute(
        "UPDATE ext_citation_file_ticket SET max_downloads=1, download_count=1 WHERE citation_id=?",
        ("cite-limit",),
    )
    await db.commit()
    with pytest.raises(CitationFileError) as limited_exc:
        await claim_citation_file_ticket(db, "cite-limit", limited.token)
    assert limited_exc.value.status_code == 404
