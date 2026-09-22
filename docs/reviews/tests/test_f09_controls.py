"""Synthetic controls using real functions; not service integration acceptance."""
import asyncio
import importlib.util
import io
import json
import logging
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
from review_test_tools import ROOT, functions
sys.path.insert(0, str(ROOT))
from enterprise.gateway.query.answer_split import (
    split_assistant_output, StreamThinkSplitter, public_reasoning, safe_execution_reasoning,
)


class ReasoningTests(unittest.TestCase):
    def test_raw_and_legacy_never_public(self):
        self.assertEqual(public_reasoning('PRIVATE_SYNTHETIC'), '正在处理请求。')
        self.assertIsNone(safe_execution_reasoning('PRIVATE_SYNTHETIC', 'safe_execution_v1'))
        self.assertIsNone(safe_execution_reasoning('正在处理请求。', None))

    def test_fragmented_case_tags_and_flags(self):
        for text in ('<THINK>PRIVATE_SYNTHETIC</THINK>answer', '<think>PRIVATE_SYNTHETIC'):
            stream = StreamThinkSplitter()
            pieces = [p for char in text for p in stream.feed(char)]
            answer = ''.join(v for kind,v in pieces if kind == 'answer')
            self.assertNotIn('PRIVATE_SYNTHETIC', answer)
            self.assertEqual(answer, split_assistant_output(text).answer)
        stream = StreamThinkSplitter()
        self.assertEqual(stream.feed('PRIVATE', start_to_think=True), [('reasoning','PRIVATE')])
        self.assertEqual(stream.feed('', end_to_think=True), [])
        self.assertEqual(stream.feed('answer'), [('answer','answer')])

    def test_code_literals_preserved_json_and_stream(self):
        for text in ('`<think>literal</think>` tail', '```xml\n<think>literal</think>\n```\ntail',
                     '    <think>literal</think>\ntail'):
            self.assertEqual(split_assistant_output(text).answer, text)
            splitter = StreamThinkSplitter()
            answer = ''.join(v for ch in text for kind,v in splitter.feed(ch) if kind == 'answer')
            self.assertEqual(answer, text)

    def test_unclosed_after_closed_is_not_answer(self):
        result = split_assistant_output('<think>first</think>answer<think>PRIVATE')
        self.assertEqual(result.answer, 'answer')
        self.assertIn('PRIVATE', result.reasoning)



class LoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_logs_remove_message_and_exception_payload(self):
        spec = importlib.util.spec_from_file_location('safe_logging_fixture',ROOT/'ragflow/rag/safe_logging.py')
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        old = logging.getLogRecordFactory()
        stream = io.StringIO(); handler = logging.StreamHandler(stream)
        logger = logging.getLogger('f09-fixture'); logger.addHandler(handler); logger.setLevel(logging.WARNING); logger.propagate=False
        @mod.scoped_execution
        async def run(**kw):
            try: raise ValueError('PRIVATE_EXCEPTION')
            except ValueError: logger.exception('PRIVATE_PROMPT %s','PRIVATE_TOOL')
            yield 1
        try:
            self.assertEqual([x async for x in run(doc_scope_mode='restrict')],[1])
            self.assertNotIn('PRIVATE',stream.getvalue())
            self.assertIn('enterprise_execution_event',stream.getvalue())
            self.assertFalse(mod.active())
        finally:
            logging.setLogRecordFactory(old); logger.removeHandler(handler)



class TimelineTests(unittest.TestCase):
    def test_unknown_stages_and_free_text_metadata_never_render(self):
        from unittest.mock import patch
        from types import ModuleType
        modules={}
        for name in ('rag','rag.advanced_rag'):
            modules[name]=ModuleType(name); modules[name].__path__=[]
        with patch.dict(sys.modules,modules):
            for suffix in ('think_log','think_timeline'):
                name='rag.advanced_rag.'+suffix
                spec=importlib.util.spec_from_file_location(name,ROOT/f'ragflow/rag/advanced_rag/{suffix}.py')
                mod=importlib.util.module_from_spec(spec); sys.modules[name]=mod
                spec.loader.exec_module(mod)
                modules[name]=mod
            timeline=modules['rag.advanced_rag.think_timeline']
            logs=modules['rag.advanced_rag.think_log']
            self.assertIsNone(logs.public_think_log_detail('[PRIVATE_STAGE] content'))
            token=timeline.begin_think_timeline()
            try:
                timeline.record_think_timeline_stage('PRIVATE_STAGE')
                timeline.record_think_timeline_stage('retrieval',meta={'hitCount':3,'toolName':'PRIVATE_TOOL',
                    'status':'PRIVATE_STATUS','mode':'PRIVATE_MODE'})
                entries=timeline.snapshot_think_timeline()
                self.assertEqual(len(entries),1)
                self.assertEqual(entries[0]['meta'],{'hitCount':3})
                entries[0]['display']='PRIVATE_DISPLAY'
                self.assertNotIn('PRIVATE',timeline.render_think_timeline(entries))
            finally: timeline.reset_think_timeline(token)
