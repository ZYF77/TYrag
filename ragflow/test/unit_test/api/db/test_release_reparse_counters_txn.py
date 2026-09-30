"""Regression: release_reparse_counters must not close DB while a txn is open.

Root cause (production code=100): release_reparse_counters used DB.atomic() and
then called DocumentService.increment_chunk_num, whose @DB.connection_context()
closes the connection on exit while the outer atomic transaction is still open.
peewee then raises OperationalError("Attempting to close database while
transaction is open").

Fix contract: outer @connection_context + atomic; ledger mutation via a helper
that does NOT open a nested connection_context.
"""
from __future__ import annotations

import pytest
from peewee import CharField, FloatField, IntegerField, Model, OperationalError, SqliteDatabase


def _make_models(db: SqliteDatabase):
    class Document(Model):
        id = CharField(primary_key=True)
        kb_id = CharField()
        token_num = IntegerField(default=0)
        chunk_num = IntegerField(default=0)
        process_duration = FloatField(default=0)

        class Meta:
            database = db
            table_name = "document"

    class Knowledgebase(Model):
        id = CharField(primary_key=True)
        token_num = IntegerField(default=0)
        chunk_num = IntegerField(default=0)

        class Meta:
            database = db
            table_name = "knowledgebase"

    return Document, Knowledgebase


def test_nested_connection_context_inside_atomic_raises():
    """Reproduce the production failure mode."""
    db = SqliteDatabase("/tmp/rf_release_reparse_broken.db")
    Document, Knowledgebase = _make_models(db)
    if not db.is_closed():
        db.close()
    db.connect()
    db.execute_sql("DROP TABLE IF EXISTS document")
    db.execute_sql("DROP TABLE IF EXISTS knowledgebase")
    db.create_tables([Document, Knowledgebase])
    Document.create(id="d1", kb_id="k1", token_num=10, chunk_num=2, process_duration=1.5)
    Knowledgebase.create(id="k1", token_num=10, chunk_num=2)

    @db.connection_context()
    def increment_chunk_num(doc_id, kb_id, token_num, chunk_num, duration):
        with db.atomic():
            Document.update(
                token_num=Document.token_num + token_num,
                chunk_num=Document.chunk_num + chunk_num,
                process_duration=Document.process_duration + duration,
            ).where((Document.id == doc_id) & (Document.kb_id == kb_id)).execute()
            Knowledgebase.update(
                token_num=Knowledgebase.token_num + token_num,
                chunk_num=Knowledgebase.chunk_num + chunk_num,
            ).where(Knowledgebase.id == kb_id).execute()

    def release_broken(doc_id):
        # Pre-fix: already connected, outer atomic open.
        with db.atomic():
            fresh = Document.get_by_id(doc_id)
            increment_chunk_num(
                fresh.id, fresh.kb_id, -fresh.token_num, -fresh.chunk_num, -fresh.process_duration
            )

    with pytest.raises(OperationalError, match="close database while transaction is open"):
        release_broken("d1")


def test_fixed_pattern_commits_then_closes_cleanly():
    """Mature peewee usage: connection_context wraps atomic; delta has no close."""
    db = SqliteDatabase("/tmp/rf_release_reparse_fixed.db")
    Document, Knowledgebase = _make_models(db)
    if not db.is_closed():
        db.close()
    db.connect()
    db.execute_sql("DROP TABLE IF EXISTS document")
    db.execute_sql("DROP TABLE IF EXISTS knowledgebase")
    db.create_tables([Document, Knowledgebase])
    Document.create(id="d1", kb_id="k1", token_num=10, chunk_num=2, process_duration=1.5)
    Knowledgebase.create(id="k1", token_num=100, chunk_num=20)

    def apply_chunk_num_delta(doc_id, kb_id, token_num, chunk_num, duration):
        Document.update(
            token_num=Document.token_num + token_num,
            chunk_num=Document.chunk_num + chunk_num,
            process_duration=Document.process_duration + duration,
        ).where((Document.id == doc_id) & (Document.kb_id == kb_id)).execute()
        Knowledgebase.update(
            token_num=Knowledgebase.token_num + token_num,
            chunk_num=Knowledgebase.chunk_num + chunk_num,
        ).where(Knowledgebase.id == kb_id).execute()

    @db.connection_context()
    def release_fixed(doc_id):
        with db.atomic():
            fresh = Document.get_by_id(doc_id)
            if not (fresh.token_num or fresh.chunk_num or fresh.process_duration):
                return
            apply_chunk_num_delta(
                fresh.id, fresh.kb_id, -fresh.token_num, -fresh.chunk_num, -fresh.process_duration
            )

    db.close()
    release_fixed("d1")

    db.connect(reuse_if_open=True)
    doc = Document.get_by_id("d1")
    kb = Knowledgebase.get_by_id("k1")
    assert doc.token_num == 0
    assert doc.chunk_num == 0
    assert doc.process_duration == 0
    assert kb.token_num == 90
    assert kb.chunk_num == 18
    assert not db.in_transaction()
