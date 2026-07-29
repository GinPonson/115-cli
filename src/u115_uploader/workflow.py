"""上传校验、冲突处理、清理与审计工作流。"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from typing import Literal

import httpx

from .uploader import OpenUploader, RemoteFile, UploadError, UploadResult, _hash_file

ConflictPolicy = Literal["error", "skip", "verify", "rename"]


@dataclass(frozen=True, slots=True)
class VerifiedUpload:
    """本地文件与 115 远端文件的强校验结果。

    Attributes:
        source: 本地源文件绝对路径。
        remote: 大小与 SHA1 均匹配的 115 文件。
        uploaded: 本次是否实际传输了文件。
        instant: 本次实际上传时是否由 115 秒传完成。
    """

    source: Path
    remote: RemoteFile
    uploaded: bool
    instant: bool


def find_verified_remote(
    uploader: OpenUploader,
    source: Path,
    *,
    parent_cid: int,
    remote_name: str | None = None,
) -> RemoteFile:
    """按文件名、大小和 SHA1 在指定 115 目录中强校验本地文件。

    Args:
        uploader: 已认证的 115 客户端。
        source: 要校验的本地普通文件。
        parent_cid: 目标目录 CID。
        remote_name: 可选远端文件名；默认使用本地文件名。

    Returns:
        唯一匹配的远端文件。

    Raises:
        FileNotFoundError: 本地文件或远端匹配项不存在。
        UploadError: 同名项存在但校验不一致，或出现多个完全匹配项。
    """
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"本地普通文件不存在：{source}")
    expected_name = remote_name or source.name
    expected_size = source.stat().st_size
    expected_sha1, _ = _hash_file(source)
    same_name = [
        item for item in uploader.list_all_files(parent_cid)
        if item.name == expected_name
    ]
    matches = [
        item for item in same_name
        if item.size == expected_size and item.sha1 == expected_sha1
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise UploadError(f"115 中存在多个完全相同的文件，无法唯一确认：{expected_name}")
    if same_name:
        raise UploadError(f"115 中存在同名但大小或 SHA1 不一致的文件：{expected_name}")
    raise FileNotFoundError(f"115 目标目录中不存在已校验文件：{expected_name}")


def choose_remote_name(source: Path, existing: list[RemoteFile]) -> str:
    """为 rename 冲突策略生成目标目录中不存在的稳定文件名。

    Args:
        source: 本地源文件，用于取得名称与扩展名。
        existing: 目标目录中的直接文件。

    Returns:
        原名或 ``name (N).suffix`` 形式的不冲突名称。
    """
    names = {item.name for item in existing}
    if source.name not in names:
        return source.name
    for index in range(1, 10000):
        candidate = f"{source.stem} ({index}){source.suffix}"
        if candidate not in names:
            return candidate
    raise UploadError(f"无法为远端文件生成不冲突名称：{source.name}")


def _is_retryable(error: BaseException) -> bool:
    """判断异常链中是否包含网络传输错误。

    Args:
        error: 上传流程捕获的异常。

    Returns:
        仅当异常链包含 ``httpx.TransportError`` 时返回 ``True``。
    """
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, httpx.TransportError):
            return True
        current = current.__cause__
    return False


def upload_and_verify(
    uploader: OpenUploader,
    source: Path,
    *,
    parent_cid: int,
    part_size: int,
    conflict: ConflictPolicy,
    retries: int,
    progress_output,
) -> VerifiedUpload:
    """执行冲突处理、有限网络重试、上传和远端强校验。

    Args:
        uploader: 已认证的 115 客户端。
        source: 本地源文件。
        parent_cid: 目标目录 CID。
        part_size: OSS 分片大小。
        conflict: 同名文件处理策略。
        retries: 网络错误后的最大重试次数。
        progress_output: 上传进度输出函数。

    Returns:
        经大小和 SHA1 强校验的上传结果。

    Raises:
        UploadError: 冲突、callback 或强校验失败。
    """
    if retries < 0:
        raise ValueError("重试次数不能为负数")
    existing = uploader.list_all_files(parent_cid)
    same_name = [item for item in existing if item.name == source.name]
    remote_name = source.name
    if same_name:
        if conflict == "skip":
            return VerifiedUpload(source, same_name[0], False, False)
        if conflict == "verify":
            remote = find_verified_remote(
                uploader,
                source,
                parent_cid=parent_cid,
            )
            return VerifiedUpload(source, remote, False, False)
        if conflict == "rename":
            remote_name = choose_remote_name(source, existing)
        else:
            raise UploadError(f"115 目标目录已存在同名文件：{source.name}")

    attempt = 0
    while True:
        try:
            result: UploadResult = uploader.upload(
                source,
                cid=parent_cid,
                remote_name=remote_name,
                part_size=part_size,
                progress_output=progress_output,
            )
            break
        except UploadError as error:
            if attempt >= retries or not _is_retryable(error):
                raise
            attempt += 1
            # 仅网络层错误使用短退避；业务校验失败必须立即向上暴露。
            sleep(min(2 ** attempt, 10))

    remote = find_verified_remote(
        uploader,
        source,
        parent_cid=result.target_cid,
        remote_name=result.remote_name,
    )
    return VerifiedUpload(source, remote, True, result.instant)


def finalize_local_source(
    source: Path,
    *,
    delete_after_verify: bool,
    move_after_verify: Path | None,
) -> Path | None:
    """在远端强校验成功后删除或归档本地源文件。

    Args:
        source: 已确认上传成功的本地文件。
        delete_after_verify: 是否删除本地源文件。
        move_after_verify: 可选本地归档目录。

    Returns:
        移动后的路径；删除或不处理时返回 ``None``。

    Raises:
        ValueError: 同时指定删除和移动，或目标文件已存在。
        OSError: 删除、创建目录或移动失败。
    """
    if delete_after_verify and move_after_verify is not None:
        raise ValueError("不能同时指定删除源文件和移动源文件")
    if delete_after_verify:
        source.unlink()
        return None
    if move_after_verify is None:
        return None
    destination_dir = move_after_verify.expanduser().resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / source.name
    if destination.exists():
        raise FileExistsError(f"本地归档目标已存在：{destination}")
    return Path(shutil.move(str(source), str(destination)))


def append_manifest(
    manifest: Path,
    verified: VerifiedUpload,
    *,
    action: str,
) -> None:
    """把单个已完成结果追加为 JSON Lines 审计记录。

    Args:
        manifest: JSONL 文件路径。
        verified: 已强校验的结果。
        action: ``uploaded``、``verified`` 或 ``skipped``。
    """
    manifest = manifest.expanduser().resolve()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "source": str(verified.source),
        "remote": asdict(verified.remote),
        "uploaded": verified.uploaded,
        "instant": verified.instant,
    }
    with manifest.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
