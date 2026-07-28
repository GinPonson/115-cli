"""自有上传协议测试。"""

from pathlib import Path

import httpx

from u115_uploader.uploader import OpenUploader


class FakePutResult:
    """OSS 分片结果。"""

    etag = "etag"


class FakeInitResult:
    """OSS multipart 初始化结果。"""

    upload_id = "upload-id"


class FakeBucket:
    """记录 OSS 操作的 Bucket 替身。"""

    def __init__(self) -> None:
        self.put_headers = None
        self.parts = []
        self.completed = False

    def put_object(self, key, stream, *, headers):
        """记录单 PUT 内容和回调头。"""
        self.key = key
        self.content = stream.read()
        self.put_headers = headers

    def init_multipart_upload(self, key, *, headers):
        """返回固定 multipart upload id。"""
        self.key = key
        self.put_headers = headers
        return FakeInitResult()

    def upload_part(self, key, upload_id, part_number, chunk):
        """记录上传分片。"""
        self.parts.append((part_number, bytes(chunk)))
        return FakePutResult()

    def complete_multipart_upload(self, key, upload_id, parts):
        """标记 multipart 完成。"""
        self.completed = True

    def abort_multipart_upload(self, key, upload_id):
        """测试正常路径不应调用中止。"""
        raise AssertionError("不应中止成功上传")


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
