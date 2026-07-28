"""115 Open API 与阿里云 OSS 文件上传实现。"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Callable

import httpx
import oss2

PRO_API = "https://proapi.115.com"
PREFIX_SIZE = 128 * 1024


class UploadError(RuntimeError):
    """上传协议、业务状态或 OSS 完成确认错误。"""


@dataclass(frozen=True, slots=True)
class UploadResult:
    """统一上传结果。"""

    instant: bool
    response: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RemoteFolder:
    """115 文件夹列表中的标准化目录条目。

    Attributes:
        cid: 文件夹自身 CID。
        parent_cid: 父文件夹 CID。
        name: 文件夹名称。
    """

    cid: int
    parent_cid: int
    name: str


def _hash_file(path: Path) -> tuple[str, str]:
    """单次顺序读取，同时计算整文件和前 128 KiB 的大写 SHA1。"""
    full = hashlib.sha1()
    prefix = hashlib.sha1()
    remaining = PREFIX_SIZE
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            full.update(chunk)
            if remaining:
                prefix_chunk = chunk[:remaining]
                prefix.update(prefix_chunk)
                remaining -= len(prefix_chunk)
    return full.hexdigest().upper(), prefix.hexdigest().upper()


def _range_sha1(path: Path, raw_range: str) -> str:
    """按官方闭区间 ``start-end`` 计算大写 SHA1。"""
    try:
        start, end = (int(value) for value in raw_range.split("-", 1))
    except (TypeError, ValueError) as error:
        raise UploadError("115 返回了无效的文件校验区间") from error
    size = path.stat().st_size
    if start < 0 or end < start or end >= size:
        raise UploadError("115 返回的文件校验区间越界")
    with path.open("rb") as stream:
        stream.seek(start)
        data = stream.read(end - start + 1)
    if len(data) != end - start + 1:
        raise UploadError("读取文件校验区间失败，源文件可能已变化")
    return hashlib.sha1(data).hexdigest().upper()


def _callback_fields(raw: Any) -> tuple[str, str]:
    """兼容 callback 对象或单元素数组，并严格提取两个 OSS 头。"""
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if not isinstance(raw, dict):
        raise UploadError("115 未返回 OSS 完成回调")
    callback = str(raw.get("callback") or "")
    callback_var = str(raw.get("callback_var") or "")
    if not callback:
        raise UploadError("115 返回的 OSS callback 为空")
    return callback, callback_var


class ProgressReporter:
    """把累计上传字节转换为单行进度信息。"""

    def __init__(self, total: int, output: Callable[[str], None]) -> None:
        self.total = total
        self.output = output
        self.transferred = 0
        self.started_at = monotonic()

    def add(self, increment: int) -> None:
        """累加一个已完成分片的字节数。"""
        self.transferred = min(self.total, self.transferred + increment)
        elapsed = max(monotonic() - self.started_at, 0.001)
        percent = 100.0 if self.total == 0 else self.transferred / self.total * 100
        speed = self.transferred / elapsed / 1024 / 1024
        self.output(
            f"\r上传中 {percent:6.2f}% "
            f"({self.transferred}/{self.total} bytes, {speed:.2f} MiB/s)"
        )


class OpenUploader:
    """严格实现 115 init、签名状态机、STS 与 OSS 上传。"""

    def __init__(
        self,
        access_token: str,
        *,
        http_client: httpx.Client | None = None,
        bucket_factory: Callable[..., Any] | None = None,
    ) -> None:
        """初始化上传器。

        Args:
            access_token: 有效的 115 OAuth access token。
            http_client: 测试可注入的 HTTP 客户端。
            bucket_factory: 测试可注入的 OSS Bucket 工厂。
        """
        if not access_token:
            raise ValueError("access token 不能为空")
        self.http = http_client or httpx.Client(timeout=60)
        self.headers = {"Authorization": f"Bearer {access_token}"}
        self.bucket_factory = bucket_factory or oss2.Bucket

    def upload(
        self,
        source: Path,
        *,
        cid: int | None = None,
        remote_dir: str | None = None,
        part_size: int = 32 * 1024 * 1024,
        progress_output: Callable[[str], None] = print,
    ) -> UploadResult:
        """上传一个本地文件到目标 CID 或远端目录路径。"""
        source = source.expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"指定文件不存在：{source}")
        if not source.is_file():
            raise ValueError(f"指定路径不是普通文件：{source}")
        if cid is not None and remote_dir is not None:
            raise ValueError("CID 和远端目录路径不能同时指定")
        if cid is not None and cid < 0:
            raise ValueError("目标目录 CID 不能为负数")
        if part_size < 100 * 1024:
            raise ValueError("OSS 分片大小不能小于 100 KiB")

        file_size = source.stat().st_size
        target_cid = self.resolve_remote_dir(remote_dir) if remote_dir is not None else (cid or 0)
        file_sha1, pre_sha1 = _hash_file(source)
        payload = {
            "file_name": source.name,
            "file_size": str(file_size),
            "target": f"U_1_{target_cid}",
            "fileid": file_sha1,
            "preid": pre_sha1,
            "topupload": "0",
        }
        data = self._init(payload)
        status = int(data.get("status") or 0)
        code = int(data.get("code") or 0)

        if (code, status) in {(700, 6), (701, 7)}:
            sign_key = str(data.get("sign_key") or "")
            sign_check = str(data.get("sign_check") or "")
            if not sign_key or not sign_check:
                raise UploadError("115 未返回完整的二次签名信息")
            signed_payload = {
                **payload,
                "sign_key": sign_key,
                "sign_val": _range_sha1(source, sign_check),
            }
            data = self._init(signed_payload)
            status = int(data.get("status") or 0)
            code = int(data.get("code") or 0)

        if (code, status) == (702, 8):
            raise UploadError("文件签名认证失败")
        if status == 2:
            return UploadResult(True, data)
        if status != 1:
            raise UploadError(str(data.get("message") or "115 拒绝了上传初始化"))

        bucket_name = str(data.get("bucket") or "")
        object_key = str(data.get("object") or "")
        if not bucket_name or not object_key:
            raise UploadError("115 未返回完整的 OSS 上传目标")
        callback, callback_var = _callback_fields(data.get("callback"))
        sts = self._get_upload_token()
        self._upload_oss(
            source,
            bucket_name=bucket_name,
            object_key=object_key,
            callback=callback,
            callback_var=callback_var,
            sts=sts,
            part_size=part_size,
            progress_output=progress_output,
        )
        return UploadResult(False, data)

    def resolve_remote_dir(self, remote_dir: str) -> int:
        """将 ``/目录/子目录`` 逐级解析为最终目录 CID。

        Args:
            remote_dir: 从根目录开始的绝对路径；``/`` 表示根目录。

        Returns:
            最后一层目录的 CID。

        Raises:
            ValueError: 路径不是绝对路径或包含 ``..``。
            UploadError: 某一级目录不存在、重名或列表响应无效。
        """
        if not remote_dir.startswith("/"):
            raise ValueError("115 目录路径必须以 / 开头")
        segments = [segment for segment in remote_dir.split("/") if segment not in ("", ".")]
        if any(segment == ".." for segment in segments):
            raise ValueError("115 目录路径不能包含 ..")
        current_cid = 0
        traversed: list[str] = []
        for segment in segments:
            matches = self._find_child_folders(current_cid, segment)
            traversed.append(segment)
            current_path = "/" + "/".join(traversed)
            if not matches:
                raise UploadError(f"115 目录不存在：{current_path}")
            if len(matches) > 1:
                raise UploadError(f"115 目录存在同名项，无法唯一解析：{current_path}")
            current_cid = matches[0]
        return current_cid

    def _find_child_folders(self, parent_cid: int, name: str) -> list[int]:
        """分页列出父目录，并返回名称完全匹配的子目录 CID。"""
        return [
            folder.cid
            for folder in self.list_child_folders(parent_cid)
            if folder.name == name
        ]

    def list_child_folders(self, parent_cid: int = 0) -> list[RemoteFolder]:
        """分页列出指定 115 目录的直接子文件夹。

        Args:
            parent_cid: 父目录 CID；根目录为 0，不能为负数。

        Returns:
            按文件名升序排列的直接子文件夹列表。

        Raises:
            ValueError: 父目录 CID 为负数。
            UploadError: 网络、业务状态或响应字段不符合接口契约。
        """
        if parent_cid < 0:
            raise ValueError("父目录 CID 不能为负数")
        return self._list_folders(
            f"{PRO_API}/open/ufile/files",
            {
                "aid": 1,
                "cid": str(parent_cid),
                "o": "file_name",
                "asc": 1,
                "show_dir": 1,
                "fc_mix": 1,
                "count_folders": 1,
                # 对齐 StarVault：nf=1 让 115 只返回文件夹，避免无谓传输文件条目。
                "nf": 1,
            },
            expected_parent_cid=parent_cid,
        )

    def search_folders(self, query: str) -> list[RemoteFolder]:
        """按名称在整个 115 账号中搜索文件夹。

        Args:
            query: 非空的文件夹名称关键词，由 115 执行子串匹配。

        Returns:
            按文件名升序排列的匹配文件夹列表。

        Raises:
            ValueError: 搜索关键词为空。
            UploadError: 网络、业务状态或响应字段不符合接口契约。
        """
        query = query.strip()
        if not query:
            raise ValueError("文件夹搜索关键词不能为空")
        return self._list_folders(
            f"{PRO_API}/open/ufile/search",
            {
                "search_value": query,
                "aid": 1,
                "cid": "0",
                "o": "file_name",
                "asc": 1,
                "show_dir": 1,
                "fc_mix": 1,
                "count_folders": 1,
                # 对齐 StarVault：fc=1 表示搜索结果只保留文件夹。
                "fc": 1,
            },
            expected_parent_cid=None,
        )

    def _list_folders(
        self,
        url: str,
        base_params: dict[str, Any],
        *,
        expected_parent_cid: int | None,
    ) -> list[RemoteFolder]:
        """分页请求文件列表端点并严格标准化其中的文件夹条目。

        Args:
            url: 115 文件列表或搜索端点。
            base_params: 除 offset、limit 外的查询参数。
            expected_parent_cid: 列目录时已知的父 CID；搜索时为 ``None``，改读响应字段。

        Returns:
            去重后的文件夹列表。

        Raises:
            UploadError: 请求失败或文件夹响应字段无效。
        """
        offset = 0
        limit = 200
        folders: list[RemoteFolder] = []
        seen: set[int] = set()
        while True:
            try:
                response = self.http.request(
                    "GET",
                    url,
                    headers=self.headers,
                    params={
                        **base_params,
                        "offset": offset,
                        "limit": limit,
                    },
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as error:
                raise UploadError("读取 115 目录失败") from error
            if not isinstance(payload, dict) or payload.get("state") not in (True, 1):
                message = payload.get("error") if isinstance(payload, dict) else None
                raise UploadError(str(message or "读取 115 目录失败"))
            items = payload.get("data")
            if not isinstance(items, list):
                raise UploadError("115 目录列表响应格式无效")
            for item in items:
                if not isinstance(item, dict):
                    raise UploadError("115 目录条目响应格式无效")
                folder = self._parse_folder_item(
                    item,
                    expected_parent_cid=expected_parent_cid,
                )
                # 接口偶尔无视 nf/fc 并混入文件，明确过滤但不掩盖文件夹字段错误。
                if folder is not None and folder.cid not in seen:
                    seen.add(folder.cid)
                    folders.append(folder)
            offset += len(items)
            try:
                count = int(payload.get("count") or 0)
            except (TypeError, ValueError) as error:
                raise UploadError("115 返回了无效的目录总数") from error
            if not items or offset >= count:
                return folders

    @staticmethod
    def _parse_folder_item(
        item: dict[str, Any],
        *,
        expected_parent_cid: int | None,
    ) -> RemoteFolder | None:
        """兼容两种 115 列表字段并把文件夹转换为统一模型。

        Args:
            item: 115 返回的单个文件或文件夹对象。
            expected_parent_cid: 调用方已知的父 CID；全局搜索时为 ``None``。

        Returns:
            文件夹对应的 ``RemoteFolder``；普通文件返回 ``None``。

        Raises:
            UploadError: 文件夹缺少名称、CID 或父 CID 字段。
        """
        search_schema = "file_name" in item
        compact_schema = "fn" in item
        if search_schema:
            # 搜索端点使用 file_category=0 表示文件夹；请求已带 fc=1，但仍严格判别。
            is_folder = str(item.get("file_category", "0")) == "0"
        elif compact_schema:
            is_folder = str(item.get("fc")) == "0"
        else:
            is_folder = "fid" not in item
        if not is_folder:
            return None
        try:
            raw_name = (
                item.get("file_name")
                if search_schema
                else item.get("fn") if compact_schema else item.get("n")
            )
            name = str(raw_name or "")
            folder_id = (
                item["file_id"]
                if search_schema
                else item["fid"] if compact_schema else item["cid"]
            )
            raw_parent = (
                expected_parent_cid
                if expected_parent_cid is not None
                else item.get("parent_id", item.get("pid"))
            )
            if not name or raw_parent is None:
                raise KeyError("name or parent cid")
            return RemoteFolder(
                cid=int(folder_id),
                parent_cid=int(raw_parent),
                name=name,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise UploadError("115 返回了无效的文件夹字段") from error

    def _init(self, payload: dict[str, str]) -> dict[str, Any]:
        """调用上传初始化，错误响应不泄露 JSON 或敏感字段。"""
        return self._request_data("POST", f"{PRO_API}/open/upload/init", data=payload)

    def _get_upload_token(self) -> dict[str, Any]:
        """获取阿里云 OSS STS 临时凭证。"""
        data = self._request_data("GET", f"{PRO_API}/open/upload/get_token")
        required = ("endpoint", "AccessKeyId", "AccessKeySecret", "SecurityToken")
        if any(not data.get(field) for field in required):
            raise UploadError("115 未返回完整的 OSS 临时凭证")
        return data

    def _request_data(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """执行 Bearer 请求并严格拆解成功 envelope。"""
        try:
            response = self.http.request(method, url, headers=self.headers, **kwargs)
            response.raise_for_status()
            envelope = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise UploadError("115 上传接口请求失败") from error
        if not isinstance(envelope, dict):
            raise UploadError("115 上传响应格式无效")
        if envelope.get("state") not in (True, 1):
            message = envelope.get("message") or "115 上传请求失败"
            raise UploadError(str(message))
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise UploadError("115 上传响应缺少 data 对象")
        # 部分响应把业务 code 放在顶层，部分放在 data；统一到 data 供状态机判断。
        if "code" not in data and "code" in envelope:
            data = {**data, "code": envelope["code"]}
        return data

    def _upload_oss(
        self,
        source: Path,
        *,
        bucket_name: str,
        object_key: str,
        callback: str,
        callback_var: str,
        sts: dict[str, Any],
        part_size: int,
        progress_output: Callable[[str], None],
    ) -> None:
        """使用 STS 执行单 PUT 或流式 multipart 上传。"""
        auth = oss2.StsAuth(
            str(sts["AccessKeyId"]),
            str(sts["AccessKeySecret"]),
            str(sts["SecurityToken"]),
        )
        bucket = self.bucket_factory(auth, str(sts["endpoint"]), bucket_name)
        headers = {
            "x-oss-callback": base64.b64encode(callback.encode()).decode(),
            "x-oss-callback-var": base64.b64encode(callback_var.encode()).decode(),
        }
        total = source.stat().st_size
        progress = ProgressReporter(total, progress_output)
        if total <= part_size:
            with source.open("rb") as stream:
                bucket.put_object(object_key, stream, headers=headers)
            progress.add(total)
            return

        upload_id = ""
        try:
            upload_id = bucket.init_multipart_upload(
                object_key,
                headers=headers,
            ).upload_id
            parts: list[oss2.models.PartInfo] = []
            with source.open("rb") as stream:
                part_number = 1
                while chunk := stream.read(part_size):
                    result = bucket.upload_part(
                        object_key,
                        upload_id,
                        part_number,
                        chunk,
                    )
                    parts.append(oss2.models.PartInfo(part_number, result.etag, size=len(chunk)))
                    progress.add(len(chunk))
                    part_number += 1
            bucket.complete_multipart_upload(object_key, upload_id, parts)
        except BaseException:
            # 已创建会话但未完成时明确中止，避免遗留无主 OSS 分片占用空间。
            if upload_id:
                bucket.abort_multipart_upload(object_key, upload_id)
            raise


def upload_file(
    access_token: str,
    source: Path,
    *,
    cid: int | None = None,
    remote_dir: str | None = None,
    part_size: int = 32 * 1024 * 1024,
    progress_output: Callable[[str], None] = print,
    uploader_factory: Callable[[str], OpenUploader] = OpenUploader,
) -> UploadResult:
    """便于 CLI 和测试调用的上传入口。"""
    return uploader_factory(access_token).upload(
        source,
        cid=cid,
        remote_dir=remote_dir,
        part_size=part_size,
        progress_output=progress_output,
    )
