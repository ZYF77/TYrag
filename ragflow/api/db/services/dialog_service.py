#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import asyncio
import html
import logging
import re
import time
import uuid
from copy import deepcopy
from rag.advanced_rag.agentic_rag import RAGTools

logger = logging.getLogger(__name__)
from datetime import datetime, timezone
from functools import partial
from timeit import default_timer as timer
from langfuse import Langfuse, propagate_attributes
from peewee import fn
from api.db.services.file_service import FileService
from common.constants import LLMType, ParserType, StatusEnum
from api.db.db_models import DB, Dialog
from api.db.services.common_service import CommonService
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.knowledgebase_service import KnowledgebaseService, validate_dataset_embedding_models
from api.db.services.langfuse_service import TenantLangfuseService
from api.db.services.llm_service import LLMBundle
from common.metadata_utils import apply_meta_data_filter
from api.utils.reference_metadata_utils import (
    enrich_chunks_with_document_metadata,
    resolve_reference_metadata_preferences,
)
from api.utils.scope_identity_prompt import (
    _build_scope_identity_knowledge_block,
    _filter_scope_device_identifiers,
    _prepend_scope_identity_knowledge,
)
from api.db.joint_services.tenant_model_service import get_tenant_default_model_by_type, resolve_model_config, resolve_model_type, get_model_config_by_id
from common.time_utils import current_timestamp, datetime_format
from common.text_utils import normalize_arabic_digits
from rag.advanced_rag.knowlege_compile.mind_map_extractor import MindMapExtractor
from rag.advanced_rag import DeepResearcher
from rag.app.tag import label_question
from rag.grounding.guard import (
    STANDARD_ABSTAIN_ANSWER,
    GroundingResult,
    apply_identifier_numeric_fuse,
    empty_reference,
    strip_ungrounded_zero_measurements,
    _unmatched_keys_are_zero_only,
)
from rag.nlp.search import index_name
from rag.prompts.generator import chunks_format, citation_prompt, cross_languages, full_question, kb_prompt, keyword_extraction, message_fit_in, PROMPT_JINJA_ENV, ASK_SUMMARY
from common.token_utils import num_tokens_from_string
from common.misc_utils import thread_pool_exec
from rag.utils.web_search_conn import create_web_search_provider, has_web_search_provider
from rag.utils.tts_cache import synthesize_with_cache
from common.string_utils import remove_redundant_spaces
from common import settings


def _grounding_requested(version) -> bool:
    return type(version) is int and version == 1


def _grounding_markers(knowledge: str) -> tuple[str, str]:
    """Return collision-free per-request markers for one knowledge body."""
    knowledge = knowledge or ""
    while True:
        nonce = uuid.uuid4().hex
        start = f"<GROUNDING_START:{nonce}>"
        end = f"<GROUNDING_END:{nonce}>"
        if start not in knowledge and end not in knowledge:
            return start, end


def _extract_effective_knowledge(content: str, start: str | None, end: str | None) -> str:
    if not start or not content:
        return ""
    start_index = content.find(start)
    if start_index < 0:
        return ""
    body_start = start_index + len(start)
    end_index = content.find(end, body_start) if end else -1
    return content[body_start:end_index if end_index >= 0 else len(content)]


# Identifier/Numeric Fuse implementation is retained below, but not applied on
# the live path until explicitly re-enabled. Prompt-fit, empty_response, and
# prompt/log redaction still follow grounding_version=1. Candidate-token
# buffering follows this fuse switch so streaming can stay live while the
# fuse is off.
_IDENTIFIER_NUMERIC_FUSE_ENABLED = False


def _buffer_candidate_tokens(grounding_enabled: bool) -> bool:
    return bool(grounding_enabled and _IDENTIFIER_NUMERIC_FUSE_ENABLED)


def _use_simple_chat(prompt_config, kwargs) -> bool:
    return not prompt_config.get("reasoning", 0) and not kwargs.get("reasoning")


def _log_grounding_guard(result, *, abstain: bool) -> None:
    logging.info(
        "grounding_guard passed=%s unmatched_identifiers=%s unmatched_numbers=%s unmatched_number_keys=%s abstain=%s",
        result.passed,
        result.unmatched_identifiers,
        result.unmatched_numbers,
        list(getattr(result, "unmatched_number_keys", ()) or ()),
        abstain,
    )


_SHORT_FUSE_RETRY_SUFFIX = (
    "\n\n[系统约束] 请仅根据原有证据重新简短回答。"
    "不要新增任何数字、数量统计或数字序号；证据中的原始数字除外。"
    "不要用 0 或 0.0 作为未给出参数的占位值。"
    "不要因为需要重写而改成无依据拒答。"
)
_SHORT_FUSE_RETRY_MAX_TOKENS = 512


def _fuse_or_keep(
    ans: dict,
    *,
    effective_knowledge: str,
    attachment_observations,
    allowed_identifiers,
):
    if not _IDENTIFIER_NUMERIC_FUSE_ENABLED:
        logging.info("grounding_guard skipped enabled=False")
        return ans, GroundingResult(
            passed=True,
            unmatched_identifiers=0,
            unmatched_numbers=0,
            unmatched_number_keys=(),
        )

    result = apply_identifier_numeric_fuse(
        ans.get("answer") or "",
        effective_knowledge,
        attachment_observations,
        allowed_identifiers,
    )
    _log_grounding_guard(result, abstain=not result.passed)
    if result.passed:
        return ans, result

    # Deterministic repair: drop invented 0/0.0+unit placeholders, then re-fuse.
    if _unmatched_keys_are_zero_only(result):
        cleaned_answer = strip_ungrounded_zero_measurements(ans.get("answer") or "")
        if cleaned_answer.strip() != (ans.get("answer") or "").strip():
            cleaned = dict(ans)
            cleaned["answer"] = cleaned_answer
            result2 = apply_identifier_numeric_fuse(
                cleaned_answer,
                effective_knowledge,
                attachment_observations,
                allowed_identifiers,
            )
            logging.info(
                "grounding_guard zero_placeholder_strip unmatched_before=%s passed=%s unmatched_numbers=%s",
                result.unmatched_numbers,
                result2.passed,
                result2.unmatched_numbers,
            )
            _log_grounding_guard(result2, abstain=not result2.passed)
            if result2.passed:
                return cleaned, result2
            result = result2

    fused = dict(ans)
    fused["answer"] = STANDARD_ABSTAIN_ANSWER
    fused["reference"] = empty_reference()
    return fused, result


def _should_short_fuse_retry(result, *, effective_knowledge: str, allow_short_retry: bool) -> bool:
    """Retry only for numeric-only fuse fails when evidence was provided to the model."""
    if not _IDENTIFIER_NUMERIC_FUSE_ENABLED:
        return False
    if not allow_short_retry:
        return False
    if not (effective_knowledge or "").strip():
        return False
    if result.passed:
        return False
    return result.unmatched_identifiers == 0 and result.unmatched_numbers > 0


def _with_short_fuse_retry_instruction(history: list) -> list:
    if not history:
        return history
    out = deepcopy(history)
    last = dict(out[-1])
    content = last.get("content")
    if isinstance(content, list):
        last["content"] = list(content) + [{"type": "text", "text": _SHORT_FUSE_RETRY_SUFFIX}]
    else:
        last["content"] = str(content or "") + _SHORT_FUSE_RETRY_SUFFIX
    out[-1] = last
    return out


async def _generate_short_fuse_retry(
    chat_mdl,
    *,
    prompt: str,
    prompt4citation: str,
    msg: list,
    gen_conf: dict,
    model_type: str,
    image_files=None,
):
    retry_conf = dict(gen_conf or {})
    retry_conf["max_tokens"] = min(
        int(retry_conf.get("max_tokens") or _SHORT_FUSE_RETRY_MAX_TOKENS),
        _SHORT_FUSE_RETRY_MAX_TOKENS,
    )
    history = _with_short_fuse_retry_instruction(msg[1:])
    system = prompt + (prompt4citation or "")
    logging.info(
        "grounding_guard retry=1 max_tokens=%s",
        retry_conf.get("max_tokens"),
    )
    if model_type == "chat":
        return await chat_mdl.async_chat(system, history, retry_conf)
    return await chat_mdl.async_chat(system, history, retry_conf, images=image_files)


def _grounding_abstain_event(**extra) -> dict:
    payload = {
        "answer": STANDARD_ABSTAIN_ANSWER,
        "reference": empty_reference(),
        "prompt": "",
        "audio_binary": None,
        "final": True,
    }
    payload.update(extra)
    return payload


def _chunk_kb_id_for_doc(row_dict, kb_ids, doc_id):
    if len(kb_ids or []) == 1:
        return kb_ids[0]
    return row_dict.get("kb_id") or row_dict.get("kb_id_kwd")


async def _hydrate_chunk_vectors(retriever, chunks, tenant_ids, kb_ids):
    """
    Citation prep: on the ES backend the main retrieval call deliberately
    skips fetching the chunk embedding. insert_citations needs it, so we
    pull the vectors for just the candidate chunks right before computing
    answer-vs-chunk similarity. Chunks without an ES chunk_id (e.g. web
    search results) keep whatever placeholder they were given. Other
    backends still carry vectors in the chunk, so we skip the round-trip.
    """
    if settings.DOC_ENGINE_INFINITY or settings.DOC_ENGINE_OCEANBASE or settings.DOC_ENGINE_SERENEDB:
        return
    if not chunks:
        return
    dim = 0
    for ck in chunks:
        v = ck.get("vector")
        if isinstance(v, list) and v:
            dim = len(v)
            break
    if not dim:
        return
    # Skip chunks that already have a non-zero vector (e.g. parent chunks
    # produced by retrieval_by_children copy the child vector inline).
    chunk_ids = []
    for ck in chunks:
        cid = ck.get("chunk_id")
        if not cid:
            continue
        v = ck.get("vector") or []
        if any(x for x in v):
            continue
        chunk_ids.append(cid)
    if not chunk_ids:
        return
    try:
        vectors = await retriever.fetch_chunk_vectors(chunk_ids, tenant_ids, kb_ids, dim)
    except Exception as e:  # noqa: BLE001 - degrade gracefully on hydrate failure
        logger.warning("fetch_chunk_vectors failed; citations will use placeholders: %s", e)
        return
    if not vectors:
        return
    for ck in chunks:
        cid = ck.get("chunk_id")
        if cid and cid in vectors:
            ck["vector"] = vectors[cid]


def _normalize_internet_flag(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    return None


def _should_use_web_search(prompt_config, internet=None):
    if not has_web_search_provider(prompt_config):
        return False
    normalized = _normalize_internet_flag(internet)
    return normalized is True


def _resolve_reference_metadata(config, request_payload=None):
    return resolve_reference_metadata_preferences(request_payload or {}, config)


def _enrich_chunks_with_document_metadata(chunks, metadata_fields=None):
    enrich_chunks_with_document_metadata(chunks, metadata_fields)


class DialogService(CommonService):
    model = Dialog

    @classmethod
    def save(cls, **kwargs):
        """Save a new record to database.

        This method creates a new record in the database with the provided field values,
        forcing an insert operation rather than an update.

        Args:
            **kwargs: Record field values as keyword arguments.

        Returns:
            Model instance: The created record object.
        """
        sample_obj = cls.model(**kwargs).save(force_insert=True)
        return sample_obj

    @classmethod
    def update_many_by_id(cls, data_list):
        """Update multiple records by their IDs.

        This method updates multiple records in the database, identified by their IDs.
        It automatically updates the update_time and update_date fields for each record.

        Args:
            data_list (list): List of dictionaries containing record data to update.
                             Each dictionary must include an 'id' field.
        """
        with DB.atomic():
            for data in data_list:
                data["update_time"] = current_timestamp()
                data["update_date"] = datetime_format(datetime.now())
                cls.model.update(data).where(cls.model.id == data["id"]).execute()

    @classmethod
    @DB.connection_context()
    def get_list(cls, tenant_id, page_number, items_per_page, orderby, desc, id, name):
        chats = cls.model.select()
        if id:
            chats = chats.where(cls.model.id == id)
        if name:
            chats = chats.where(cls.model.name == name)
        chats = chats.where((cls.model.tenant_id == tenant_id) & (cls.model.status == StatusEnum.VALID.value))
        if desc:
            chats = chats.order_by(cls.model.getter_by(orderby).desc())
        else:
            chats = chats.order_by(cls.model.getter_by(orderby).asc())

        chats = chats.paginate(page_number, items_per_page)

        return list(chats.dicts())

    @classmethod
    @DB.connection_context()
    def get_by_tenant_ids(
        cls,
        joined_tenant_ids,
        user_id,
        page_number,
        items_per_page,
        orderby,
        desc,
        keywords,
        id=None,
        name=None,
    ):
        from api.db.db_models import User

        fields = [
            cls.model.id,
            cls.model.tenant_id,
            cls.model.name,
            cls.model.description,
            cls.model.language,
            cls.model.llm_id,
            cls.model.llm_setting,
            cls.model.prompt_type,
            cls.model.prompt_config,
            cls.model.similarity_threshold,
            cls.model.vector_similarity_weight,
            cls.model.top_n,
            cls.model.top_k,
            cls.model.do_refer,
            cls.model.rerank_id,
            cls.model.kb_ids,
            cls.model.icon,
            cls.model.status,
            User.nickname,
            User.avatar.alias("tenant_avatar"),
            cls.model.update_time,
            cls.model.create_time,
        ]
        dialogs = (
            cls.model.select(*fields)
            .join(User, on=(cls.model.tenant_id == User.id))
            .where(
                (cls.model.tenant_id.in_(joined_tenant_ids) | (cls.model.tenant_id == user_id)) & (cls.model.status == StatusEnum.VALID.value),
            )
        )
        if id:
            dialogs = dialogs.where(cls.model.id == id)
        if name:
            dialogs = dialogs.where(cls.model.name == name)
        if keywords:
            dialogs = dialogs.where(fn.LOWER(cls.model.name).contains(keywords.lower()))
        if desc:
            dialogs = dialogs.order_by(cls.model.getter_by(orderby).desc())
        else:
            dialogs = dialogs.order_by(cls.model.getter_by(orderby).asc())

        count = dialogs.count()

        if page_number and items_per_page:
            dialogs = dialogs.paginate(page_number, items_per_page)

        return list(dialogs.dicts()), count

    @classmethod
    @DB.connection_context()
    def get_all_dialogs_by_tenant_id(cls, tenant_id):
        fields = [cls.model.id]
        dialogs = cls.model.select(*fields).where(cls.model.tenant_id == tenant_id)
        dialogs.order_by(cls.model.create_time.asc())
        offset, limit = 0, 100
        res = []
        while True:
            d_batch = dialogs.offset(offset).limit(limit)
            _temp = list(d_batch.dicts())
            if not _temp:
                break
            res.extend(_temp)
            offset += limit
        return res

    @classmethod
    @DB.connection_context()
    def get_null_tenant_llm_id_row(cls):
        fields = [cls.model.id, cls.model.tenant_id, cls.model.llm_id]
        objs = cls.model.select(*fields).where(cls.model.tenant_llm_id.is_null())
        return list(objs)

    @classmethod
    @DB.connection_context()
    def get_null_tenant_rerank_id_row(cls):
        fields = [cls.model.id, cls.model.tenant_id, cls.model.rerank_id]
        objs = cls.model.select(*fields).where(cls.model.tenant_rerank_id.is_null())
        return list(objs)


def _resolve_dialog_llm_config(dialog):
    """Resolve the dialog chat/vision model, falling back to the tenant default.

    Dialogs created before a provider re-import can still store a deleted
    tenant_model.id in llm_id / tenant_llm_id. Name-based fallback then
    treats the UUID as ``model@provider`` and raises ``Provider not found``.
    """
    try:
        if not dialog.llm_id:
            return get_tenant_default_model_by_type(dialog.tenant_id, LLMType.CHAT)
        if dialog.tenant_llm_id:
            try:
                llm_types = resolve_model_type(dialog.tenant_id, dialog.llm_id)
                if "chat" in llm_types:
                    return get_model_config_by_id(dialog.tenant_id, LLMType.CHAT, dialog.tenant_llm_id)
                return resolve_model_config(dialog.tenant_id, LLMType.VISION, dialog.llm_id)
            except LookupError:
                pass
        llm_types = resolve_model_type(dialog.tenant_id, dialog.llm_id)
        if "chat" in llm_types:
            return resolve_model_config(dialog.tenant_id, LLMType.CHAT, dialog.llm_id)
        return resolve_model_config(dialog.tenant_id, LLMType.VISION, dialog.llm_id)
    except LookupError:
        logging.warning(
            "dialog chat model missing tenant_id=%s llm_id=%s tenant_llm_id=%s; using tenant default",
            dialog.tenant_id,
            getattr(dialog, "llm_id", None),
            getattr(dialog, "tenant_llm_id", None),
        )
        return get_tenant_default_model_by_type(dialog.tenant_id, LLMType.CHAT)


async def async_chat_solo(dialog, messages, stream=True, session_id=None, grounding_version=None, **kwargs):
    grounding_enabled = _grounding_requested(grounding_version)
    allowed_identifiers = list(kwargs.pop("allowed_identifiers", None) or [])
    attachment_observations = kwargs.pop("attachment_observations", None)
    last_user = str(messages[-1].get("content") or "") if messages else ""
    if last_user:
        allowed_identifiers.append(last_user)
    attachments = ""
    image_attachments = []
    image_files = []

    model_config = _resolve_dialog_llm_config(dialog)

    bundle_kwargs = {"langfuse_session_id": session_id}
    if grounding_enabled:
        bundle_kwargs["disable_langfuse"] = True
    chat_mdl = LLMBundle(dialog.tenant_id, model_config, **bundle_kwargs)
    factory = model_config.get("llm_factory", "") if model_config else ""
    if "files" in messages[-1]:
        if model_config["model_type"] == "chat":
            text_attachments, image_attachments = split_file_attachments(messages[-1]["files"])
        else:
            text_attachments, image_files = split_file_attachments(messages[-1]["files"], raw=True)
        attachments = "\n\n".join(text_attachments)

    prompt_config = dialog.prompt_config
    tts_mdl = None
    if prompt_config.get("tts") and not grounding_enabled:
        default_tts_model = get_tenant_default_model_by_type(dialog.tenant_id, LLMType.TTS)
        tts_mdl = LLMBundle(dialog.tenant_id, default_tts_model, trace_context=chat_mdl.trace_context, langfuse_session_id=session_id)
    msg = [{"role": m["role"], "content": re.sub(r"##\d+\$\$", "", m["content"])} for m in messages if m["role"] != "system"]
    if attachments and msg:
        msg[-1]["content"] += attachments
    if model_config["model_type"] == "chat" and image_attachments:
        convert_last_user_msg_to_multimodal(msg, image_attachments, factory)
    sys_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    system_prompt = prompt_config.get("system", "").replace("{date}", sys_date)
    if stream:
        if model_config["model_type"] == "chat":
            stream_iter = chat_mdl.async_chat_streamly_delta(system_prompt, msg, dialog.llm_setting)
        else:
            stream_iter = chat_mdl.async_chat_streamly_delta(system_prompt, msg, dialog.llm_setting, images=image_files)
        last_state = None
        async for kind, value, state in _stream_with_think_delta(stream_iter):
            last_state = state
            if _buffer_candidate_tokens(grounding_enabled):
                continue
            if kind == "marker":
                flags = {"start_to_think": True} if value == "<think>" else {"end_to_think": True}
                yield {"answer": "", "reference": {}, "audio_binary": None, "prompt": "", "created_at": time.time(), "final": False, **flags}
                continue
            yield {
                "answer": value,
                "reference": {},
                "audio_binary": tts(tts_mdl, value),
                "prompt": "",
                "created_at": time.time(),
                "final": False,
            }
        if grounding_enabled:
            answer = last_state.full_text if last_state else ""
            fused, _result = _fuse_or_keep(
                {"answer": answer, "reference": {}, "prompt": "", "created_at": time.time()},
                effective_knowledge="",
                attachment_observations=attachment_observations,
                allowed_identifiers=allowed_identifiers,
            )
            fused["audio_binary"] = None
            fused["final"] = True
            yield fused
    else:
        if model_config["model_type"] == "chat":
            answer = await chat_mdl.async_chat(system_prompt, msg, dialog.llm_setting)
        else:
            answer = await chat_mdl.async_chat(system_prompt, msg, dialog.llm_setting, images=image_files)
        user_content = msg[-1].get("content", "[content not available]")
        if not grounding_enabled:
            logging.debug("User: {}|Assistant: {}".format(user_content, answer))
        payload = {"answer": answer, "reference": {}, "audio_binary": tts(tts_mdl, answer), "prompt": "", "created_at": time.time()}
        if grounding_enabled:
            payload, _result = _fuse_or_keep(
                payload,
                effective_knowledge="",
                attachment_observations=attachment_observations,
                allowed_identifiers=allowed_identifiers,
            )
            payload["audio_binary"] = tts(tts_mdl, payload.get("answer") or "")
        yield payload


def get_models(dialog, trace_context=None, langfuse_session_id=None, disable_langfuse=False):
    embd_mdl, chat_mdl, rerank_mdl, tts_mdl = None, None, None, None

    def bundle_kwargs():
        result = {"trace_context": trace_context, "langfuse_session_id": langfuse_session_id}
        if disable_langfuse:
            result["disable_langfuse"] = True
        return result

    kbs = KnowledgebaseService.get_by_ids(dialog.kb_ids)
    err = validate_dataset_embedding_models(kbs)
    if err:
        raise Exception(err)

    if kbs and kbs[0].embd_id:
        embd_owner_tenant_id = kbs[0].tenant_id
        embd_model_config = resolve_model_config(embd_owner_tenant_id, LLMType.EMBEDDING, kbs[0].embd_id)
        embd_mdl = LLMBundle(embd_owner_tenant_id, embd_model_config, **bundle_kwargs())
        if not embd_mdl:
            raise LookupError("Embedding model(%s) not found" % kbs[0].embd_id)

    chat_model_config = _resolve_dialog_llm_config(dialog)

    chat_mdl = LLMBundle(dialog.tenant_id, chat_model_config, **bundle_kwargs())

    if dialog.rerank_id:
        if dialog.tenant_rerank_id:
            try:
                rerank_model_config = get_model_config_by_id(dialog.tenant_id, LLMType.RERANK, dialog.tenant_rerank_id)
            except LookupError:
                rerank_model_config = resolve_model_config(dialog.tenant_id, LLMType.RERANK, dialog.rerank_id)
        else:
            rerank_model_config = resolve_model_config(dialog.tenant_id, LLMType.RERANK, dialog.rerank_id)
        rerank_mdl = LLMBundle(dialog.tenant_id, rerank_model_config, **bundle_kwargs())

    if dialog.prompt_config.get("tts") and not disable_langfuse:
        default_tts_model_config = get_tenant_default_model_by_type(dialog.tenant_id, LLMType.TTS)
        tts_mdl = LLMBundle(dialog.tenant_id, default_tts_model_config, **bundle_kwargs())
    return kbs, embd_mdl, rerank_mdl, chat_mdl, tts_mdl


def split_file_attachments(files: list[dict] | None, raw: bool = False) -> tuple[list[str], list[str] | list[dict]]:
    if not files:
        return [], []

    text_attachments = []
    if raw:
        file_contents, image_files = FileService.get_files(files, raw=True)
        for content in file_contents:
            if not isinstance(content, str):
                content = str(content)
            text_attachments.append(content)
        return text_attachments, image_files

    image_attachments = []
    for content in FileService.get_files(files, raw=False):
        if not isinstance(content, str):
            content = str(content)
        if content.strip().startswith("data:"):
            image_attachments.append(content.strip())
            continue
        text_attachments.append(content)
    return text_attachments, image_attachments


_DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<b64>[A-Za-z0-9+/=\s]+)$")


def _parse_data_uri_or_b64(s: str, default_mime: str = "image/png") -> tuple[str, str]:
    s = (s or "").strip()
    match = _DATA_URI_RE.match(s)
    if match:
        mime = match.group("mime").strip()
        b64 = match.group("b64").strip()
        return mime, b64
    return default_mime, s


def _normalize_text_from_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for blk in content:
            if isinstance(blk, dict):
                if blk.get("type") in {"text", "input_text"}:
                    txt = blk.get("text")
                    if txt:
                        texts.append(str(txt))
                elif "text" in blk and isinstance(blk.get("text"), (str, int, float)):
                    texts.append(str(blk["text"]))
        return "\n".join(texts).strip()
    return str(content)


def filter_kbinfos_to_doc_ids(
    kbinfos: dict | None,
    allowed: list[str] | None,
    *,
    allow_web: bool = False,
) -> dict:
    """Hard-drop chunks/doc_aggs outside Gateway doc_ids (simple-chat path)."""
    if not isinstance(kbinfos, dict) or not allowed:
        return kbinfos or {"chunks": [], "doc_aggs": []}
    allowed_set = set(allowed)

    def _keep_chunk(chunk: dict) -> bool:
        document_id = chunk.get("document_id") or chunk.get("doc_id")
        if document_id in allowed_set:
            return True
        if not allow_web:
            return False
        url = str(chunk.get("url") or "").strip()
        return url.startswith("http://") or url.startswith("https://")

    chunks = [
        chunk
        for chunk in kbinfos.get("chunks", []) or []
        if isinstance(chunk, dict) and _keep_chunk(chunk)
    ]
    aggs = [
        agg
        for agg in kbinfos.get("doc_aggs", []) or []
        if isinstance(agg, dict) and agg.get("doc_id") in allowed_set
    ]
    seen = {agg.get("doc_id") for agg in aggs}
    for chunk in chunks:
        document_id = chunk.get("document_id") or chunk.get("doc_id")
        if document_id not in allowed_set or document_id in seen:
            continue
        seen.add(document_id)
        aggs.append(
            {
                "doc_id": document_id,
                "doc_name": chunk.get("docnm_kwd") or chunk.get("document_name") or "",
            }
        )
    scoped = dict(kbinfos)
    scoped["chunks"] = chunks
    scoped["doc_aggs"] = aggs
    return scoped


def convert_last_user_msg_to_multimodal(msg: list[dict], image_data_uris: list[str], factory: str) -> None:
    if not msg or not image_data_uris:
        return

    factory_norm = (factory or "").strip().lower()

    for idx in range(len(msg) - 1, -1, -1):
        if msg[idx].get("role") != "user":
            continue

        original_content = msg[idx].get("content", "")
        text = _normalize_text_from_content(original_content)

        if factory_norm == "gemini":
            parts = []
            if text:
                parts.append({"text": text})
            for image in image_data_uris:
                mime, b64 = _parse_data_uri_or_b64(str(image), default_mime="image/png")
                parts.append({"inline_data": {"mime_type": mime, "data": b64}})
            msg[idx]["content"] = parts
            return

        if factory_norm == "anthropic":
            blocks = []
            if text:
                blocks.append({"type": "text", "text": text})
            for image in image_data_uris:
                mime, b64 = _parse_data_uri_or_b64(str(image), default_mime="image/png")
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": b64},
                    }
                )
            msg[idx]["content"] = blocks
            return

        multimodal_content = []
        if isinstance(original_content, list):
            multimodal_content = deepcopy(original_content)
        else:
            text_content = "" if original_content is None else str(original_content)
            if text_content:
                multimodal_content.append({"type": "text", "text": text_content})

        for data_uri in image_data_uris:
            image_url = data_uri
            if not isinstance(image_url, str):
                image_url = str(image_url)
            if not image_url.startswith("data:"):
                image_url = f"data:image/png;base64,{image_url}"
            multimodal_content.append({"type": "image_url", "image_url": {"url": image_url}})

        msg[idx]["content"] = multimodal_content
        return


BAD_CITATION_PATTERNS = [
    re.compile(r"\(\s*ID\s*[: ]*\s*(\d+)\s*\)"),  # (ID: 12)
    re.compile(r"\[\s*ID\s*[: ]*\s*(\d+)\s*\]"),  # [ID: 12]
    re.compile(r"【\s*ID\s*[: ]*\s*(\d+)\s*】"),  # 【ID: 12】
    re.compile(r"ref\s*(\d+)", flags=re.IGNORECASE),  # ref12、REF 12
]
CITATION_MARKER_PATTERN = re.compile(r"\[(?:ID:)?([0-9\u0660-\u0669\u06F0-\u06F9]+)\]")


def repair_bad_citation_formats(answer: str, kbinfos: dict, idx: set):
    max_index = len(kbinfos["chunks"])
    normalized_answer = normalize_arabic_digits(answer) or ""

    def safe_add(i):
        if 0 <= i < max_index:
            idx.add(i)
            return True
        return False

    def find_and_replace(pattern, group_index=1, repl=lambda digits: f"ID:{digits}"):
        nonlocal answer
        nonlocal normalized_answer

        matches = list(pattern.finditer(normalized_answer))
        if not matches:
            return

        parts = []
        last_idx = 0
        for match in matches:
            parts.append(answer[last_idx : match.start()])
            try:
                i = int(match.group(group_index))
            except Exception:
                parts.append(answer[match.start() : match.end()])
                last_idx = match.end()
                continue

            if safe_add(i):
                digit_start, digit_end = match.span(group_index)
                digits_original = answer[digit_start:digit_end]
                parts.append(f"[{repl(digits_original)}]")
            else:
                parts.append(answer[match.start() : match.end()])
            last_idx = match.end()

        parts.append(answer[last_idx:])
        answer = "".join(parts)
        normalized_answer = normalize_arabic_digits(answer) or ""

    for pattern in BAD_CITATION_PATTERNS:
        find_and_replace(pattern)

    return answer, idx


async def async_chat(dialog, messages, stream=True, **kwargs):
    logging.debug("Begin async_chat")
    assert messages[-1]["role"] == "user", "The last content of this conversation is not from user."
    grounding_enabled = _grounding_requested(kwargs.get("grounding_version"))
    # Prefer Gateway scope_identifiers for the generation-side identity block.
    # Fall back to a copy of allowed_identifiers taken BEFORE appending last_user
    # (grounding appends the full question; that must never become an equipment id).
    scope_identifiers = list(kwargs.pop("scope_identifiers", None) or [])
    allowed_identifiers = list(kwargs.pop("allowed_identifiers", None) or [])
    scope_id_sources = list(scope_identifiers) if scope_identifiers else list(allowed_identifiers)
    attachment_observations = kwargs.pop("attachment_observations", None)
    last_user = str(messages[-1].get("content") or "")
    if last_user:
        allowed_identifiers.append(last_user)
    session_id = kwargs.get("session_id")
    use_web_search = _should_use_web_search(dialog.prompt_config, kwargs.get("internet"))
    logging.debug("web_search kb=%s configured=%s internet=%r enabled=%s", bool(dialog.kb_ids), has_web_search_provider(dialog.prompt_config), kwargs.get("internet"), use_web_search)
    if not dialog.kb_ids and not use_web_search:
        solo_kwargs = {"session_id": session_id}
        if grounding_enabled:
            solo_kwargs["grounding_version"] = 1
            solo_kwargs["allowed_identifiers"] = allowed_identifiers
            solo_kwargs["attachment_observations"] = attachment_observations
        async for ans in async_chat_solo(dialog, messages, stream, **solo_kwargs):
            yield ans
        return

    chat_start_ts = timer()
    llm_model_config = _resolve_dialog_llm_config(dialog)

    factory = llm_model_config.get("llm_factory", "") if llm_model_config else ""
    max_tokens = llm_model_config.get("max_tokens") or 8192

    check_llm_ts = timer()

    langfuse_tracer = None
    langfuse_generation = None
    trace_context = {}
    langfuse_keys = None if grounding_enabled else TenantLangfuseService.filter_by_tenant(tenant_id=dialog.tenant_id)
    if langfuse_keys:
        langfuse = Langfuse(public_key=langfuse_keys.public_key, secret_key=langfuse_keys.secret_key, host=langfuse_keys.host)
        try:
            if langfuse.auth_check():
                langfuse_tracer = langfuse
                trace_id = langfuse_tracer.create_trace_id()
                trace_context = {"trace_id": trace_id}
        except Exception:
            # Skip langfuse tracing if connection fails
            pass

    check_langfuse_tracer_ts = timer()
    model_kwargs = {"trace_context": trace_context, "langfuse_session_id": session_id}
    if grounding_enabled:
        model_kwargs["disable_langfuse"] = True
    kbs, embd_mdl, rerank_mdl, chat_mdl, tts_mdl = get_models(dialog, **model_kwargs)
    toolcall_session, tools = kwargs.get("toolcall_session"), kwargs.get("tools")
    if toolcall_session and tools:
        chat_mdl.bind_tools(toolcall_session, tools)
    bind_models_ts = timer()

    retriever = settings.retriever
    questions = [m["content"] for m in messages if m["role"] == "user"][-3:]
    attachments = None
    if "doc_ids" in kwargs:
        attachments = [doc_id for doc_id in kwargs["doc_ids"].split(",") if doc_id]
    attachments_ = ""
    image_attachments = []
    image_files = []
    if "doc_ids" in messages[-1]:
        attachments = [doc_id for doc_id in messages[-1]["doc_ids"] if doc_id]
    if "files" in messages[-1]:
        if llm_model_config["model_type"] == "chat":
            text_attachments, image_attachments = split_file_attachments(messages[-1]["files"])
        else:
            text_attachments, image_files = split_file_attachments(messages[-1]["files"], raw=True)
        attachments_ = "\n\n".join(text_attachments)

    # Gateway-provided doc_ids hard ceiling — meta filter must not UNION-expand it.
    gateway_doc_ids = list(attachments) if attachments else None

    prompt_config = dialog.prompt_config
    if dialog.meta_data_filter:
        attachments = await apply_meta_data_filter(
            dialog.meta_data_filter,
            None,
            questions[-1],
            chat_mdl,
            attachments,
            kb_ids=dialog.kb_ids,
            metas_loader=lambda: DocMetadataService.get_flatted_meta_by_kbs(dialog.kb_ids),
        )
        if gateway_doc_ids is not None:
            allowed = set(gateway_doc_ids)
            attachments = [doc_id for doc_id in attachments or [] if doc_id in allowed]

    include_reference_metadata, metadata_fields = _resolve_reference_metadata(prompt_config, request_payload=kwargs)
    field_map = KnowledgebaseService.get_field_map(dialog.kb_ids)
    logging.debug(f"field_map retrieved: {field_map}")
    # try to use sql if field mapping is good to go
    if field_map and not grounding_enabled:
        logging.debug("Use SQL to retrieval:{}".format(questions[-1]))
        ans = await use_sql(questions[-1], field_map, dialog.tenant_id, chat_mdl, prompt_config.get("quote", True), dialog.kb_ids, doc_ids=attachments)
        # For aggregate queries (COUNT, SUM, etc.), chunks may be empty but answer is still valid
        if ans and (ans.get("reference", {}).get("chunks") or ans.get("answer")):
            if gateway_doc_ids is not None and isinstance(ans.get("reference"), dict):
                ans["reference"] = filter_kbinfos_to_doc_ids(ans["reference"], gateway_doc_ids)
            if include_reference_metadata and ans.get("reference", {}).get("chunks"):
                if len(dialog.kb_ids) != 1 and any(not c.get("kb_id") for c in ans["reference"]["chunks"]):
                    logging.warning(
                        "Skipping some _enrich_chunks_with_document_metadata results because dialog.kb_ids has %d entries and use_sql returned chunks without kb_id.",
                        len(dialog.kb_ids),
                    )
                _enrich_chunks_with_document_metadata(ans["reference"]["chunks"], metadata_fields)
            yield ans
            return
        else:
            logging.debug("SQL failed or returned no results, falling back to vector search")

    param_keys = [p["key"] for p in prompt_config.get("parameters", [])]
    if dialog.kb_ids and "knowledge" not in param_keys and "{knowledge}" in prompt_config.get("system", ""):
        logging.warning("prompt_config['parameters'] is missing 'knowledge' entry despite kb_ids being set; auto-fixing.")
        prompt_config.setdefault("parameters", []).append({"key": "knowledge", "optional": False})
        param_keys.append("knowledge")
    logging.debug(f"attachments={attachments}, param_keys={param_keys}, embd_mdl={embd_mdl}")

    sys_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    kwargs["date"] = sys_date
    for p in prompt_config.get("parameters", []):
        if p["key"] == "knowledge":
            continue
        if p["key"] not in kwargs and not p["optional"]:
            raise KeyError("Miss parameter: " + p["key"])
        if p["key"] not in kwargs:
            prompt_config["system"] = prompt_config["system"].replace("{%s}" % p["key"], " ")

    if len(questions) > 1 and prompt_config.get("refine_multiturn"):
        questions = [
            await full_question(
                dialog.tenant_id,
                dialog.llm_id,
                messages,
                chat_mdl=chat_mdl if grounding_enabled else None,
            )
        ]
    else:
        questions = questions[-1:]

    if prompt_config.get("cross_languages"):
        questions = [
            await cross_languages(
                dialog.tenant_id,
                dialog.llm_id,
                questions[0],
                prompt_config["cross_languages"],
                chat_mdl=chat_mdl if grounding_enabled else None,
            )
        ]

    if prompt_config.get("keyword", False):
        questions[-1] = questions[-1] + "," + await keyword_extraction(chat_mdl, questions[-1])
    refine_question_ts = timer()

    thought = ""
    kbinfos = {"total": 0, "chunks": [], "doc_aggs": []}
    knowledges = []

    if "knowledge" in param_keys:
        logging.debug("Proceeding with retrieval")
        tenant_ids = list(set([kb.tenant_id for kb in kbs]))
        knowledges = []
        # replaced by extension of reasoning: 0, 1, 2
        if False:  # prompt_config.get("reasoning", False) or kwargs.get("reasoning"):
            reasoner = DeepResearcher(
                chat_mdl,
                prompt_config,
                partial(
                    retriever.retrieval,
                    embd_mdl=embd_mdl,
                    tenant_ids=tenant_ids,
                    kb_ids=dialog.kb_ids,
                    page=1,
                    page_size=dialog.top_n,
                    similarity_threshold=0.2,
                    vector_similarity_weight=0.3,
                    doc_ids=attachments,
                ),
                internet_enabled=use_web_search,
            )
            queue = asyncio.Queue()

            async def callback(msg: str):
                nonlocal queue
                await queue.put(msg + "<br/>")

            await callback("<START_DEEP_RESEARCH>")
            task = asyncio.create_task(reasoner.research(kbinfos, questions[-1], questions[-1], callback=callback))
            while True:
                msg = await queue.get()
                if msg.find("<START_DEEP_RESEARCH>") == 0:
                    yield {"answer": "<retrieving>", "reference": {}, "audio_binary": None, "final": False}
                elif msg.find("<END_DEEP_RESEARCH>") == 0:
                    yield {"answer": "</retrieving>", "reference": {}, "audio_binary": None, "final": False}
                    break
                else:
                    yield {"answer": msg, "reference": {}, "audio_binary": None, "final": False}

            await task

        else:
            if embd_mdl:
                kbinfos = await retriever.retrieval(
                    " ".join(questions),
                    embd_mdl,
                    tenant_ids,
                    dialog.kb_ids,
                    1,
                    dialog.top_n,
                    dialog.similarity_threshold,
                    dialog.vector_similarity_weight,
                    doc_ids=attachments,
                    top=dialog.top_k,
                    aggs=True,
                    rerank_mdl=rerank_mdl,
                    rank_feature=label_question(" ".join(questions), kbs),
                )
                if prompt_config.get("toc_enhance"):
                    cks = await retriever.retrieval_by_toc(" ".join(questions), kbinfos["chunks"], tenant_ids, chat_mdl, dialog.top_n)
                    if cks:
                        kbinfos["chunks"] = cks
                kbinfos["chunks"] = retriever.retrieval_by_children(kbinfos["chunks"], tenant_ids)
            if use_web_search:
                try:
                    web_search = create_web_search_provider(prompt_config)
                    web_res = await thread_pool_exec(
                        web_search.retrieve_chunks, " ".join(questions)
                    )
                    kbinfos["chunks"].extend(web_res.get("chunks", []))
                    kbinfos["doc_aggs"].extend(web_res.get("doc_aggs", []))
                except Exception:
                    logging.warning(
                        "web search unavailable; continuing with internal knowledge"
                    )
            if prompt_config.get("use_kg"):
                default_chat_model = get_tenant_default_model_by_type(dialog.tenant_id, LLMType.CHAT)
                kg_bundle_kwargs = {"trace_context": trace_context, "langfuse_session_id": session_id}
                if grounding_enabled:
                    kg_bundle_kwargs["disable_langfuse"] = True
                ck = await settings.kg_retriever.retrieval(
                    " ".join(questions), tenant_ids, dialog.kb_ids, embd_mdl, LLMBundle(dialog.tenant_id, default_chat_model, **kg_bundle_kwargs)
                )
                if ck["content_with_weight"]:
                    kbinfos["chunks"].insert(0, ck)

    if include_reference_metadata:
        logging.debug(
            "reference_metadata enrichment enabled for async_chat: chunk_count=%d metadata_fields=%s",
            len(kbinfos.get("chunks", [])),
            metadata_fields,
        )
        _enrich_chunks_with_document_metadata(kbinfos.get("chunks", []), metadata_fields)

    if gateway_doc_ids is not None:
        kbinfos = filter_kbinfos_to_doc_ids(
            kbinfos, gateway_doc_ids, allow_web=use_web_search
        )

    knowledges = kb_prompt(kbinfos, max_tokens)
    knowledges = _prepend_scope_identity_knowledge(
        knowledges,
        scope_id_sources,
        reject_values=(last_user,),
    )
    retrieved_knowledge_count = len(kbinfos.get("chunks", []))
    if grounding_enabled:
        logging.debug("retrieval completed: grounding_version=%s knowledge_count=%d", 1, len(knowledges))
    else:
        logging.debug("{}->{}".format(" ".join(questions), "\n->".join(knowledges)))

    retrieval_ts = timer()
    if not knowledges and prompt_config.get("empty_response") and not messages[-1].get("files"):
        empty_res = prompt_config["empty_response"]
        if grounding_enabled:
            logging.info(
                "grounding prompt fit: retrieved_knowledge_count=%d included_knowledge_count=0 effective_knowledge_length=0",
                retrieved_knowledge_count,
            )
        else:
            logging.debug("async_chat empty_response path: empty_res=%r tts_mdl=%r", empty_res, tts_mdl)
        # HTML-escape for frontend display so DOMPurify does not strip
        # unknown tags (e.g. <abc> → &lt;abc&gt;), which would otherwise
        # leave the content blank and stall the UI on "Searching…".
        # The raw value is still used for TTS (which has its own tag-
        # stripping in clean_tts_text).
        escaped_answer = html.escape(empty_res)
        if grounding_enabled:
            logging.info(
                "grounding answer finalized: answer_length=%d contains_empty_response=%s",
                len(STANDARD_ABSTAIN_ANSWER),
                True,
            )
            yield _grounding_abstain_event()
            return
        yield {"answer": escaped_answer, "reference": {}, "prompt": "", "audio_binary": None, "final": False}
        yield {
            "answer": escaped_answer,
            "reference": kbinfos,
            "prompt": "\n\n### Query:\n%s" % " ".join(questions),
            "audio_binary": tts(tts_mdl, empty_res),
            "final": True,
        }
        return

    gen_conf = dialog.llm_setting
    grounding_start = grounding_end = None
    grounding_prompt_unfit = False
    while True:
        # Only overwrite kwargs["knowledge"] when retrieval produced something;
        # otherwise preserve any caller-supplied value.
        knowledge_text = "\n\n------\n\n".join(knowledges)
        if knowledge_text:
            kwargs["knowledge"] = "\n------\n" + knowledge_text
        else:
            kwargs.setdefault("knowledge", "")

        if grounding_enabled and kwargs.get("knowledge"):
            knowledge_body = str(kwargs["knowledge"])
            if grounding_start is None:
                grounding_start, grounding_end = _grounding_markers(knowledge_body)
            kwargs["knowledge"] = f"{grounding_start}{knowledge_body}{grounding_end}"

        system_content = prompt_config["system"].format(**kwargs) + attachments_
        # If knowledge was retrieved but the template has no {knowledge}
        # placeholder, auto-append it so the LLM still sees the context.
        if knowledges and "{knowledge}" not in prompt_config.get("system", ""):
            system_content += kwargs["knowledge"]
        msg = [{"role": "system", "content": system_content}]
        msg.extend([{"role": m["role"], "content": re.sub(r"##\d+\$\$", "", m["content"])} for m in messages if m["role"] != "system"])
        used_token_count, msg = message_fit_in(msg, int(max_tokens * 0.95))
        prompt = msg[0]["content"]
        if not grounding_enabled or not knowledges or grounding_start in prompt:
            break
        if len(knowledges) == 1:
            knowledges = []
            grounding_prompt_unfit = True
            break
        knowledges = knowledges[:-1]

    effective_knowledge = _extract_effective_knowledge(prompt, grounding_start, grounding_end)
    if grounding_enabled:
        logging.info(
            "grounding prompt fit: retrieved_knowledge_count=%d included_knowledge_count=%d effective_knowledge_length=%d",
            retrieved_knowledge_count,
            len(knowledges),
            len(effective_knowledge),
        )

    if grounding_prompt_unfit:
        logging.info(
            "grounding answer finalized: answer_length=%d contains_empty_response=%s",
            len(STANDARD_ABSTAIN_ANSWER),
            True,
        )
        yield _grounding_abstain_event()
        return

    prompt4citation = ""
    if knowledges and (prompt_config.get("quote", True) and kwargs.get("quote", True)):
        prompt4citation = citation_prompt()
    if llm_model_config["model_type"] == "chat" and image_attachments:
        convert_last_user_msg_to_multimodal(msg, image_attachments, factory)
    assert len(msg) >= 2, f"message_fit_in has bug: {msg}"

    if "max_tokens" in gen_conf:
        gen_conf["max_tokens"] = min(gen_conf["max_tokens"], max_tokens - used_token_count)

    async def decorate_answer(answer, *, allow_short_retry: bool = True):
        nonlocal embd_mdl, prompt_config, knowledges, kwargs, kbinfos, prompt, retrieval_ts, questions, langfuse_generation

        system_prompt_snapshot = prompt
        refs = []
        ans = answer.split("</think>")
        think = ""
        if len(ans) == 2:
            think = ans[0] + "</think>"
            answer = ans[1]

        if knowledges and (prompt_config.get("quote", True) and kwargs.get("quote", True)):
            idx = set([])
            normalized_answer = normalize_arabic_digits(answer) or ""
            if embd_mdl and not CITATION_MARKER_PATTERN.search(normalized_answer):
                # Main retrieval no longer ships chunk vectors back from ES.
                # Pull them on demand for the chunks we are about to cite.
                await _hydrate_chunk_vectors(retriever, kbinfos.get("chunks", []), tenant_ids, dialog.kb_ids)
                answer, idx = retriever.insert_citations(
                    answer,
                    [ck["content_ltks"] for ck in kbinfos["chunks"]],
                    [ck["vector"] for ck in kbinfos["chunks"]],
                    embd_mdl,
                    tkweight=1 - dialog.vector_similarity_weight,
                    vtweight=dialog.vector_similarity_weight,
                )
            else:
                for match in CITATION_MARKER_PATTERN.finditer(normalized_answer):
                    i = int(match.group(1))
                    if i < len(kbinfos["chunks"]):
                        idx.add(i)

            answer, idx = repair_bad_citation_formats(answer, kbinfos, idx)

            idx = set([kbinfos["chunks"][int(i)]["doc_id"] for i in idx])
            recall_docs = [d for d in kbinfos["doc_aggs"] if d["doc_id"] in idx]
            if not recall_docs:
                recall_docs = kbinfos["doc_aggs"]
            kbinfos["doc_aggs"] = recall_docs

            refs = deepcopy(kbinfos)
            for c in refs["chunks"]:
                if c.get("vector"):
                    del c["vector"]

        if answer.lower().find("invalid key") >= 0 or answer.lower().find("invalid api") >= 0:
            answer += " Please set LLM API-Key in 'User Setting -> Model providers -> API-Key'"
        if grounding_enabled:
            empty_res = prompt_config.get("empty_response", "")
            logging.info(
                "grounding answer finalized: answer_length=%d contains_empty_response=%s",
                len(think + answer),
                bool(empty_res and empty_res in (think + answer)),
            )
        finish_chat_ts = timer()

        total_time_cost = (finish_chat_ts - chat_start_ts) * 1000
        check_llm_time_cost = (check_llm_ts - chat_start_ts) * 1000
        check_langfuse_tracer_cost = (check_langfuse_tracer_ts - check_llm_ts) * 1000
        bind_embedding_time_cost = (bind_models_ts - check_langfuse_tracer_ts) * 1000
        refine_question_time_cost = (refine_question_ts - bind_models_ts) * 1000
        retrieval_time_cost = (retrieval_ts - refine_question_ts) * 1000
        generate_result_time_cost = (finish_chat_ts - retrieval_ts) * 1000

        tk_num = num_tokens_from_string(think + answer)
        prompt += "\n\n### Query:\n%s" % " ".join(questions)
        prompt = (
            f"{prompt}\n\n"
            "## Time elapsed:\n"
            f"  - Total: {total_time_cost:.1f}ms\n"
            f"  - Check LLM: {check_llm_time_cost:.1f}ms\n"
            f"  - Check Langfuse tracer: {check_langfuse_tracer_cost:.1f}ms\n"
            f"  - Bind models: {bind_embedding_time_cost:.1f}ms\n"
            f"  - Query refinement(LLM): {refine_question_time_cost:.1f}ms\n"
            f"  - Retrieval: {retrieval_time_cost:.1f}ms\n"
            f"  - Generate answer: {generate_result_time_cost:.1f}ms\n\n"
            "## Token usage:\n"
            f"  - Generated tokens(approximately): {tk_num}\n"
            f"  - Token speed: {int(tk_num / (generate_result_time_cost / 1000.0))}/s"
        )

        # Add a condition check to call the end method only if langfuse_generation exists
        if langfuse_generation is not None:
            if grounding_enabled:
                langfuse_output = {"grounding_version": 1, "created_at": time.time()}
            else:
                langfuse_output = "\n" + re.sub(r"^.*?(### Query:.*)", r"\1", prompt, flags=re.DOTALL)
                langfuse_output = {"time_elapsed:": re.sub(r"\n", "  \n", langfuse_output), "created_at": time.time()}
            langfuse_generation.update(
                output=langfuse_output,
                usage_details={
                    "input": used_token_count,
                    "output": tk_num,
                    "total": used_token_count + tk_num,
                },
            )
            langfuse_generation.end()

        payload = {
            "answer": think + answer,
            "reference": refs,
            "prompt": "" if grounding_enabled else re.sub(r"\n", "  \n", prompt),
            "created_at": time.time(),
        }
        if grounding_enabled:
            payload, fuse_result = _fuse_or_keep(
                payload,
                effective_knowledge=effective_knowledge,
                attachment_observations=attachment_observations,
                allowed_identifiers=allowed_identifiers,
            )
            if _should_short_fuse_retry(
                fuse_result,
                effective_knowledge=effective_knowledge,
                allow_short_retry=allow_short_retry,
            ):
                retry_raw = await _generate_short_fuse_retry(
                    chat_mdl,
                    prompt=system_prompt_snapshot,
                    prompt4citation=prompt4citation,
                    msg=msg,
                    gen_conf=gen_conf,
                    model_type=llm_model_config["model_type"],
                    image_files=image_files,
                )
                return await decorate_answer(retry_raw, allow_short_retry=False)
        return payload

    if langfuse_tracer:
        try:
            observation_input = {"grounding_version": 1} if grounding_enabled else {"prompt": prompt, "prompt4citation": prompt4citation, "messages": msg}
            observation_kwargs = {
                "as_type": "generation",
                "trace_context": trace_context,
                "name": "chat",
                "model": llm_model_config["llm_name"],
                "input": observation_input,
            }
            if session_id:
                with propagate_attributes(session_id=session_id):
                    langfuse_generation = langfuse_tracer.start_observation(**observation_kwargs)
            else:
                langfuse_generation = langfuse_tracer.start_observation(**observation_kwargs)
        except Exception as e:  # noqa: BLE001 - tracing must not break chat flow
            logger.warning("Langfuse start_observation failed; continuing without tracing: %s", e)
            langfuse_tracer = None
            langfuse_generation = None

    if stream:
        if llm_model_config["model_type"] == "chat":
            stream_iter = chat_mdl.async_chat_streamly_delta(prompt + prompt4citation, msg[1:], gen_conf)
        else:
            stream_iter = chat_mdl.async_chat_streamly_delta(prompt + prompt4citation, msg[1:], gen_conf, images=image_files)
        last_state = None
        async for kind, value, state in _stream_with_think_delta(stream_iter):
            last_state = state
            if _buffer_candidate_tokens(grounding_enabled):
                continue
            if kind == "marker":
                flags = {"start_to_think": True} if value == "<think>" else {"end_to_think": True}
                yield {"answer": "", "reference": {}, "audio_binary": None, "final": False, **flags}
                continue
            yield {"answer": value, "reference": {}, "audio_binary": tts(tts_mdl, value), "final": False}
        full_answer = last_state.full_text if last_state else ""
        if full_answer:
            final = await decorate_answer(_extract_visible_answer(thought + full_answer))
            final["final"] = True
            final["audio_binary"] = None
            if not grounding_enabled:
                final["answer"] = ""
            yield final
        elif grounding_enabled:
            logging.info("grounding answer finalized: answer_length=0 contains_empty_response=False")
            yield _grounding_abstain_event()
    else:
        if llm_model_config["model_type"] == "chat":
            answer = await chat_mdl.async_chat(prompt + prompt4citation, msg[1:], gen_conf)
        else:
            answer = await chat_mdl.async_chat(prompt + prompt4citation, msg[1:], gen_conf, images=image_files)
        user_content = msg[-1].get("content", "[content not available]")
        if not grounding_enabled:
            logging.debug("User: {}|Assistant: {}".format(user_content, answer))
        res = await decorate_answer(answer)
        res["audio_binary"] = tts(tts_mdl, res.get("answer") or "")
        yield res

    return


async def use_sql(question, field_map, tenant_id, chat_mdl, quota=True, kb_ids=None, doc_ids=None):
    """Answer a natural-language question by generating and executing SQL against the document index.

    Detects the active document engine (Infinity, OceanBase, or Elasticsearch), asks the
    chat model to produce the appropriate SQL, injects a validated kb_id filter, executes
    the query, and returns formatted results with optional source citations.

    Args:
        question: Natural-language question from the user.
        field_map: Mapping of field names to types describing the indexed document schema.
        tenant_id: Tenant identifier used to derive the target index/table name.
        chat_mdl: LLM bundle used to generate SQL from the question.
        quota: Whether to enforce token-quota checks (default True).
        kb_ids: Optional list of knowledge-base UUIDs to restrict the query scope.
        doc_ids: Optional list of document UUIDs to restrict the query scope.

    Returns:
        A dict with keys ``answer`` (formatted response string), ``reference``
        (dict of supporting document chunks and doc_aggs), and ``prompt``
        (the system prompt used), or ``None`` if SQL generation or execution fails.
    """
    logging.debug(f"use_sql: Question: {question}")

    # Determine which document engine we're using
    if settings.DOC_ENGINE_INFINITY:
        doc_engine = "infinity"
    elif settings.DOC_ENGINE_OCEANBASE:
        doc_engine = "oceanbase"
    else:
        doc_engine = "es"

    def _assert_valid_uuid(value: str, label: str = "id") -> None:
        if label == "doc_id" and str(value) == "-999":
            return
        try:
            uuid.UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            logger.warning("SQL injection guard rejected invalid %s value (length=%d)", label, len(str(value)))
            raise ValueError(f"Invalid {label} format: {value!r}")

    if isinstance(doc_ids, str):
        doc_ids = [doc_id for doc_id in doc_ids.split(",") if doc_id]
    else:
        doc_ids = [doc_id for doc_id in doc_ids or [] if doc_id]

    # Construct the full table name
    # For Elasticsearch: ragflow_{tenant_id} (kb_id is in WHERE clause)
    # For Infinity: ragflow_{tenant_id}_{kb_id} (each KB has its own table)
    base_table = index_name(tenant_id)
    if doc_engine == "infinity" and kb_ids and len(kb_ids) == 1:
        # Infinity: append kb_id to table name — validate before interpolating
        _assert_valid_uuid(kb_ids[0], "kb_id")
        table_name = f"{base_table}_{kb_ids[0]}"
        logging.debug(f"use_sql: Using Infinity table name: {table_name}")
    else:
        # Elasticsearch/OpenSearch: use base index name
        table_name = base_table
        logging.debug(f"use_sql: Using ES/OS table name: {table_name}")

    expected_doc_name_column = "docnm" if doc_engine == "infinity" else "docnm_kwd"

    def has_source_columns(columns):
        """Return True if the result set contains the columns needed to build source citations."""
        normalized_names = {str(col.get("name", "")).lower() for col in columns}
        return "doc_id" in normalized_names and bool({"docnm_kwd", "docnm"} & normalized_names)

    def is_aggregate_sql(sql_text):
        """Return True if *sql_text* contains an aggregate function (COUNT, SUM, AVG, MAX, MIN, DISTINCT)."""
        return bool(re.search(r"(count|sum|avg|max|min|distinct)\s*\(", (sql_text or "").lower()))

    def normalize_sql(sql):
        """Strip LLM artefacts from *sql* and return a clean, executable SQL string.

        Removes ``<think>`` reasoning blocks, Chinese reasoning markers, markdown
        code fences, and trailing semicolons that some engines reject.
        """
        logging.debug(f"use_sql: Raw SQL from LLM: {repr(sql[:500])}")
        # Remove think blocks if present (format: </think>...)
        sql = re.sub(r"</think>\n.*?\n\s*", "", sql, flags=re.DOTALL)
        sql = re.sub(r"思考\n.*?\n", "", sql, flags=re.DOTALL)
        # Remove markdown code blocks (```sql ... ```)
        sql = re.sub(r"```(?:sql)?\s*", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"```\s*$", "", sql, flags=re.IGNORECASE)
        # Remove trailing semicolon that ES SQL parser doesn't like
        return sql.rstrip().rstrip(";").strip()

    def add_kb_filter(sql):
        """Inject validated scope filters into *sql*.

        Infinity encodes single-KB scope in the table name, so only document
        scope is injected there. All ids are validated before interpolation.
        """
        scope_filters = []
        sql_lower = sql.lower()
        if doc_engine != "infinity" and kb_ids and "kb_id =" not in sql_lower and "kb_id=" not in sql_lower:
            for kid in kb_ids:
                _assert_valid_uuid(kid, "kb_id")
            if len(kb_ids) == 1:
                scope_filters.append(f"kb_id = '{kb_ids[0]}'")
            else:
                scope_filters.append("(" + " OR ".join([f"kb_id = '{kid}'" for kid in kb_ids]) + ")")
        if doc_ids:
            for doc_id in doc_ids:
                _assert_valid_uuid(doc_id, "doc_id")
            if len(doc_ids) == 1:
                scope_filters.append(f"doc_id = '{doc_ids[0]}'")
            else:
                scope_filters.append("(" + " OR ".join([f"doc_id = '{doc_id}'" for doc_id in doc_ids]) + ")")
        if not scope_filters:
            return sql

        scope_filter = " and ".join(scope_filters)
        trailing_clause = re.search(r"\b(group\s+by|having|order\s+by|limit|offset)\b", sql, flags=re.IGNORECASE)
        insert_pos = trailing_clause.start() if trailing_clause else len(sql)

        if not re.search(r"\bwhere\b", sql, flags=re.IGNORECASE):
            sql = sql[:insert_pos].rstrip() + f" WHERE {scope_filter}" + (" " + sql[insert_pos:] if trailing_clause else "")
        else:
            sql = sql[:insert_pos].rstrip() + f" and {scope_filter}" + (" " + sql[insert_pos:] if trailing_clause else "")
        return sql

    def is_row_count_question(q: str) -> bool:
        """Return True if *q* is asking for a total row count of a dataset or table."""
        q = (q or "").lower()
        if not re.search(r"\bhow many rows\b|\bnumber of rows\b|\brow count\b", q):
            return False
        return bool(re.search(r"\bdataset\b|\btable\b|\bspreadsheet\b|\bexcel\b", q))

    # Generate engine-specific SQL prompts
    if doc_engine == "infinity":
        # Build Infinity prompts with JSON extraction context
        json_field_names = list(field_map.keys())
        row_count_override = f"SELECT COUNT(*) AS rows FROM {table_name}" if is_row_count_question(question) else None
        sys_prompt = """You are a Database Administrator. Write SQL for a table with JSON 'chunk_data' column.

JSON Extraction: json_extract_string(chunk_data, '$.FieldName')
Numeric Cast: CAST(json_extract_string(chunk_data, '$.FieldName') AS INTEGER/FLOAT)
NULL Check: json_extract_isnull(chunk_data, '$.FieldName') == false

RULES:
1. Use EXACT field names (case-sensitive) from the list below
2. For SELECT: include doc_id, docnm, and json_extract_string() for requested fields
3. For COUNT: use COUNT(*) or COUNT(DISTINCT json_extract_string(...))
4. Add AS alias for extracted field names
5. DO NOT select 'content' field
6. Only add NULL check (json_extract_isnull() == false) in WHERE clause when:
   - Question asks to "show me" or "display" specific columns
   - Question mentions "not null" or "excluding null"
   - Add NULL check for count specific column
   - DO NOT add NULL check for COUNT(*) queries (COUNT(*) counts all rows including nulls)
7. json_extract_string() returns JSON-quoted strings ("value"), so WHERE comparisons MUST wrap values in double-quotes inside single-quotes (no spaces between quotes): '"value"' (e.g. WHERE json_extract_string(chunk_data, '$.name') = '"Alice"')
8. For partial text search, use LIKE with wildcards: '"%value%"' (e.g. WHERE json_extract_string(chunk_data, '$.name') LIKE '"%Alice%"')
9. Output ONLY the SQL, no explanations"""
        user_prompt = """Table: {}
Fields (EXACT case): {}
{}
Question: {}
Write SQL using json_extract_string() with exact field names. Include doc_id, docnm for data queries. Only SQL.""".format(
            table_name, ", ".join(json_field_names), "\n".join([f"  - {field}" for field in json_field_names]), question
        )
    elif doc_engine == "oceanbase":
        # Build OceanBase prompts with JSON extraction context
        json_field_names = list(field_map.keys())
        row_count_override = f"SELECT COUNT(*) AS rows FROM {table_name}" if is_row_count_question(question) else None
        sys_prompt = """You are a Database Administrator. Write SQL for a table with JSON 'chunk_data' column.

JSON Extraction: json_extract_string(chunk_data, '$.FieldName')
Numeric Cast: CAST(json_extract_string(chunk_data, '$.FieldName') AS INTEGER/FLOAT)
NULL Check: json_extract_isnull(chunk_data, '$.FieldName') == false

RULES:
1. Use EXACT field names (case-sensitive) from the list below
2. For SELECT: include doc_id, docnm_kwd, and json_extract_string() for requested fields
3. For COUNT: use COUNT(*) or COUNT(DISTINCT json_extract_string(...))
4. Add AS alias for extracted field names
5. DO NOT select 'content' field
6. Only add NULL check (json_extract_isnull() == false) in WHERE clause when:
   - Question asks to "show me" or "display" specific columns
   - Question mentions "not null" or "excluding null"
   - Add NULL check for count specific column
   - DO NOT add NULL check for COUNT(*) queries (COUNT(*) counts all rows including nulls)
7. Output ONLY the SQL, no explanations"""
        user_prompt = """Table: {}
Fields (EXACT case): {}
{}
Question: {}
Write SQL using json_extract_string() with exact field names. Include doc_id, docnm_kwd for data queries. Only SQL.""".format(
            table_name, ", ".join(json_field_names), "\n".join([f"  - {field}" for field in json_field_names]), question
        )
    else:
        # Build ES/OS prompts with direct field access
        row_count_override = None
        sys_prompt = """You are a Database Administrator. Write SQL queries.

RULES:
1. Use EXACT field names from the schema below (e.g., product_tks, not product)
2. Quote field names starting with digit: "123_field"
3. Add IS NOT NULL in WHERE clause when:
   - Question asks to "show me" or "display" specific columns
4. Include doc_id/docnm in non-aggregate statement
5. Output ONLY the SQL, no explanations"""
        user_prompt = """Table: {}
Available fields:
{}
Question: {}
Write SQL using exact field names above. Include doc_id, docnm_kwd for data queries. Only SQL.""".format(table_name, "\n".join([f"  - {k} ({v})" for k, v in field_map.items()]), question)

    tried_times = 0

    async def get_table(custom_user_prompt=None):
        nonlocal sys_prompt, user_prompt, question, tried_times, row_count_override
        if row_count_override and custom_user_prompt is None:
            sql = row_count_override
        else:
            prompt = custom_user_prompt if custom_user_prompt is not None else user_prompt
            sql = await chat_mdl.async_chat(sys_prompt, [{"role": "user", "content": prompt}], {"temperature": 0.06})
        sql = normalize_sql(sql)
        sql = add_kb_filter(sql)

        logging.debug(f"{question} get SQL(refined): {sql}")
        tried_times += 1
        logging.debug(f"use_sql: Executing SQL retrieval (attempt {tried_times})")
        tbl = settings.retriever.sql_retrieval(sql, format="json")
        if tbl is None:
            logging.debug("use_sql: SQL retrieval failed (returned None)")
            return None, sql
        row_count = len(tbl.get("rows", []))
        if row_count == 0:
            logging.debug("use_sql: SQL execution succeeded but returned 0 rows")
        else:
            logging.debug(f"use_sql: SQL retrieval completed, got {row_count} rows")
        return tbl, sql

    async def repair_table_for_missing_source_columns(previous_sql):
        if doc_engine in ("infinity", "oceanbase"):
            json_field_names = list(field_map.keys())
            repair_prompt = """Table name: {};
JSON fields available in 'chunk_data' column (use exact names):
{}

Question: {}
Previous SQL:
{}

The previous SQL result is missing required source columns for citations.
Rewrite SQL to keep the same query intent and include doc_id and {} in the SELECT list.
For extracted JSON fields, use json_extract_string(chunk_data, '$.field_name').
Return ONLY SQL.""".format(table_name, "\n".join([f"  - {field}" for field in json_field_names]), question, previous_sql, expected_doc_name_column)
        else:
            repair_prompt = """Table name: {}
Available fields:
{}

Question: {}
Previous SQL:
{}

The previous SQL result is missing required source columns for citations.
Rewrite SQL to keep the same query intent and include doc_id and docnm_kwd in the SELECT list.
Return ONLY SQL.""".format(table_name, "\n".join([f"  - {k} ({v})" for k, v in field_map.items()]), question, previous_sql)
        return await get_table(custom_user_prompt=repair_prompt)

    try:
        tbl, sql = await get_table()
        logging.debug(f"use_sql: Initial SQL execution SUCCESS. SQL: {sql}")
        logging.debug(f"use_sql: Retrieved {len(tbl.get('rows', []))} rows, columns: {[c['name'] for c in tbl.get('columns', [])]}")
    except Exception as e:
        logging.warning(f"use_sql: Initial SQL execution FAILED with error: {e}")
        # Build retry prompt with error information
        if doc_engine in ("infinity", "oceanbase"):
            # Build Infinity error retry prompt
            json_field_names = list(field_map.keys())
            user_prompt = """
Table name: {};
JSON fields available in 'chunk_data' column (use these exact names in json_extract_string):
{}

Question: {}
Please write the SQL using json_extract_string(chunk_data, '$.field_name') with the field names from the list above. Only SQL, no explanations.


The SQL error you provided last time is as follows:
{}

Please correct the error and write SQL again using json_extract_string(chunk_data, '$.field_name') syntax with the correct field names. Only SQL, no explanations.
""".format(table_name, "\n".join([f"  - {field}" for field in json_field_names]), question, e)
        else:
            # Build ES/OS error retry prompt
            user_prompt = """
        Table name: {};
        Table of database fields are as follows (use the field names directly in SQL):
        {}

        Question are as follows:
        {}
        Please write the SQL using the exact field names above, only SQL, without any other explanations or text.


        The SQL error you provided last time is as follows:
        {}

        Please correct the error and write SQL again using the exact field names above, only SQL, without any other explanations or text.
        """.format(table_name, "\n".join([f"{k} ({v})" for k, v in field_map.items()]), question, e)
        try:
            tbl, sql = await get_table()
            logging.debug(f"use_sql: Retry SQL execution SUCCESS. SQL: {sql}")
            logging.debug(f"use_sql: Retrieved {len(tbl.get('rows', []))} rows on retry")
        except Exception:
            logging.error("use_sql: Retry SQL execution also FAILED, returning None")
            return

    if len(tbl["rows"]) == 0:
        logging.warning(f"use_sql: No rows returned from SQL query, returning None. SQL: {sql}")
        return None

    if not is_aggregate_sql(sql) and not has_source_columns(tbl.get("columns", [])):
        logging.warning(f"use_sql: Non-aggregate SQL missing required source columns; retrying once. SQL: {sql}")
        try:
            repaired_tbl, repaired_sql = await repair_table_for_missing_source_columns(sql)
            if repaired_tbl and len(repaired_tbl.get("rows", [])) > 0 and has_source_columns(repaired_tbl.get("columns", [])):
                tbl, sql = repaired_tbl, repaired_sql
                logging.info(f"use_sql: Source-column SQL repair succeeded. SQL: {sql}")
            else:
                logging.warning(f"use_sql: Source-column SQL repair did not provide required columns. Repaired SQL: {repaired_sql}")
        except Exception as e:
            logging.warning(f"use_sql: Source-column SQL repair failed, returning best-effort answer. Error: {e}")

    logging.debug(f"use_sql: Proceeding with {len(tbl['rows'])} rows to build answer")

    docid_idx = set([ii for ii, c in enumerate(tbl["columns"]) if c["name"].lower() == "doc_id"])
    doc_name_idx = set([ii for ii, c in enumerate(tbl["columns"]) if c["name"].lower() in ["docnm_kwd", "docnm"]])
    kb_id_idx = set([ii for ii, c in enumerate(tbl["columns"]) if c["name"].lower() in ["kb_id", "kb_id_kwd"]])

    logging.debug(f"use_sql: All columns: {[(i, c['name']) for i, c in enumerate(tbl['columns'])]}")
    logging.debug(f"use_sql: docid_idx={docid_idx}, doc_name_idx={doc_name_idx}, kb_id_idx={kb_id_idx}")

    column_idx = [ii for ii in range(len(tbl["columns"])) if ii not in (docid_idx | doc_name_idx | kb_id_idx)]

    logging.debug(f"use_sql: column_idx={column_idx}")
    logging.debug(f"use_sql: field_map={field_map}")

    # Helper function to map column names to display names
    def map_column_name(col_name):
        if col_name.lower() == "count(star)":
            return "COUNT(*)"

        # First, try to extract AS alias from any expression (aggregate functions, json_extract_string, etc.)
        # Pattern: anything AS alias_name
        as_match = re.search(r"\s+AS\s+([^\s,)]+)", col_name, re.IGNORECASE)
        if as_match:
            alias = as_match.group(1).strip("\"'")

            # Use the alias for display name lookup
            if alias in field_map:
                display = field_map[alias]
                return re.sub(r"(/.*|（[^（）]+）)", "", display)
            # If alias not in field_map, try to match case-insensitively
            for field_key, display_value in field_map.items():
                if field_key.lower() == alias.lower():
                    return re.sub(r"(/.*|（[^（）]+）)", "", display_value)
            # Return alias as-is if no mapping found
            return alias

        # Try direct mapping first (for simple column names)
        if col_name in field_map:
            display = field_map[col_name]
            # Clean up any suffix patterns
            return re.sub(r"(/.*|（[^（）]+）)", "", display)

        # Try case-insensitive match for simple column names
        col_lower = col_name.lower()
        for field_key, display_value in field_map.items():
            if field_key.lower() == col_lower:
                return re.sub(r"(/.*|（[^（）]+）)", "", display_value)

        # For aggregate expressions or complex expressions without AS alias,
        # try to replace field names with display names
        result = col_name
        for field_name, display_name in field_map.items():
            # Replace field_name with display_name in the expression
            result = result.replace(field_name, display_name)

        # Clean up any suffix patterns
        result = re.sub(r"(/.*|（[^（）]+）)", "", result)
        return result

    # compose Markdown table
    columns = "|" + "|".join([map_column_name(tbl["columns"][i]["name"]) for i in column_idx]) + ("|Source|" if docid_idx and doc_name_idx else "|")

    line = "|" + "|".join(["------" for _ in range(len(column_idx))]) + ("|------|" if docid_idx and doc_name_idx else "")

    # Build rows ensuring column names match values - create a dict for each row
    # keyed by column name to handle any SQL column order
    rows = []
    for row_idx, r in enumerate(tbl["rows"]):
        row_dict = {tbl["columns"][i]["name"]: r[i] for i in range(len(tbl["columns"])) if i < len(r)}
        if row_idx == 0:
            logging.debug(f"use_sql: First row data: {row_dict}")
        row_values = []
        for col_idx in column_idx:
            col_name = tbl["columns"][col_idx]["name"]
            value = row_dict.get(col_name, " ")
            row_values.append(remove_redundant_spaces(str(value)).replace("None", " "))
        # Add Source column with citation marker if Source column exists
        if docid_idx and doc_name_idx:
            row_values.append(f" ##{row_idx}$$")
        row_str = "|" + "|".join(row_values) + "|"
        if re.sub(r"[ |]+", "", row_str):
            rows.append(row_str)
    if quota:
        rows = "\n".join(rows)
    else:
        rows = "\n".join(rows)
    rows = re.sub(r"T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+Z)?\|", "|", rows)

    if not docid_idx or not doc_name_idx:
        logging.warning(f"use_sql: SQL missing required doc_id or docnm_kwd field. docid_idx={docid_idx}, doc_name_idx={doc_name_idx}. SQL: {sql}")
        # For aggregate queries (COUNT, SUM, AVG, MAX, MIN, DISTINCT), fetch doc_id, docnm_kwd separately
        # to provide source chunks, but keep the original table format answer
        if is_aggregate_sql(sql):
            # Keep original table format as answer
            answer = "\n".join([columns, line, rows])

            # Now fetch doc_id, docnm_kwd to provide source chunks
            # Extract WHERE clause from the original SQL
            where_match = re.search(r"\bwhere\b(.+?)(?:\bgroup by\b|\border by\b|\blimit\b|$)", sql, re.IGNORECASE)
            if where_match:
                where_clause = where_match.group(1).strip()
                # Build a query to get source fields with the same WHERE clause.
                # Single-KB queries can derive kb_id from the dialog, while multi-KB
                # ES/OS queries need the row value for metadata enrichment.
                chunks_kb_column = ", kb_id" if not (kb_ids and len(kb_ids) == 1) else ""
                chunks_sql = f"select doc_id, {expected_doc_name_column}{chunks_kb_column} from {table_name} where {where_clause}"
                # Add LIMIT to avoid fetching too many chunks
                if "limit" not in chunks_sql.lower():
                    chunks_sql += " limit 20"
                logging.debug(f"use_sql: Fetching chunks with SQL: {chunks_sql}")
                try:
                    chunks_tbl = settings.retriever.sql_retrieval(chunks_sql, format="json")
                    if chunks_tbl.get("rows") and len(chunks_tbl["rows"]) > 0:
                        # Build chunks reference - use case-insensitive matching
                        chunks_did_idx = next((i for i, c in enumerate(chunks_tbl["columns"]) if c["name"].lower() == "doc_id"), None)
                        chunks_dn_idx = next((i for i, c in enumerate(chunks_tbl["columns"]) if c["name"].lower() in ["docnm_kwd", "docnm"]), None)
                        chunks_kb_idx = next((i for i, c in enumerate(chunks_tbl["columns"]) if c["name"].lower() in ["kb_id", "kb_id_kwd"]), None)
                        if chunks_did_idx is not None and chunks_dn_idx is not None:
                            chunks = []
                            for r in chunks_tbl["rows"]:
                                chunk = {"doc_id": r[chunks_did_idx], "docnm_kwd": r[chunks_dn_idx]}
                                row_dict = {chunks_tbl["columns"][i]["name"]: r[i] for i in range(len(chunks_tbl["columns"])) if i < len(r)}
                                kb_id = _chunk_kb_id_for_doc(row_dict, kb_ids, chunk["doc_id"])
                                if kb_id:
                                    chunk["kb_id"] = kb_id
                                elif chunks_kb_idx is not None:
                                    chunk["kb_id"] = r[chunks_kb_idx]
                                chunks.append(chunk)
                            # Build doc_aggs
                            doc_aggs = {}
                            for r in chunks_tbl["rows"]:
                                doc_id = r[chunks_did_idx]
                                doc_name = r[chunks_dn_idx]
                                if doc_id not in doc_aggs:
                                    doc_aggs[doc_id] = {"doc_name": doc_name, "count": 0}
                                doc_aggs[doc_id]["count"] += 1
                            doc_aggs_list = [{"doc_id": did, "doc_name": d["doc_name"], "count": d["count"]} for did, d in doc_aggs.items()]
                            logging.debug(f"use_sql: Returning aggregate answer with {len(chunks)} chunks from {len(doc_aggs)} documents")
                            return {"answer": answer, "reference": {"chunks": chunks, "doc_aggs": doc_aggs_list}, "prompt": sys_prompt}
                except Exception as e:
                    logging.warning(f"use_sql: Failed to fetch chunks: {e}")
            # Fallback: return answer without chunks
            return {"answer": answer, "reference": {"chunks": [], "doc_aggs": []}, "prompt": sys_prompt}
        # Fallback to table format for other cases
        return {"answer": "\n".join([columns, line, rows]), "reference": {"chunks": [], "doc_aggs": []}, "prompt": sys_prompt}

    docid_idx = list(docid_idx)[0]
    doc_name_idx = list(doc_name_idx)[0]
    doc_aggs = {}
    for r in tbl["rows"]:
        if r[docid_idx] not in doc_aggs:
            doc_aggs[r[docid_idx]] = {"doc_name": r[doc_name_idx], "count": 0}
        doc_aggs[r[docid_idx]]["count"] += 1

    result = {
        "answer": "\n".join([columns, line, rows]),
        "reference": {
            "chunks": [
                {
                    key: value
                    for key, value in {
                        "doc_id": r[docid_idx],
                        "docnm_kwd": r[doc_name_idx],
                        "kb_id": _chunk_kb_id_for_doc(
                            {tbl["columns"][i]["name"]: r[i] for i in range(len(tbl["columns"])) if i < len(r)},
                            kb_ids,
                            r[docid_idx],
                        ),
                    }.items()
                    if value
                }
                for r in tbl["rows"]
            ],
            "doc_aggs": [{"doc_id": did, "doc_name": d["doc_name"], "count": d["count"]} for did, d in doc_aggs.items()],
        },
        "prompt": sys_prompt,
    }
    logging.debug(f"use_sql: Returning answer with {len(result['reference']['chunks'])} chunks from {len(doc_aggs)} documents")
    return result


def clean_tts_text(text: str) -> str:
    if not text:
        return ""

    logging.debug("clean_tts_text BEFORE: %r", text)

    text = text.encode("utf-8", "ignore").decode("utf-8", "ignore")

    text = re.sub(r"[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F]", "", text)

    emoji_pattern = re.compile(
        "[\U0001f600-\U0001f64f\U0001f300-\U0001f5ff\U0001f680-\U0001f6ff\U0001f1e0-\U0001f1ff\U00002700-\U000027bf\U0001f900-\U0001f9ff\U0001fa70-\U0001faff\U0001fad0-\U0001faff]+", flags=re.UNICODE
    )
    text = emoji_pattern.sub("", text)

    # Strip XML/SSML/HTML-like tags so the TTS engine does not hang on
    # unclosed or unknown markup (e.g. <abc> in empty_response).
    text = re.sub(r"<[^>]*>", "", text)

    text = re.sub(r"\s+", " ", text).strip()

    MAX_LEN = 500
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN]

    logging.debug("clean_tts_text AFTER: %r", text)
    return text


def tts(tts_mdl, text):
    if not tts_mdl or not text:
        return None
    text = clean_tts_text(text)
    if not text:
        return None
    return synthesize_with_cache(tts_mdl, text)


class _ThinkStreamState:
    def __init__(self) -> None:
        self.full_text = ""
        self.last_idx = 0
        self.last_model_full = ""
        self.in_think = False
        self.close_pending = False
        self.pending_after_close = ""
        self.think_buffer = ""
        self.answer_buffer = ""


def _extract_visible_answer(text: str) -> str:
    text = text or ""
    if "</think>" not in text:
        return re.sub(r"</?think>", "", text)

    thought, answer = text.rsplit("</think>", 1)
    thought = re.sub(r"</?think>", "", thought).strip()
    answer = re.sub(r"</?think>", "", answer)
    if not thought:
        return answer
    return f"<think>{thought}</think>{answer}"


async def _stream_with_think_delta(stream_iter, min_tokens: int = 16):
    state = _ThinkStreamState()

    def _emit_text(section: str, text: str):
        if not text:
            return None
        if section == "think":
            return text
        state.answer_buffer += text
        if num_tokens_from_string(state.answer_buffer) >= min_tokens:
            out = state.answer_buffer
            state.answer_buffer = ""
            return out
        return None

    def _flush_think_buffer():
        if not state.think_buffer:
            return None
        out = state.think_buffer
        state.think_buffer = ""
        return out

    def _flush_answer_buffer():
        if not state.answer_buffer:
            return None
        out = state.answer_buffer
        state.answer_buffer = ""
        return out

    async for chunk in stream_iter:
        if not chunk:
            continue
        if chunk.startswith(state.last_model_full):
            new_part = chunk[len(state.last_model_full) :]
            state.last_model_full = chunk
        else:
            new_part = chunk
            state.last_model_full += chunk
        if not new_part:
            continue
        state.full_text += new_part
        pending = new_part

        if state.close_pending and "</think>" not in pending:
            state.close_pending = False
            think_piece = _flush_think_buffer()
            if think_piece is not None:
                yield ("text", think_piece, state)
            state.in_think = False
            yield ("marker", "</think>", state)
            if state.pending_after_close:
                answer_piece = state.pending_after_close
                state.pending_after_close = ""
                out = _emit_text("answer", answer_piece)
                if out is not None:
                    yield ("text", out, state)
            answer_piece = re.sub(r"</?think>", "", pending or "")
            if answer_piece:
                out = _emit_text("answer", answer_piece)
                if out is not None:
                    yield ("text", out, state)
            continue

        while pending:
            open_idx = pending.find("<think>")
            close_idx = pending.find("</think>")

            if open_idx == -1 and close_idx == -1:
                piece = re.sub(r"</?think>", "", pending or "")
                if piece:
                    section = "think" if state.in_think else "answer"
                    out = _emit_text(section, piece)
                    if out is not None:
                        yield ("text", out, state)
                break

            if open_idx != -1 and (close_idx == -1 or open_idx < close_idx):
                before = pending[:open_idx]
                if before:
                    piece = re.sub(r"</?think>", "", before or "")
                    section = "think" if state.in_think else "answer"
                    out = _emit_text(section, piece)
                    if out is not None:
                        yield ("text", out, state)
                pending = pending[open_idx + len("<think>") :]
                if not state.in_think:
                    answer_piece = _flush_answer_buffer()
                    if answer_piece is not None:
                        yield ("text", answer_piece, state)
                    think_piece = _flush_think_buffer()
                    if think_piece is not None:
                        yield ("text", think_piece, state)
                    state.in_think = True
                    yield ("marker", "<think>", state)
                continue

            before = pending[:close_idx]
            after = pending[close_idx + len("</think>") :]
            if before:
                piece = re.sub(r"</?think>", "", before or "")
                section = "think" if state.in_think else "answer"
                out = _emit_text(section, piece)
                if out is not None:
                    yield ("text", out, state)
            after_visible = re.sub(r"</?think>", "", after or "")
            if after_visible.strip():
                think_piece = _flush_think_buffer()
                if think_piece is not None:
                    yield ("text", think_piece, state)
                state.in_think = False
                yield ("marker", "</think>", state)
                pending = after_visible
                continue
            state.close_pending = True
            if after_visible:
                state.pending_after_close += after_visible
            pending = ""
            break

    if state.think_buffer:
        yield ("text", state.think_buffer, state)
        state.think_buffer = ""
    if state.close_pending:
        state.in_think = False
        yield ("marker", "</think>", state)
    if state.answer_buffer:
        yield ("text", state.answer_buffer, state)
        state.answer_buffer = ""
    if state.pending_after_close:
        yield ("text", state.pending_after_close, state)
        state.pending_after_close = ""


async def async_ask(question, kb_ids, tenant_id, chat_llm_name=None, search_config={}, search_id=None):
    doc_ids = search_config.get("doc_ids", [])
    rerank_mdl = None
    kb_ids = search_config.get("kb_ids", kb_ids)
    chat_llm_name = search_config.get("chat_id", chat_llm_name)
    rerank_id = search_config.get("rerank_id", "")
    meta_data_filter = search_config.get("meta_data_filter")
    include_reference_metadata, metadata_fields = _resolve_reference_metadata(search_config)

    kbs = KnowledgebaseService.get_by_ids(kb_ids)
    if not kbs:
        if not kb_ids:
            error = "**ERROR**: No KB selected"
        else:
            error = "**ERROR**: The selected KB is not valid"
        yield {"answer": error, "reference": {}, "final": True}
        return

    embedding_list = list(set([kb.embd_id for kb in kbs]))

    is_knowledge_graph = all([kb.parser_id == ParserType.KG for kb in kbs])
    retriever = settings.retriever if not is_knowledge_graph else settings.kg_retriever
    embd_owner_tenant_id = kbs[0].tenant_id
    embd_model_config = resolve_model_config(embd_owner_tenant_id, LLMType.EMBEDDING, embedding_list[0])
    embd_mdl = LLMBundle(embd_owner_tenant_id, embd_model_config)
    chat_model_config = resolve_model_config(tenant_id, LLMType.CHAT, chat_llm_name)
    chat_mdl = LLMBundle(tenant_id, chat_model_config)
    if rerank_id:
        rerank_model_config = resolve_model_config(tenant_id, LLMType.RERANK, rerank_id)
        rerank_mdl = LLMBundle(tenant_id, rerank_model_config)
    max_tokens = chat_mdl.max_length
    tenant_ids = list(set([kb.tenant_id for kb in kbs]))

    if meta_data_filter:
        doc_ids = await apply_meta_data_filter(
            meta_data_filter,
            None,
            question,
            chat_mdl,
            doc_ids,
            kb_ids=kb_ids,
            metas_loader=lambda: DocMetadataService.get_flatted_meta_by_kbs(kb_ids),
        )

    vector_similarity_weight = search_config.get("vector_similarity_weight", 0.3)
    try:
        full_text_weight = 1 - vector_similarity_weight
    except TypeError:
        full_text_weight = None
    logger.debug(
        "Search async_ask retrieval weight: search_id=%s tenant_id=%s kb_count=%s vector_similarity_weight=%s full_text_weight=%s",
        search_id,
        tenant_id,
        len(kb_ids),
        vector_similarity_weight,
        full_text_weight,
    )

    kbinfos = await retriever.retrieval(
        question=question,
        embd_mdl=embd_mdl,
        tenant_ids=tenant_ids,
        kb_ids=kb_ids,
        page=1,
        page_size=12,
        similarity_threshold=search_config.get("similarity_threshold", 0.1),
        vector_similarity_weight=vector_similarity_weight,
        top=search_config.get("top_k", 1024),
        doc_ids=doc_ids,
        aggs=True,
        rerank_mdl=rerank_mdl,
        rank_feature=label_question(question, kbs),
        trace_id=search_id,
    )
    if include_reference_metadata:
        logging.debug(
            "reference_metadata enrichment enabled for async_ask: chunk_count=%d metadata_fields=%s",
            len(kbinfos.get("chunks", [])),
            metadata_fields,
        )
        _enrich_chunks_with_document_metadata(kbinfos.get("chunks", []), metadata_fields)

    knowledges = kb_prompt(kbinfos, max_tokens)
    sys_prompt = PROMPT_JINJA_ENV.from_string(ASK_SUMMARY).render(knowledge="\n".join(knowledges))

    msg = [{"role": "user", "content": question}]

    async def decorate_answer(answer):
        nonlocal knowledges, kbinfos, sys_prompt
        # Main retrieval no longer ships chunk vectors back from ES. Pull
        # them on demand for the chunks we are about to cite.
        await _hydrate_chunk_vectors(retriever, kbinfos.get("chunks", []), tenant_ids, kb_ids)
        answer, idx = retriever.insert_citations(answer, [ck["content_ltks"] for ck in kbinfos["chunks"]], [ck["vector"] for ck in kbinfos["chunks"]], embd_mdl, tkweight=0.7, vtweight=0.3)
        idx = set([kbinfos["chunks"][int(i)]["doc_id"] for i in idx])
        recall_docs = [d for d in kbinfos["doc_aggs"] if d["doc_id"] in idx]
        if not recall_docs:
            recall_docs = kbinfos["doc_aggs"]
        kbinfos["doc_aggs"] = recall_docs
        refs = deepcopy(kbinfos)
        for c in refs["chunks"]:
            if c.get("vector"):
                del c["vector"]

        if answer.lower().find("invalid key") >= 0 or answer.lower().find("invalid api") >= 0:
            answer += " Please set LLM API-Key in 'User Setting -> Model Providers -> API-Key'"
        refs["chunks"] = chunks_format(refs)
        return {"answer": answer, "reference": refs}

    stream_iter = chat_mdl.async_chat_streamly_delta(sys_prompt, msg, {"temperature": 0.1})
    last_state = None
    async for kind, value, state in _stream_with_think_delta(stream_iter):
        last_state = state
        if kind == "marker":
            flags = {"start_to_think": True} if value == "<think>" else {"end_to_think": True}
            yield {"answer": "", "reference": {}, "final": False, **flags}
            continue
        yield {"answer": value, "reference": {}, "final": False}
    full_answer = last_state.full_text if last_state else ""
    final = await decorate_answer(_extract_visible_answer(full_answer))
    final["final"] = True
    final["answer"] = ""
    yield final


async def gen_mindmap(question, kb_ids, tenant_id, search_config={}):
    meta_data_filter = search_config.get("meta_data_filter", {})
    doc_ids = search_config.get("doc_ids", [])
    rerank_id = search_config.get("rerank_id", "")
    rerank_mdl = None
    kbs = KnowledgebaseService.get_by_ids(kb_ids)
    if not kbs:
        return {"error": "No KB selected"}
    tenant_ids = list(set([kb.tenant_id for kb in kbs]))
    embd_owner_tenant_id = kbs[0].tenant_id
    embd_model_config = resolve_model_config(embd_owner_tenant_id, LLMType.EMBEDDING, kbs[0].embd_id)
    embd_mdl = LLMBundle(embd_owner_tenant_id, embd_model_config)
    chat_id = search_config.get("chat_id", "")
    if chat_id:
        chat_model_config = resolve_model_config(tenant_id, LLMType.CHAT, chat_id)
    else:
        chat_model_config = get_tenant_default_model_by_type(tenant_id, LLMType.CHAT)
    chat_mdl = LLMBundle(tenant_id, chat_model_config)
    if rerank_id:
        rerank_model_config = resolve_model_config(tenant_id, LLMType.RERANK, rerank_id)
        rerank_mdl = LLMBundle(tenant_id, rerank_model_config)

    if meta_data_filter:
        doc_ids = await apply_meta_data_filter(
            meta_data_filter,
            None,
            question,
            chat_mdl,
            doc_ids,
            kb_ids=kb_ids,
            metas_loader=lambda: DocMetadataService.get_flatted_meta_by_kbs(kb_ids),
        )

    ranks = await settings.retriever.retrieval(
        question=question,
        embd_mdl=embd_mdl,
        tenant_ids=tenant_ids,
        kb_ids=kb_ids,
        page=1,
        page_size=12,
        similarity_threshold=search_config.get("similarity_threshold", 0.2),
        vector_similarity_weight=search_config.get("vector_similarity_weight", 0.3),
        top=search_config.get("top_k", 1024),
        doc_ids=doc_ids,
        aggs=False,
        rerank_mdl=rerank_mdl,
        rank_feature=label_question(question, kbs),
    )
    mindmap = MindMapExtractor(chat_mdl)
    mind_map = await mindmap([c["content_with_weight"] for c in ranks["chunks"]])
    return mind_map.output


async def rag_agent(dialog, messages, stream=True, **kwargs):
    prompt_config = dialog.prompt_config or {}
    assert messages[-1]["role"] == "user", "The last content of this conversation is not from user."
    grounding_enabled = _grounding_requested(kwargs.get("grounding_version"))
    if _use_simple_chat(prompt_config, kwargs):
        async for ans in async_chat(dialog, messages, stream, **kwargs):
            yield ans
        return
    model_kwargs = {"langfuse_session_id": kwargs.get("session_id")}
    if grounding_enabled:
        model_kwargs["disable_langfuse"] = True
    kbs, embd_mdl, rerank_mdl, chat_mdl, tts_mdl = get_models(dialog, **model_kwargs)
    use_web_search = _should_use_web_search(prompt_config, kwargs.get("internet"))
    logging.debug("web_search kb=%s configured=%s internet=%r enabled=%s", bool(dialog.kb_ids), has_web_search_provider(prompt_config), kwargs.get("internet"), use_web_search)
    tenant_ids = list(set([kb.tenant_id for kb in kbs]))
    # "reasoning" arrives as "1".."4" mapping to the ordered THINKING_MODES
    # (low, medium, high, ultra); fall back to "medium" on anything else.
    from rag.advanced_rag.harness.config import THINKING_MODES

    _mode_labels = list(THINKING_MODES.keys())
    try:
        _n = int(str(kwargs.get("reasoning")).strip())
        thinking_mode = _mode_labels[_n - 1] if 1 <= _n <= len(_mode_labels) else "medium"
    except (TypeError, ValueError):
        thinking_mode = "medium"

    gen_conf = dialog.llm_setting or {}
    doc_scope = None
    if "doc_ids" in kwargs:
        if isinstance(kwargs["doc_ids"], str):
            doc_scope = [doc_id for doc_id in kwargs["doc_ids"].split(",") if doc_id]
        elif isinstance(kwargs["doc_ids"], list):
            doc_scope = [doc_id for doc_id in kwargs["doc_ids"] if doc_id]
    if "doc_ids" in messages[-1]:
        doc_scope = [doc_id for doc_id in messages[-1]["doc_ids"] if doc_id]
    if dialog.meta_data_filter:
        initial_doc_scope = None if doc_scope is None else list(doc_scope)
        filtered_doc_scope = await apply_meta_data_filter(
            dialog.meta_data_filter,
            None,
            messages[-1].get("content", ""),
            chat_mdl,
            doc_scope,
            kb_ids=dialog.kb_ids,
            metas_loader=lambda: DocMetadataService.get_flatted_meta_by_kbs(dialog.kb_ids),
        )
        if initial_doc_scope is None:
            doc_scope = filtered_doc_scope
        else:
            allowed = set(initial_doc_scope)
            doc_scope = [
                doc_id for doc_id in filtered_doc_scope or [] if doc_id in allowed
            ]

    scope_identifiers = list(kwargs.get("scope_identifiers") or []) or list(
        kwargs.get("allowed_identifiers") or []
    )
    rag_tools = RAGTools(
        tenant_ids,
        chat_mdl,
        embed_mdl=embd_mdl,
        kb_ids=dialog.kb_ids,
        web_search=create_web_search_provider(prompt_config) if use_web_search else None,
        meta_data_filter=dialog.meta_data_filter,
        doc_scope=doc_scope,
        do_refer=False,
        thinking_mode=thinking_mode,
        scope_identifiers=scope_identifiers,
    )

    async def decorate_answer(answer):
        nonlocal rag_tools, messages

        refs = []
        if hasattr(rag_tools, "enforce_doc_scope"):
            rag_tools.kbinfos = rag_tools.enforce_doc_scope(rag_tools.kbinfos)
        ans = answer.split("</think>")
        think = ""
        if len(ans) == 2:
            think = ans[0] + "</think>"
            answer = ans[1]

        idx = set([])
        normalized_answer = normalize_arabic_digits(answer) or ""
        for match in CITATION_MARKER_PATTERN.finditer(normalized_answer):
            i = int(match.group(1))
            if i < len(rag_tools.kbinfos["chunks"]):
                idx.add(i)

        answer, idx = repair_bad_citation_formats(answer, rag_tools.kbinfos, idx)

        doc_ids = set()
        for citation in idx:
            try:
                chunk_index = int(citation)
            except (TypeError, ValueError):
                if citation:
                    doc_ids.add(str(citation))
                continue
            if 0 <= chunk_index < len(rag_tools.kbinfos["chunks"]):
                doc_id = rag_tools.kbinfos["chunks"][chunk_index].get("doc_id")
                if doc_id:
                    doc_ids.add(doc_id)

        recall_docs = [d for d in rag_tools.kbinfos["doc_aggs"] if d["doc_id"] in doc_ids]
        if not recall_docs:
            recall_docs = rag_tools.kbinfos["doc_aggs"]
        rag_tools.kbinfos["doc_aggs"] = recall_docs

        refs = deepcopy(rag_tools.kbinfos) if doc_ids else []
        for c in refs.get("chunks", []) if isinstance(refs, dict) else []:
            if c.get("vector"):
                del c["vector"]

        if answer.lower().find("invalid key") >= 0 or answer.lower().find("invalid api") >= 0:
            answer += " Please set LLM API-Key in 'User Setting -> Model providers -> API-Key'"

        return {"answer": think + answer, "reference": refs, "prompt": "", "created_at": time.time()}

    # The agentic-search graph composes the final cited answer itself, so we
    # stream its tokens straight to the client instead of relaying a tool
    # result through a second outer-LLM pass.

    chat_mdl.bind_tools(None, rag_tools.tools)
    # `rag` composes the full cited answer itself, so treat it as terminal: once
    # the model calls it, stream its result and stop — otherwise the model would
    # have to relay the (citation-bearing) answer through another round, which
    # small models mangle or drop, so the client receives nothing.
    if getattr(chat_mdl, "mdl", None) is not None:
        chat_mdl.mdl.terminal_tools = {"rag"}
    if stream:
        # Surface the agentic pipeline's bracket-tagged progress logs to the
        # client as <think> content, interleaved with the real token stream.
        from rag.advanced_rag.think_log import install_think_log_handler, set_think_log_sink, reset_think_log_sink

        install_think_log_handler()
        event_queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _log_sink(msg):
            try:
                loop.call_soon_threadsafe(event_queue.put_nowait, ("log", msg))
            except RuntimeError:
                pass

        async def _drive_stream():
            try:
                stream_iter = chat_mdl.async_chat_streamly_delta(rag_tools.sys_prompt(), messages, gen_conf)
                async for kind, value, state in _stream_with_think_delta(stream_iter):
                    event_queue.put_nowait(("stream", kind, value, state))
            except Exception:
                logging.exception("rag_agent: agentic stream failed")
            finally:
                event_queue.put_nowait(("stream_done",))

        token = set_think_log_sink(_log_sink, redact_content=grounding_enabled)
        drive = asyncio.create_task(_drive_stream())
        last_state = None
        log_think_open = False
        hold_tokens = _buffer_candidate_tokens(grounding_enabled)
        try:
            while True:
                item = await event_queue.get()
                if item[0] == "log":
                    if hold_tokens:
                        continue
                    if not log_think_open:
                        yield {"answer": "", "reference": {}, "audio_binary": None, "final": False, "start_to_think": True}
                        log_think_open = True
                    yield {"answer": item[1] + "\n", "reference": {}, "audio_binary": None, "final": False}
                    continue
                if item[0] == "stream_done":
                    break
                _, kind, value, state = item
                if state is not None:
                    last_state = state
                if hold_tokens:
                    continue
                # A real stream event follows the logs -> close the log think block.
                if log_think_open:
                    yield {"answer": "", "reference": {}, "audio_binary": None, "final": False, "end_to_think": True}
                    log_think_open = False
                if kind == "marker":
                    flags = {"start_to_think": True} if value == "<think>" else {"end_to_think": True}
                    yield {"answer": "", "reference": {}, "audio_binary": None, "final": False, **flags}
                    continue
                yield {"answer": value, "reference": {}, "audio_binary": tts(tts_mdl, value), "final": False}
            if log_think_open:
                yield {"answer": "", "reference": {}, "audio_binary": None, "final": False, "end_to_think": True}
                log_think_open = False
        finally:
            reset_think_log_sink(token)
            if not drive.done():
                drive.cancel()
            try:
                await drive
            except asyncio.CancelledError:
                pass
            except Exception:
                logging.exception("rag_agent: drive task error")

        full_answer = last_state.full_text if last_state else ""
        if full_answer:
            final = await decorate_answer(_extract_visible_answer(full_answer))
            final["final"] = True
            final["audio_binary"] = None
            yield final
    else:
        answer = await chat_mdl.async_chat(rag_tools.sys_prompt(), messages, gen_conf)
        user_content = messages[-1].get("content", "[content not available]")
        if not grounding_enabled:
            logging.debug("User: {}|Assistant: {}".format(user_content, answer))
        res = await decorate_answer(answer)
        res["audio_binary"] = tts(tts_mdl, answer)
        yield res
    return
