"""自有上传协议测试。"""

from pathlib import Path

import httpx

import pytest

from u115_uploader.uploader import (
    MultipartCleanupError,
    OpenUploader,
    UploadCredentialExpiredError,
)


class FakePutResult:
    """OSS 分片结果。"""

    etag = "etag"
    crc = 123


class FakeHttpResponse:
    """模拟 oss2 底层响应体。"""

    content = b'{"state":true,"code":0}'


class FakeOssResponse:
    """模拟 oss2 响应包装。"""

    status = 200
    response = FakeHttpResponse()


class FakeCompleteResult:
    """模拟 OSS complete 与 115 callback 同时成功。"""

    status = 200
    resp = FakeOssResponse()


class FakeInitResult:
    """OSS multipart 初始化结果。"""

    upload_id = "upload-id"


class FakeBucket:
    """记录 OSS 操作的 Bucket 替身。"""

    def __init__(self) -> None:
        self.put_headers = None
        self.init_headers = None
        self.init_params = None
        self.complete_headers = None
        self.parts = []
        self.completed = False

    def put_object(self, key, stream, *, headers):
        """记录单 PUT 内容和回调头。"""
        self.key = key
        self.content = stream.read()
        self.put_headers = headers
        return FakeCompleteResult()

    def init_multipart_upload(self, key, *, headers=None, params=None):
        """返回固定 multipart upload id。"""
        self.key = key
        self.init_headers = headers
        self.init_params = params
        return FakeInitResult()

    def upload_part(self, key, upload_id, part_number, chunk):
        """记录上传分片。"""
        self.parts.append((part_number, bytes(chunk)))
        return FakePutResult()

    def complete_multipart_upload(self, key, upload_id, parts, *, headers=None):
        """记录完成请求及其 115 入库回调头。"""
        self.completed = True
        self.complete_headers = headers
        self.completed_parts = parts
        return FakeCompleteResult()

    def abort_multipart_upload(self, key, upload_id):
        """测试正常路径不应调用中止。"""
        raise AssertionError("不应中止成功上传")


class FakeExpiredBucket(FakeBucket):
    """模拟 STS 过期且 multipart 清理也失败。"""

    def upload_part(self, key, upload_id, part_number, chunk):
        """首个分片返回包含敏感字段的 STS 过期错误。"""
        raise RuntimeError({
            "details": {
                "Code": "SecurityTokenExpired",
                "Message": "expired",
                "RequestId": "upload-request",
                "SecurityToken": "secret-upload-token",
            }
        })

    def abort_multipart_upload(self, key, upload_id):
        """清理请求同样返回包含 AccessKey 的错误。"""
        raise RuntimeError({
            "details": {
                "Code": "InvalidAccessKeyId",
                "Message": "invalid",
                "RequestId": "cleanup-request",
                "OSSAccessKeyId": "secret-access-key",
            }
        })


def test_multipart_cleanup_preserves_primary_error_and_redacts_credentials(
    tmp_path: Path,
) -> None:
    """清理失败不得覆盖 STS 过期主因，错误文本也不得泄露临时凭证。"""
    source = tmp_path / "multipart.bin"
    source.write_bytes(b"a" * (200 * 1024))
    bucket = FakeExpiredBucket()
    uploader = OpenUploader("token", bucket_factory=lambda *_args: bucket)

    with pytest.raises(MultipartCleanupError) as captured:
        uploader._upload_oss(
            source,
            bucket_name="bucket",
            object_key="object",
            callback="callback",
            callback_var="vars",
            sts={
                "endpoint": "https://oss.example.com",
                "AccessKeyId": "id",
                "AccessKeySecret": "secret",
                "SecurityToken": "sts",
            },
            part_size=100 * 1024,
            progress_output=lambda _line: None,
        )

    assert isinstance(captured.value.__cause__, UploadCredentialExpiredError)
    message = str(captured.value)
    assert "临时上传凭证已过期" in message
    assert "InvalidAccessKeyId" in message
    assert "secret-upload-token" not in message
    assert "secret-access-key" not in message


def test_upload_sends_preid_and_handles_instant(tmp_path: Path) -> None:
    """首次 init 必须包含 preid；status=2 应直接秒传完成。"""
    source = tmp_path / "instant.bin"
    source.write_bytes(b"data")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"state": True, "data": {"status": 2}})

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = uploader.upload(source)

    assert result.instant is True
    assert "preid=" in captured["body"]
    assert "topupload=0" in captured["body"]
    assert "sign_key=" not in captured["body"]


def test_upload_handles_closed_range_sign_and_single_put(tmp_path: Path) -> None:
    """701/7 应计算闭区间 SHA1，认证成功后使用 OSS callback 上传。"""
    source = tmp_path / "signed.bin"
    source.write_bytes(b"abcdef")
    requests = []
    bucket = FakeBucket()
    responses = iter(
        [
            {"state": True, "data": {
                "status": 7,
                "code": 701,
                "sign_key": "key",
                "sign_check": "1-3",
            }},
            {"state": True, "data": {
                "status": 1,
                "bucket": "bucket",
                "object": "object",
                "callback": {"callback": "callback", "callback_var": "vars"},
            }},
            {"state": True, "data": {
                "endpoint": "https://oss.example.com",
                "AccessKeyId": "id",
                "AccessKeySecret": "secret",
                "SecurityToken": "sts",
            }},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=next(responses))

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        bucket_factory=lambda *_args: bucket,
    )
    result = uploader.upload(source, progress_output=lambda _line: None)

    assert result.instant is False
    assert "sign_key=key" in requests[1].content.decode()
    # SHA1("bcd")，证明 end=3 被包含。
    assert "sign_val=924F61661A3472DA74307A35F2C8D22E07E84A4D" in requests[1].content.decode()
    assert bucket.content == b"abcdef"
    assert "x-oss-callback" in bucket.put_headers


def test_multipart_callback_is_sent_when_completing_upload(tmp_path: Path) -> None:
    """分片 callback 必须随 CompleteMultipartUpload 发送，确保 115 收到入库通知。"""
    source = tmp_path / "multipart.bin"
    source.write_bytes(b"a" * (200 * 1024))
    bucket = FakeBucket()
    responses = iter(
        [
            {
                "state": True,
                "data": {
                    "status": 1,
                    "bucket": "bucket",
                    "object": "object",
                    "callback": {
                        "callback": "callback",
                        "callback_var": "vars",
                    },
                },
            },
            {
                "state": True,
                "data": {
                    "endpoint": "https://oss.example.com",
                    "AccessKeyId": "id",
                    "AccessKeySecret": "secret",
                    "SecurityToken": "sts",
                },
            },
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        bucket_factory=lambda *_args: bucket,
    )

    result = uploader.upload(
        source,
        part_size=100 * 1024,
        progress_output=lambda _line: None,
    )

    assert result.instant is False
    assert bucket.init_headers is None
    assert bucket.init_params == {"sequential": ""}
    assert bucket.completed is True
    assert all(part.part_crc == 123 for part in bucket.completed_parts)
    assert "x-oss-callback" in bucket.complete_headers
    assert "x-oss-callback-var" in bucket.complete_headers


def test_resolve_remote_directory_path_level_by_level() -> None:
    """远端路径必须从根目录开始逐级按目录名称解析 CID。"""
    requests = []
    responses = iter(
        [
            {
                "state": True,
                "count": 2,
                "data": [
                    {"fid": "10", "pid": "0", "fn": "备份", "fc": "0"},
                    {"fid": "99", "pid": "0", "fn": "备份", "fc": "1"},
                ],
            },
            {
                "state": True,
                "count": 1,
                "data": [{"cid": "20", "n": "照片"}],
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=next(responses))

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert uploader.resolve_remote_dir("/备份/照片") == 20
    assert requests[0].url.params["cid"] == "0"
    assert requests[1].url.params["cid"] == "10"


def test_ensure_remote_directory_creates_missing_child() -> None:
    """自动目录解析应保留已有父目录，并通过 Open API 创建缺失子目录。"""
    requests = []
    responses = iter(
        [
            {
                "state": True,
                "count": 1,
                "data": [{"cid": "10", "n": "withny"}],
            },
            {"state": True, "count": 0, "data": []},
            {
                "state": True,
                "data": {"file_id": "20", "file_name": "財木桜"},
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        """记录目录列表与创建请求。"""
        requests.append(request)
        return httpx.Response(200, json=next(responses))

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert uploader.ensure_remote_dir("/withny/財木桜") == 20
    assert requests[2].url.path == "/open/folder/add"
    assert requests[2].content.decode() == "pid=10&file_name=%E8%B2%A1%E6%9C%A8%E6%A1%9C"


def test_list_child_folders_uses_folder_only_filter_and_paginates() -> None:
    """目录列表应使用 nf=1，兼容两种字段形状并按 count 完整翻页。"""
    requests = []
    responses = iter(
        [
            {
                "state": True,
                "count": 3,
                "data": [
                    {"cid": "10", "n": "相册"},
                    {"fid": "file-1", "cid": "0", "n": "普通文件.txt"},
                ],
            },
            {
                "state": True,
                "count": 3,
                "data": [
                    {"fid": "20", "pid": "0", "fn": "视频", "fc": "0"},
                ],
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=next(responses))

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    folders = uploader.list_child_folders(0)

    assert [(folder.cid, folder.parent_cid, folder.name) for folder in folders] == [
        (10, 0, "相册"),
        (20, 0, "视频"),
    ]
    assert requests[0].url.params["nf"] == "1"
    assert requests[1].url.params["offset"] == "2"


def test_search_folders_uses_global_folder_search_shape() -> None:
    """全局文件夹搜索应传 fc=1，并解析 search 端点的完整字段名。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "state": True,
                "count": 1,
                "data": [
                    {
                        "file_id": "88",
                        "parent_id": "9",
                        "file_name": "旅行照片",
                        "file_category": "0",
                    }
                ],
            },
        )

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    folders = uploader.search_folders("旅行")

    assert [(folder.cid, folder.parent_cid, folder.name) for folder in folders] == [
        (88, 9, "旅行照片"),
    ]
    assert captured["search_value"] == "旅行"
    assert captured["fc"] == "1"


def test_list_files_page_returns_pagination_metadata() -> None:
    """文件列表应只请求文件，并根据原始页长度计算下一页 offset。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "state": True,
                "count": 3,
                "data": [
                    {
                        "fid": "101",
                        "cid": "9",
                        "n": "a.txt",
                        "s": "12",
                        "sha": "abc",
                    },
                    {
                        "fid": "102",
                        "cid": "9",
                        "n": "b.txt",
                        "s": "34",
                        "sha": "def",
                    },
                ],
            },
        )

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    page = uploader.list_files_page(9, offset=0, limit=2)

    assert [(item.file_id, item.name, item.size, item.sha1) for item in page.files] == [
        (101, "a.txt", 12, "ABC"),
        (102, "b.txt", 34, "DEF"),
    ]
    assert page.total == 3
    assert page.next_offset == 2
    assert captured["show_dir"] == "0"
    assert captured["limit"] == "2"


def test_search_files_page_uses_file_filter_and_search_schema() -> None:
    """文件搜索应传 fc=2，并解析完整字段名和结束页状态。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "state": True,
                "count": 1,
                "data": [
                    {
                        "file_id": "201",
                        "parent_id": "20",
                        "file_name": "校验文件.bin",
                        "file_size": "4096",
                        "sha1": "123abc",
                        "file_category": "1",
                    }
                ],
            },
        )

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    page = uploader.search_files_page("校验", offset=0, limit=100)

    assert page.files[0].parent_cid == 20
    assert page.files[0].sha1 == "123ABC"
    assert page.next_offset is None
    assert captured["search_value"] == "校验"
    assert captured["fc"] == "2"


def test_trash_file_uses_exact_file_and_parent_ids() -> None:
    """回收站请求必须提交调用方确认的精确文件 ID 与父 CID。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"state": True, "data": ["123"]})

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    uploader.trash_file(123, 9)

    assert captured["url"].endswith("/open/ufile/delete")
    assert "file_ids=123" in captured["body"]
    assert "parent_id=9" in captured["body"]
