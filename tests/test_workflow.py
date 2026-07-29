"""上传后强校验、冲突处理和本地清理测试。"""

from pathlib import Path

import pytest

from u115_uploader.uploader import RemoteFile, UploadError, UploadResult
from u115_uploader.workflow import (
    VerifiedUpload,
    append_manifest,
    finalize_local_source,
    find_verified_remote,
    upload_and_verify,
)


class FakeWorkflowUploader:
    """提供可变远端文件列表的上传工作流替身。"""

    def __init__(self, files: list[RemoteFile]) -> None:
        self.files = files
        self.upload_calls: list[tuple[Path, str | None]] = []

    def list_all_files(self, parent_cid: int) -> list[RemoteFile]:
        """返回指定 CID 的测试文件。"""
        assert parent_cid == 9
        return list(self.files)

    def upload(
        self,
        source: Path,
        *,
        cid: int,
        remote_name: str | None,
        part_size: int,
        progress_output,
    ) -> UploadResult:
        """模拟上传并把与本地内容一致的文件加入远端。"""
        import hashlib

        self.upload_calls.append((source, remote_name))
        sha1 = hashlib.sha1(source.read_bytes()).hexdigest().upper()
        uploaded = RemoteFile(
            file_id=100 + len(self.files),
            parent_cid=cid,
            name=remote_name or source.name,
            size=source.stat().st_size,
            sha1=sha1,
        )
        self.files.append(uploaded)
        return UploadResult(
            instant=False,
            response={},
            file_sha1=sha1,
            target_cid=cid,
            remote_name=uploaded.name,
        )


def test_find_verified_remote_requires_name_size_and_sha1(tmp_path: Path) -> None:
    """远端强校验必须同时匹配名称、大小和 SHA1。"""
    source = tmp_path / "video.bin"
    source.write_bytes(b"content")
    uploader = FakeWorkflowUploader(
        [
            RemoteFile(
                file_id=1,
                parent_cid=9,
                name=source.name,
                size=source.stat().st_size,
                sha1="BAD",
            )
        ]
    )

    with pytest.raises(UploadError, match="大小或 SHA1 不一致"):
        find_verified_remote(uploader, source, parent_cid=9)


def test_upload_and_verify_renames_conflicting_remote_file(tmp_path: Path) -> None:
    """rename 策略应上传到新名称并再次执行强校验。"""
    source = tmp_path / "video.mp4"
    source.write_bytes(b"new")
    uploader = FakeWorkflowUploader(
        [RemoteFile(1, 9, "video.mp4", 3, "OLD")]
    )

    verified = upload_and_verify(
        uploader,
        source,
        parent_cid=9,
        part_size=1024,
        conflict="rename",
        retries=0,
        progress_output=lambda _text: None,
    )

    assert verified.uploaded is True
    assert verified.remote.name == "video (1).mp4"
    assert uploader.upload_calls == [(source, "video (1).mp4")]


def test_finalize_local_source_deletes_only_when_explicit(tmp_path: Path) -> None:
    """本地文件只有在显式开启删除动作后才会被移除。"""
    source = tmp_path / "uploaded.bin"
    source.write_bytes(b"done")

    assert finalize_local_source(
        source,
        delete_after_verify=False,
        move_after_verify=None,
    ) is None
    assert source.exists()

    finalize_local_source(
        source,
        delete_after_verify=True,
        move_after_verify=None,
    )
    assert not source.exists()


def test_manifest_is_json_lines_without_protocol_response(tmp_path: Path) -> None:
    """manifest 应记录可审计字段，但不能写入上传协议响应。"""
    source = tmp_path / "file.txt"
    source.write_text("ok", encoding="utf-8")
    remote = RemoteFile(10, 9, source.name, 2, "SHA1")
    verified = VerifiedUpload(source, remote, True, False)
    manifest = tmp_path / "manifest.jsonl"

    append_manifest(manifest, verified, action="uploaded")

    content = manifest.read_text(encoding="utf-8")
    assert '"file_id": 10' in content
    assert '"action": "uploaded"' in content
    assert "response" not in content
