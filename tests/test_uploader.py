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
