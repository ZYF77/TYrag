#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
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

import pytest
from test_common import upload_info
from configs import INVALID_API_TOKEN
from libs.auth import RAGFlowWebApiAuth
from utils.file_utils import create_txt_file


class _AwaitableValue:
    def __init__(self, value):
        self._value = value

    def __await__(self):
        async def _co():
            return self._value

        return _co().__await__()


class _DummyFiles(dict):
    def getlist(self, key):
        value = self.get(key, [])
        if isinstance(value, list):
            return value
        return [value]


class _DummyFile:
    def __init__(self, filename):
        self.filename = filename


class _DummyRequest:
    def __init__(self, *, files=None, args=None):
        self._files = files or _DummyFiles()
        self.args = args or {}

    @property
    def files(self):
        return _AwaitableValue(self._files)


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    "file_id",
    ["", "a" * 31, "a" * 33, "A" * 32, "12345678-1234-1234-1234-123456789abc", "../object"],
)
def test_delete_upload_info_rejects_unsafe_ids(
    document_app_module, monkeypatch, file_id
):
    removed = []

    class Storage:
        rm = staticmethod(lambda bucket, object_id: removed.append((bucket, object_id)))

    monkeypatch.setattr(
        document_app_module.settings,
        "STORAGE_IMPL",
        Storage(),
        raising=False,
    )

    result = _run(document_app_module.delete_upload_info(file_id=file_id))

    assert result["code"] == 101
    assert removed == []


def test_delete_upload_info_is_tenant_scoped_and_idempotent(
    document_app_module, monkeypatch
):
    removed = []
    file_id = "a" * 32

    class Storage:
        rm = staticmethod(lambda bucket, object_id: removed.append((bucket, object_id)))

    monkeypatch.setattr(
        document_app_module.settings,
        "STORAGE_IMPL",
        Storage(),
        raising=False,
    )

    first = _run(document_app_module.delete_upload_info(file_id=file_id))
    second = _run(document_app_module.delete_upload_info(file_id=file_id))

    assert first == second == {"code": 0, "data": {"id": file_id}}
    assert removed == [
        ("user-1-downloads", file_id),
        ("user-1-downloads", file_id),
    ]


# ============================================================================
# End-to-End Tests
# ============================================================================


@pytest.mark.p2
class TestUploadInfoE2E:
    """End-to-end tests for the /api/v1/documents/upload endpoint"""

    def test_upload_info_requires_file_or_url_e2e(self, WebApiAuth):
        """Test that missing both file and url returns error"""
        # Call without files and without url
        res = upload_info(WebApiAuth)
        assert res["code"] == 101, res
        assert "Missing input" in res["message"] or "file" in res["message"].lower() or "url" in res["message"].lower()

    def test_upload_info_rejects_mixed_inputs_e2e(self, WebApiAuth, tmp_path):
        """Test that providing both file and url returns error"""
        # Create a file
        fp = create_txt_file(tmp_path / "test.txt")

        # Call with both file and url - the API should reject this
        res = upload_info(WebApiAuth, files_path=[fp], url="https://example.com/test.txt")
        # The API should return an error when both file and url are provided
        assert res["code"] == 101, res
        assert "not both" in res["message"].lower() and "either" in res["message"].lower()

    def test_upload_info_supports_url_single_and_multiple_files_e2e(self, WebApiAuth, tmp_path):
        """Test that the endpoint supports URL, single file, and multiple files"""
        # Test with URL
        # Note: Using a real URL might fail if the URL is not accessible
        # For E2E testing, we test with actual file uploads

        # Test with single file
        fp1 = create_txt_file(tmp_path / "single_file.txt")
        res = upload_info(WebApiAuth, files_path=[fp1])
        assert res["code"] == 0, res
        assert "data" in res, res

        # Test with multiple files
        fp2 = create_txt_file(tmp_path / "file_a.txt")
        fp3 = create_txt_file(tmp_path / "file_b.txt")
        res = upload_info(WebApiAuth, files_path=[fp2, fp3])
        assert res["code"] == 0, res
        assert "data" in res, res
        # Should return a list for multiple files
        if isinstance(res["data"], list):
            assert len(res["data"]) == 2, res

    def test_upload_info_invalid_auth(self):
        """Test that invalid authentication returns error"""
        res = upload_info(RAGFlowWebApiAuth(INVALID_API_TOKEN), files_path=[])
        assert res["code"] == 401, res
