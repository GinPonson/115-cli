"""自有上传协议测试。"""

import hashlib
from pathlib import Path

import httpx

import pytest

from u115_uploader.uploader import (
    MultipartCleanupError,
    OpenUploader,
    UploadCredentialExpiredError,
    UploadError,
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


class FakeCallbackFailureHttpResponse:
    """回调失败响应体：HTTP 200 但 115 业务 code 非零。"""

    content = b'{"state": false, "code": 10001, "message": "quota"}'


class FakeCallbackFailureOssResponse:
    """模拟 oss2 对回调失败响应的包装。"""

    response = FakeCallbackFailureHttpResponse()


class FakeCallbackFailureResult:
    """模拟 OSS 已存储对象、但 115 入库回调业务失败。"""

    status = 200
    resp = FakeCallbackFailureOssResponse()


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


class FakeCallbackFailureBucket(FakeBucket):
    """模拟 OSS 对象已写入、但 115 入库回调业务失败。"""

    def complete_multipart_upload(self, key, upload_id, parts, *, headers=None):
        """返回 HTTP 200 但业务 code 非零的回调结果。"""
        self.completed = True
        self.complete_headers = headers
        self.completed_parts = parts
        return FakeCallbackFailureResult()


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


def test_user_info_parses_space_and_vip() -> None:
    """用户信息应兼容字符串与数字两种数值形态并标准化空间与会员字段。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/open/user/info")
        return httpx.Response(
            200,
            json={
                "state": True,
                "data": {
                    "user_id": 12345,
                    "user_name": "测试用户",
                    "rt_space_info": {
                        "all_total": {"size": "5497558138880"},
                        "all_remain": {"size": 1099511627776},
                        "all_use": {"size": 4398046511104},
                    },
                    "vip_info": {"level_name": "年费VIP", "expire": 1800000000},
                },
            },
        )

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    info = uploader.user_info()

    assert info.user_id == 12345
    assert info.user_name == "测试用户"
    assert info.total_size == 5497558138880
    assert info.remain_size == 1099511627776
    assert info.use_size == 4398046511104
    assert info.vip_level == "年费VIP"
    assert info.vip_expire == 1800000000


def test_move_copy_rename_submit_form_actions() -> None:
    """move/copy/rename 应以 form 字段提交精确 ID，业务失败时抛出 115 消息。"""
    captured: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.url.path, request.content.decode()))
        if request.url.path.endswith("/open/ufile/move"):
            return httpx.Response(200, json={"state": False, "message": "目标目录无效"})
        return httpx.Response(200, json={"state": True})

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(UploadError, match="目标目录无效"):
        uploader.move_entries([1, 2, 3], 9)
    uploader.copy_entries([7], 0, no_dupli=True)
    uploader.rename_entry(5, "新名字.bin")

    assert captured[0] == ("/open/ufile/move", "file_ids=1%2C2%2C3&to_cid=9")
    assert captured[1][0] == "/open/ufile/copy"
    assert "pid=0" in captured[1][1]
    assert "no_dupli=1" in captured[1][1]
    assert captured[2][0] == "/open/ufile/update"
    assert "file_id=5" in captured[2][1]


def test_move_copy_rename_reject_invalid_arguments() -> None:
    """非法 ID 与非法新名称必须在进入网络请求前被拒绝。"""
    uploader = OpenUploader("token", http_client=httpx.Client(transport=httpx.MockTransport(
        lambda _request: pytest.fail("不应发起网络请求"),
    )))

    with pytest.raises(ValueError):
        uploader.move_entries([], 0)
    with pytest.raises(ValueError):
        uploader.move_entries([1, 0], 0)
    with pytest.raises(ValueError):
        uploader.copy_entries([1], -1)
    with pytest.raises(ValueError):
        uploader.rename_entry(0, "名字")
    with pytest.raises(ValueError):
        uploader.rename_entry(1, "")
    with pytest.raises(ValueError):
        uploader.rename_entry(1, "a/b")
    with pytest.raises(ValueError):
        uploader.rename_entry(1, "..")


def test_folder_info_parses_file_entry_with_string_size() -> None:
    """详情接口应容忍字符串数值，并以 paths 末项补全父目录。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["file_id"] == "42"
        return httpx.Response(
            200,
            json={
                "state": True,
                "data": {
                    "file_id": "42",
                    "file_name": "电影.mkv",
                    "pick_code": "pickcode",
                    "sha1": "abc123",
                    "file_size": "4096",
                    "file_category": "1",
                    "paths": [
                        {"file_id": "0", "file_name": "根目录"},
                        {"file_id": "9", "file_name": "媒体"},
                    ],
                },
            },
        )

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    entry = uploader.folder_info(42)

    assert entry.file_id == 42
    assert entry.parent_id == 9
    assert entry.name == "电影.mkv"
    assert entry.is_folder is False
    assert entry.size == 4096
    assert entry.sha1 == "ABC123"
    assert entry.pick_code == "pickcode"


def test_folder_info_marks_folder_entry() -> None:
    """file_category=0 应识别为文件夹且大小固定为 0。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": True,
                "data": {
                    "file_id": "7",
                    "file_name": "目录",
                    "file_category": "0",
                    "file_size": "999",
                },
            },
        )

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    entry = uploader.folder_info(7)

    assert entry.is_folder is True
    assert entry.size == 0
    assert entry.pick_code == ""


def test_download_url_sends_browser_headers_and_uses_file_id_key() -> None:
    """直链必须携带浏览器 UA，并优先取响应中本文件 ID 对应的条目。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/open/folder/get_info"):
            return httpx.Response(200, json={"state": True, "data": {
                "file_id": "42",
                "file_name": "a.bin",
                "pick_code": "pc",
                "file_category": "1",
                "file_size": "3",
                "sha1": "aa",
            }})
        assert request.url.path.endswith("/open/ufile/downurl")
        assert "pick_code=pc" in request.content.decode()
        assert request.headers["User-Agent"].startswith("Mozilla/5.0")
        assert request.headers["Referer"] == "https://115.com/"
        return httpx.Response(200, json={"state": True, "data": {
            "99": {"file_name": "other.bin", "url": {"url": "https://other"}},
            "42": {
                "file_name": "a.bin",
                "file_size": "3",
                "sha1": "aa",
                "url": {"url": "https://cdn/a.bin"},
            },
        }})

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    link = uploader.download_url(42)

    assert link.url == "https://cdn/a.bin"
    assert link.name == "a.bin"
    assert link.size == 3
    assert link.sha1 == "AA"


def test_download_url_rejects_folder_entry() -> None:
    """文件夹没有下载直链，应在读取详情后立即失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": True, "data": {
            "file_id": "1",
            "file_name": "目录",
            "file_category": "0",
        }})

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(UploadError, match="文件夹没有下载直链"):
        uploader.download_url(1)


def test_trash_list_parses_dynamic_keys() -> None:
    """回收站列表应跳过分页保留键，并把动态键条目标准化。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["offset"] == "0"
        assert request.url.params["limit"] == "100"
        return httpx.Response(200, json={"state": True, "data": {
            "offset": 0,
            "limit": 100,
            "count": 2,
            "rb_pass": 0,
            "301": {
                "id": "301",
                "file_name": "删除的文件.bin",
                "type": "1",
                "file_size": "2048",
                "dtime": "1700000000",
                "cid": 9,
                "parent_name": "媒体",
                "pick_code": "pc1",
                "status": "0",
            },
            "302": {
                "id": "302",
                "file_name": "删除的目录",
                "type": "2",
                "dtime": "1700000001",
                "cid": "9",
                "parent_name": "媒体",
                "pick_code": "pc2",
                "status": "-1",
            },
        }})

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    page = uploader.list_trash_page()

    assert page.total == 2
    assert page.next_offset is None
    first, second = page.items
    assert first.trash_id == "301"
    assert first.is_folder is False
    assert first.size == 2048
    assert first.deleted_time == 1700000000
    assert first.parent_cid == 9
    assert first.status == 0
    assert second.is_folder is True
    assert second.size == 0
    assert second.status == -1


def test_list_all_trash_follows_pages_and_deduplicates() -> None:
    """list_all_trash 应跨页读取并按条目 ID 去重。"""
    pages = iter(
        [
            {"state": True, "data": {
                "offset": 0, "limit": 200, "count": 3, "rb_pass": 0,
                "1": {"file_name": "一", "type": "1"},
                "2": {"file_name": "二", "type": "1"},
            }},
            {"state": True, "data": {
                "offset": 2, "limit": 200, "count": 3, "rb_pass": 0,
                "3": {"file_name": "三", "type": "1"},
            }},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(pages))

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    items = uploader.list_all_trash()

    assert [item.trash_id for item in items] == ["1", "2", "3"]


def test_restore_trash_reports_per_item_failures() -> None:
    """恢复接口应检查逐条结果并汇总失败条目，而不是只看整体 state。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.content.decode() == "tid=1%2C2"
        return httpx.Response(200, json={"state": True, "data": {
            "1": {"state": True, "error": "", "errno": 0},
            "2": {"state": False, "error": "条目不存在", "errno": 990001},
        }})

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(UploadError, match="2: 条目不存在"):
        uploader.restore_trash(["1", "2"])


def test_restore_trash_succeeds_when_per_item_data_missing() -> None:
    """整体成功但响应省略逐条 data 时，不得误报为失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"state": True})

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    uploader.restore_trash(["1"])  # 不抛异常即为成功


def test_purge_trash_joins_ids_and_rejects_invalid() -> None:
    """彻底删除应以逗号合并 tid，非法 ID 在请求前被拒绝。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"state": True})

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    uploader.purge_trash(["5", "6"])

    assert captured["body"] == "tid=5%2C6"
    with pytest.raises(ValueError):
        uploader.purge_trash(["abc"])


def test_add_offline_urls_reports_per_link_state() -> None:
    """多链接提交应按行合并，并保留每个链接的独立结果。"""

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "wp_path_id=9" in body
        assert "%0A" in body
        return httpx.Response(200, json={"state": True, "data": [
            {
                "state": True,
                "code": 0,
                "message": "",
                "info_hash": "hash-a",
                "url": "magnet:a",
            },
            {
                "state": False,
                "code": 1,
                "message": "链接无效",
                "info_hash": "",
                "url": "magnet:b",
            },
        ]})

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    results = uploader.add_offline_urls(["magnet:a", "magnet:b"], wp_path_id=9)

    assert results[0].state is True
    assert results[0].info_hash == "hash-a"
    assert results[0].message == ""
    assert results[1].state is False
    assert results[1].message == "链接无效"


def test_add_offline_urls_reject_empty_links() -> None:
    """空链接必须在请求前被拒绝。"""
    uploader = OpenUploader("token", http_client=httpx.Client(transport=httpx.MockTransport(
        lambda _request: pytest.fail("不应发起网络请求"),
    )))

    with pytest.raises(ValueError):
        uploader.add_offline_urls([" "])


def test_offline_list_quota_del_and_clear() -> None:
    """任务列表、配额、删除与清空应正确解析响应并提交参数。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/open/offline/get_task_list"):
            assert request.url.params["page"] == "2"
            return httpx.Response(200, json={"state": True, "data": {
                "page": 2,
                "page_count": 3,
                "count": 25,
                "tasks": [{
                    "info_hash": "h1",
                    "name": "任务一",
                    "size": "1024",
                    "status": 1,
                    "percentDone": 55,
                    "add_time": 1,
                    "last_update": 2,
                    "file_id": "11",
                    "delete_file_id": "12",
                    "url": "magnet:a",
                    "wp_path_id": "9",
                }],
            }})
        if path.endswith("/open/offline/get_quota_info"):
            return httpx.Response(
                200,
                json={"state": True, "data": {"surplus": 3, "used": 7, "count": 10}},
            )
        if path.endswith("/open/offline/del_task"):
            captured["del"] = request.content.decode()
            return httpx.Response(200, json={"state": True})
        assert path.endswith("/open/offline/clear_task")
        captured["clear"] = request.content.decode()
        return httpx.Response(200, json={"state": True})

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    page = uploader.list_offline_tasks_page(page=2)
    assert (page.page, page.page_count, page.count) == (2, 3, 25)
    task = page.tasks[0]
    assert task.info_hash == "h1"
    assert task.name == "任务一"
    assert task.size == 1024
    assert task.status == 1
    assert task.percent_done == 55
    assert task.delete_file_id == "12"

    quota = uploader.offline_quota()
    assert (quota.surplus, quota.used, quota.count) == (3, 7, 10)

    uploader.delete_offline_task("h1", delete_files=True)
    assert "info_hash=h1" in captured["del"]
    assert "del_source_file=1" in captured["del"]

    uploader.clear_offline_tasks(flag=2)
    assert captured["clear"] == "flag=2"
    with pytest.raises(ValueError):
        uploader.clear_offline_tasks(flag=9)


def test_add_offline_torrent_uploads_parses_and_submits(tmp_path: Path) -> None:
    """BT 任务应先上传种子，确认远端文件后解析并提交。"""
    torrent = tmp_path / "sample.torrent"
    torrent_bytes = b"torrent-bytes"
    torrent.write_bytes(torrent_bytes)
    torrent_sha1 = hashlib.sha1(torrent_bytes).hexdigest().upper()
    bucket = FakeBucket()
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/open/upload/init"):
            return httpx.Response(200, json={"state": True, "data": {
                "status": 1,
                "bucket": "bucket",
                "object": "object",
                "callback": {"callback": "cb", "callback_var": "cv"},
            }})
        if path.endswith("/open/upload/get_token"):
            return httpx.Response(200, json={"state": True, "data": {
                "endpoint": "https://oss.example.com",
                "AccessKeyId": "id",
                "AccessKeySecret": "secret",
                "SecurityToken": "sts",
            }})
        if path.endswith("/open/ufile/files"):
            return httpx.Response(200, json={
                "state": True,
                "count": 1,
                "data": [{
                    "fid": "77",
                    "cid": "9",
                    "fn": "sample.torrent",
                    "fs": str(len(torrent_bytes)),
                    "sha1": torrent_sha1,
                }],
            })
        if path.endswith("/open/folder/get_info"):
            return httpx.Response(200, json={"state": True, "data": {
                "file_id": "77",
                "file_name": "sample.torrent",
                "pick_code": "pc",
                "file_category": "1",
                "sha1": torrent_sha1,
                "file_size": str(len(torrent_bytes)),
            }})
        if path.endswith("/open/offline/torrent"):
            captured["parse"] = request.content.decode()
            return httpx.Response(200, json={"state": True, "data": {
                "info_hash": "bt-hash",
                "torrent_name": "sample",
                "file_count": 2,
                "file_size": 1024,
                "torrent_filelist": [],
            }})
        assert path.endswith("/open/offline/add_task_bt")
        captured["bt"] = request.content.decode()
        return httpx.Response(200, json={"state": True})

    uploader = OpenUploader(
        "token",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        bucket_factory=lambda *_args: bucket,
    )
    progress: list[str] = []

    info_hash = uploader.add_offline_torrent(
        torrent,
        wp_path_id=9,
        progress_output=progress.append,
    )

    assert info_hash == "bt-hash"
    # progress_output 必须原样透传给上传流程，保证 CLI 能控制输出风格。
    assert len(progress) == 1
    assert "100.00%" in progress[0]
    assert bucket.content == torrent_bytes
    assert f"torrent_sha1={torrent_sha1}" in captured["parse"]
    assert "pick_code=pc" in captured["parse"]
    assert "info_hash=bt-hash" in captured["bt"]
    assert "save_path=9" in captured["bt"]
    assert "wp_path_id=9" in captured["bt"]


def test_callback_failure_does_not_create_self_referential_cause(tmp_path: Path) -> None:
    """回调业务失败时不得让 __cause__ 指向自身，否则上层异常链遍历会死循环。

    回归背景：``_upload_oss`` 曾以 ``raise upload_error from error`` 收尾，
    而回调失败场景下两者是同一对象，于是形成自环；``_is_retryable`` 沿
    ``__cause__`` 遍历时永不终止，进程进入纯用户态自旋（生产已实际发生）。
    """
    source = tmp_path / "callback-failure.bin"
    source.write_bytes(b"a" * (200 * 1024))
    bucket = FakeCallbackFailureBucket()
    uploader = OpenUploader("token", bucket_factory=lambda *_args: bucket)

    with pytest.raises(UploadError) as captured:
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

    error = captured.value
    # 关键断言：异常链必须是有限且无自环的。
    assert error.__cause__ is not error
    assert bucket.completed is True
