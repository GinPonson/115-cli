"""115 Open API 与阿里云 OSS 文件上传实现。"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Callable

import httpx

PRO_API = "https://proapi.115.com"
PREFIX_SIZE = 128 * 1024


def _load_oss2() -> Any:
    """延迟加载 OSS SDK，并隔离其 Python 3.12 文档字符串语法警告。

    Returns:
        已加载的 ``oss2`` 模块。

    Notes:
        仅忽略第三方模块导入阶段的 ``SyntaxWarning``；上传时的网络、协议和业务异常
        不在此处捕获，仍会完整向上暴露。
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        import oss2

    return oss2


class UploadError(RuntimeError):
    """上传协议、业务状态或 OSS 完成确认错误。"""


class UploadCredentialExpiredError(UploadError):
    """OSS 临时上传凭证已过期，可以重新初始化上传后重试。"""


class MultipartCleanupError(UploadError):
    """上传失败后，OSS multipart 清理操作也失败。"""


_SENSITIVE_ERROR_KEYS = {
    "accesskeyid",
    "accesskeysecret",
    "authorization",
    "ossaccesskeyid",
    "securitytoken",
    "access_token",
    "refresh_token",
}
_SENSITIVE_TEXT_PATTERN = re.compile(
    r"(?i)(SecurityToken|AccessKeyId|AccessKeySecret|OSSAccessKeyId|"
    r"access_token|refresh_token|Authorization)"
    r"(?P<separator>['\"\s:=]+)(?P<value>[^,}\s'\"]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def _redact_error_text(value: object) -> str:
    """对非结构化异常文本中的常见凭证字段进行兜底脱敏。"""
    text = str(value)
    text = _SENSITIVE_TEXT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group('separator')}<redacted>",
        text,
    )
    return _BEARER_PATTERN.sub("Bearer <redacted>", text)


def _exception_details(error: BaseException) -> dict[str, Any]:
    """从第三方 SDK 异常中提取结构化详情。

    Args:
        error: 可能包含 ``details`` 或字典参数的异常。

    Returns:
        可安全继续检查的详情字典；不存在时返回空字典。
    """
    details = getattr(error, "details", None)
    if isinstance(details, dict):
        return details
    for argument in getattr(error, "args", ()):
        if isinstance(argument, dict):
            nested = argument.get("details")
            return nested if isinstance(nested, dict) else argument
    return {}


def _safe_error_details(error: BaseException) -> dict[str, Any]:
    """生成移除凭证字段后的异常详情。"""
    return {
        str(key): "<redacted>" if str(key).lower() in _SENSITIVE_ERROR_KEYS else value
        for key, value in _exception_details(error).items()
    }


def format_safe_error(error: BaseException) -> str:
    """格式化不包含 OAuth 或 OSS 临时凭证的错误信息。

    Args:
        error: 需要展示或写入审计日志的异常。

    Returns:
        已脱敏、适合用户日志的错误文本。
    """
    if isinstance(error, UploadError):
        return _redact_error_text(error)
    details = _safe_error_details(error)
    if details:
        code = details.get("Code") or details.get("code")
        message = details.get("Message") or details.get("message")
        request_id = details.get("RequestId") or details.get("request_id")
        fields = [
            f"code={code}" if code else "",
            f"message={message}" if message else "",
            f"request_id={request_id}" if request_id else "",
        ]
        summary = ", ".join(field for field in fields if field)
        return f"{type(error).__name__}: {summary or '远端请求失败'}"
    return f"{type(error).__name__}: {_redact_error_text(error)}"


def _normalize_oss_error(error: BaseException) -> UploadError:
    """把 OSS SDK 异常转换为稳定且脱敏的项目异常。"""
    details = _exception_details(error)
    code = str(details.get("Code") or details.get("code") or "")
    request_id = details.get("RequestId") or details.get("request_id")
    suffix = f"（request_id={request_id}）" if request_id else ""
    if code == "SecurityTokenExpired":
        return UploadCredentialExpiredError(f"OSS 临时上传凭证已过期{suffix}")
    return UploadError(f"OSS 请求失败：{format_safe_error(error)}")


@dataclass(frozen=True, slots=True)
class UploadResult:
    """统一上传结果。

    Attributes:
        instant: 是否由 115 秒传完成。
        response: 115 上传初始化响应，仅供协议诊断使用。
        file_sha1: 上传前计算的本地文件大写 SHA1。
        target_cid: 实际上传到的父目录 CID。
        remote_name: 实际提交给 115 的文件名。
    """

    instant: bool
    response: dict[str, Any]
    file_sha1: str = ""
    target_cid: int = 0
    remote_name: str = ""


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


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """115 文件列表中的标准化文件条目。

    Attributes:
        file_id: 文件自身 ID。
        parent_cid: 父文件夹 CID。
        name: 文件名称。
        size: 文件大小，单位为字节。
        sha1: 115 返回的大写 SHA1；接口未提供时为空字符串。
    """

    file_id: int
    parent_cid: int
    name: str
    size: int
    sha1: str


@dataclass(frozen=True, slots=True)
class RemoteFilePage:
    """115 文件列表的单页结果。

    Attributes:
        files: 当前页的文件条目。
        offset: 当前页请求偏移。
        limit: 当前页请求大小。
        total: 当前筛选条件下的文件总数。
        next_offset: 下一页偏移；没有下一页时为 ``None``。
    """

    files: tuple[RemoteFile, ...]
    offset: int
    limit: int
    total: int
    next_offset: int | None


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
        oss2 = _load_oss2()
        self.http = http_client or httpx.Client(timeout=60)
        self.headers = {"Authorization": f"Bearer {access_token}"}
        self.oss2 = oss2
        self.bucket_factory = bucket_factory or oss2.Bucket

    def upload(
        self,
        source: Path,
        *,
        cid: int | None = None,
        remote_dir: str | None = None,
        remote_name: str | None = None,
        part_size: int = 32 * 1024 * 1024,
        progress_output: Callable[[str], None] = print,
    ) -> UploadResult:
        """上传一个本地文件到目标 CID 或远端目录路径。

        Args:
            source: 本地普通文件。
            cid: 115 目标目录 CID。
            remote_dir: 115 目标目录绝对路径。
            remote_name: 可选的远端文件名；默认使用本地文件名。
            part_size: OSS 分片大小，单位为字节。
            progress_output: 单行进度输出函数。

        Returns:
            包含上传模式、SHA1、目标 CID 与远端名称的上传结果。
        """
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
        upload_name = remote_name if remote_name is not None else source.name
        if not upload_name or "/" in upload_name or "\x00" in upload_name:
            raise ValueError("远端文件名不能为空，且不能包含 / 或 NUL")
        payload = {
            "file_name": upload_name,
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
            return UploadResult(True, data, file_sha1, target_cid, upload_name)
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
        return UploadResult(False, data, file_sha1, target_cid, upload_name)

    def list_all_files(self, parent_cid: int) -> list[RemoteFile]:
        """读取指定目录内的全部直接文件。

        Args:
            parent_cid: 目标目录 CID。

        Returns:
            按 115 文件列表顺序去重后的全部文件。

        Raises:
            UploadError: 分页未前进或远端响应无效。
        """
        files: list[RemoteFile] = []
        seen: set[int] = set()
        offset = 0
        while True:
            page = self.list_files_page(parent_cid, offset=offset, limit=1150)
            for remote_file in page.files:
                if remote_file.file_id not in seen:
                    seen.add(remote_file.file_id)
                    files.append(remote_file)
            if page.next_offset is None:
                return files
            if page.next_offset <= offset:
                raise UploadError("115 文件列表分页偏移未前进")
            offset = page.next_offset

    def trash_file(self, file_id: int, parent_cid: int) -> None:
        """把一个精确文件 ID 移入 115 回收站。

        Args:
            file_id: 要删除的文件 ID，必须为正整数。
            parent_cid: 文件当前所在父目录 CID。

        Raises:
            ValueError: ID 参数非法。
            UploadError: 115 拒绝删除或响应无效。
        """
        if file_id <= 0:
            raise ValueError("文件 ID 必须为正整数")
        if parent_cid < 0:
            raise ValueError("父目录 CID 不能为负数")
        try:
            response = self.http.post(
                f"{PRO_API}/open/ufile/delete",
                headers=self.headers,
                data={"file_ids": str(file_id), "parent_id": str(parent_cid)},
            )
            response.raise_for_status()
            envelope = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise UploadError("115 回收站请求失败") from error
        if not isinstance(envelope, dict) or envelope.get("state") not in (True, 1):
            message = envelope.get("message") if isinstance(envelope, dict) else None
            raise UploadError(str(message or "115 拒绝把文件移入回收站"))

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

    def list_child_folders(
        self,
        parent_cid: int = 0,
        *,
        max_results: int | None = None,
    ) -> list[RemoteFolder]:
        """分页列出指定 115 目录的直接子文件夹。

        Args:
            parent_cid: 父目录 CID；根目录为 0，不能为负数。
            max_results: 最多读取的目录数；``None`` 表示读取全部。

        Returns:
            按文件名升序排列的直接子文件夹列表。

        Raises:
            ValueError: 父目录 CID 为负数。
            UploadError: 网络、业务状态或响应字段不符合接口契约。
        """
        if parent_cid < 0:
            raise ValueError("父目录 CID 不能为负数")
        if max_results is not None and max_results <= 0:
            return []
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
            max_results=max_results,
        )

    def search_folders(
        self,
        query: str,
        *,
        max_results: int | None = None,
    ) -> list[RemoteFolder]:
        """按名称在整个 115 账号中搜索文件夹。

        Args:
            query: 非空的文件夹名称关键词，由 115 执行子串匹配。
            max_results: 最多读取的目录数；``None`` 表示读取全部。

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
            max_results=max_results,
        )

    def list_files_page(
        self,
        parent_cid: int = 0,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> RemoteFilePage:
        """分页列出指定 115 目录中的直接文件，不包含子文件夹。

        Args:
            parent_cid: 父目录 CID；根目录为 0。
            offset: 从零开始的分页偏移。
            limit: 单页条数，范围为 1 到 1150。

        Returns:
            包含总数和下一页偏移的标准化文件页。

        Raises:
            ValueError: CID、offset 或 limit 超出允许范围。
            UploadError: 请求失败或响应字段不符合接口契约。
        """
        self._validate_file_page(parent_cid=parent_cid, offset=offset, limit=limit)
        return self._request_file_page(
            f"{PRO_API}/open/ufile/files",
            {
                "aid": 1,
                "cid": str(parent_cid),
                "o": "file_name",
                "asc": 1,
                "offset": offset,
                "limit": limit,
                # 对齐 StarVault：show_dir=0 仅返回当前目录中的文件。
                "show_dir": 0,
                "fc_mix": 0,
                "count_folders": 0,
            },
            requested_offset=offset,
            requested_limit=limit,
            expected_parent_cid=parent_cid,
        )

    def search_files_page(
        self,
        query: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> RemoteFilePage:
        """分页按名称搜索整个 115 账号中的文件。

        Args:
            query: 非空文件名关键词，由 115 执行子串匹配。
            offset: 从零开始的分页偏移。
            limit: 单页条数，范围为 1 到 1150。

        Returns:
            包含总数和下一页偏移的标准化搜索结果页。

        Raises:
            ValueError: 关键词为空，或分页参数超出允许范围。
            UploadError: 请求失败或响应字段不符合接口契约。
        """
        query = query.strip()
        if not query:
            raise ValueError("文件搜索关键词不能为空")
        self._validate_file_page(parent_cid=0, offset=offset, limit=limit)
        return self._request_file_page(
            f"{PRO_API}/open/ufile/search",
            {
                "search_value": query,
                "aid": 1,
                "cid": "0",
                "o": "file_name",
                "asc": 1,
                "offset": offset,
                "limit": limit,
                "show_dir": 0,
                "fc_mix": 0,
                "count_folders": 0,
                # 115 搜索接口 fc=2 表示只返回文件。
                "fc": 2,
            },
            requested_offset=offset,
            requested_limit=limit,
            expected_parent_cid=None,
        )

    @staticmethod
    def _validate_file_page(*, parent_cid: int, offset: int, limit: int) -> None:
        """校验文件分页参数，防止无界或无效请求进入 115 接口。

        Args:
            parent_cid: 父目录 CID。
            offset: 分页偏移。
            limit: 单页条数。

        Raises:
            ValueError: 任一参数超出允许范围。
        """
        if parent_cid < 0:
            raise ValueError("父目录 CID 不能为负数")
        if offset < 0:
            raise ValueError("文件列表 offset 不能为负数")
        if not 1 <= limit <= 1150:
            raise ValueError("文件列表 limit 必须在 1 到 1150 之间")

    def _request_file_page(
        self,
        url: str,
        params: dict[str, Any],
        *,
        requested_offset: int,
        requested_limit: int,
        expected_parent_cid: int | None,
    ) -> RemoteFilePage:
        """请求并严格解析一页 115 文件列表。

        Args:
            url: 文件列表或搜索端点。
            params: 完整查询参数。
            requested_offset: 调用方请求的 offset。
            requested_limit: 调用方请求的 limit。
            expected_parent_cid: 目录列表已知的父 CID；搜索时为 ``None``。

        Returns:
            标准化文件页。

        Raises:
            UploadError: HTTP、业务状态、分页字段或文件字段无效。
        """
        try:
            response = self.http.request(
                "GET",
                url,
                headers=self.headers,
                params=params,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise UploadError("读取 115 文件列表失败") from error
        if not isinstance(payload, dict) or payload.get("state") not in (True, 1):
            message = payload.get("error") if isinstance(payload, dict) else None
            raise UploadError(str(message or "读取 115 文件列表失败"))
        items = payload.get("data")
        if not isinstance(items, list):
            raise UploadError("115 文件列表响应格式无效")
        try:
            total = int(payload.get("count") or 0)
        except (TypeError, ValueError) as error:
            raise UploadError("115 返回了无效的文件总数") from error

        files: list[RemoteFile] = []
        seen: set[int] = set()
        for item in items:
            if not isinstance(item, dict):
                raise UploadError("115 文件条目响应格式无效")
            parsed = self._parse_file_item(
                item,
                expected_parent_cid=expected_parent_cid,
            )
            # 接口偶尔混入文件夹；只过滤类型，不掩盖真实文件的字段错误。
            if parsed is not None and parsed.file_id not in seen:
                seen.add(parsed.file_id)
                files.append(parsed)

        consumed = len(items)
        next_offset = requested_offset + consumed
        if consumed == 0 or next_offset >= total:
            next_offset = None
        return RemoteFilePage(
            files=tuple(files),
            offset=requested_offset,
            limit=requested_limit,
            total=total,
            next_offset=next_offset,
        )

    @staticmethod
    def _parse_file_item(
        item: dict[str, Any],
        *,
        expected_parent_cid: int | None,
    ) -> RemoteFile | None:
        """兼容 115 列表与搜索字段并转换单个文件条目。

        Args:
            item: 115 返回的文件或文件夹对象。
            expected_parent_cid: 已知父 CID；全局搜索时为 ``None``。

        Returns:
            文件对应的 ``RemoteFile``；文件夹返回 ``None``。

        Raises:
            UploadError: 文件缺少 ID、名称、父 CID或大小字段。
        """
        search_schema = "file_name" in item
        compact_schema = "fn" in item
        if search_schema:
            is_file = str(item.get("file_category", "1")) != "0"
        elif compact_schema:
            is_file = str(item.get("fc")) != "0"
        else:
            is_file = "fid" in item
        if not is_file:
            return None
        try:
            raw_name = (
                item.get("file_name")
                if search_schema
                else item.get("fn") if compact_schema else item.get("n")
            )
            name = str(raw_name or "")
            file_id = item["file_id"] if search_schema else item["fid"]
            raw_parent = (
                expected_parent_cid
                if expected_parent_cid is not None
                else item.get("parent_id", item.get("pid", item.get("cid")))
            )
            raw_size = (
                item.get("file_size", 0)
                if search_schema
                else item.get("fs", 0) if compact_schema else item.get("s", 0)
            )
            raw_sha1 = (
                item.get("sha1", "")
                if search_schema or compact_schema
                else item.get("sha", "")
            )
            if not name or raw_parent is None:
                raise KeyError("name or parent cid")
            size = int(raw_size or 0)
            if size < 0:
                raise ValueError("negative file size")
            return RemoteFile(
                file_id=int(file_id),
                parent_cid=int(raw_parent),
                name=name,
                size=size,
                sha1=str(raw_sha1 or "").upper(),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise UploadError("115 返回了无效的文件字段") from error

    def _list_folders(
        self,
        url: str,
        base_params: dict[str, Any],
        *,
        expected_parent_cid: int | None,
        max_results: int | None = None,
    ) -> list[RemoteFolder]:
        """分页请求文件列表端点并严格标准化其中的文件夹条目。

        Args:
            url: 115 文件列表或搜索端点。
            base_params: 除 offset、limit 外的查询参数。
            expected_parent_cid: 列目录时已知的父 CID；搜索时为 ``None``，改读响应字段。
            max_results: 最多读取的目录数；``None`` 表示读取全部。

        Returns:
            去重后的文件夹列表。

        Raises:
            UploadError: 请求失败或文件夹响应字段无效。
        """
        offset = 0
        limit = 200 if max_results is None else min(200, max_results)
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
                    if max_results is not None and len(folders) >= max_results:
                        return folders
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
        auth = self.oss2.StsAuth(
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
            try:
                with source.open("rb") as stream:
                    put_result = bucket.put_object(object_key, stream, headers=headers)
                self._require_callback_success(put_result)
                progress.add(total)
                return
            except Exception as error:
                if isinstance(error, UploadError):
                    raise
                raise _normalize_oss_error(error) from error

        upload_id = ""
        multipart_completed = False
        try:
            upload_id = bucket.init_multipart_upload(
                object_key,
                # 115 依赖 OSS 按上传顺序累计文件哈希；缺少该参数时对象虽然合并成功，
                # callback 仍会因完整文件 SHA1 无效而拒绝入库。
                params={"sequential": ""},
            ).upload_id
            parts: list[Any] = []
            with source.open("rb") as stream:
                part_number = 1
                while chunk := stream.read(part_size):
                    result = bucket.upload_part(
                        object_key,
                        upload_id,
                        part_number,
                        chunk,
                    )
                    parts.append(
                        self.oss2.models.PartInfo(
                            part_number,
                            result.etag,
                            size=len(chunk),
                            part_crc=result.crc,
                        )
                    )
                    progress.add(len(chunk))
                    part_number += 1
            complete_result = bucket.complete_multipart_upload(
                object_key,
                upload_id,
                parts,
                # p115oss 完成分片时显式使用 text/xml；该值也会参与
                # OSS callback 请求内容，不能依赖 SDK 的默认 MIME 类型。
                headers={**headers, "Content-Type": "text/xml"},
            )
            multipart_completed = True
            self._require_callback_success(complete_result)
        except BaseException as error:
            if not isinstance(error, Exception):
                # 中断信号仍需原样传播；清理失败只作为附注，不能改变控制流语义。
                if upload_id and not multipart_completed:
                    try:
                        bucket.abort_multipart_upload(object_key, upload_id)
                    except Exception as cleanup_error:
                        error.add_note(
                            "multipart 清理失败："
                            f"{format_safe_error(_normalize_oss_error(cleanup_error))}"
                        )
                raise
            # 先转换可能携带 STS 明文的 SDK 异常，确保上层日志不会泄露凭证。
            upload_error = error if isinstance(error, UploadError) else _normalize_oss_error(error)
            # Complete 成功后的 callback 业务失败不能再 Abort，否则 NoSuchUpload
            # 会覆盖真正的 115 错误；只有尚未完成的会话需要主动清理。
            if upload_id and not multipart_completed:
                try:
                    bucket.abort_multipart_upload(object_key, upload_id)
                except BaseException as cleanup_error:
                    safe_cleanup_error = (
                        cleanup_error
                        if isinstance(cleanup_error, UploadError)
                        else _normalize_oss_error(cleanup_error)
                    )
                    raise MultipartCleanupError(
                        "OSS 上传失败，且 multipart 清理失败；"
                        f"上传错误：{format_safe_error(upload_error)}；"
                        f"清理错误：{format_safe_error(safe_cleanup_error)}"
                    ) from upload_error
            raise upload_error from error

    @staticmethod
    def _require_callback_success(result: Any) -> None:
        """同时校验 OSS HTTP 状态与 115 callback 业务响应。

        OSS 即使成功存储对象，也可能返回 callback 失败；115 callback 还可能以
        HTTP 200 返回非零业务 code。两者都不能标记为上传成功。
        """
        status = int(getattr(result, "status", 0) or 0)
        if status != 200:
            raise UploadError(f"OSS 上传完成但 115 回调失败（HTTP {status}）")
        response = getattr(getattr(result, "resp", None), "response", None)
        content = getattr(response, "content", b"")
        if not content:
            return
        try:
            payload = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise UploadError("115 上传回调响应格式无效") from error
        if not isinstance(payload, dict):
            raise UploadError("115 上传回调响应格式无效")
        code = int(payload.get("code") or 0)
        state = payload.get("state")
        if code != 0 or state in (False, 0):
            message = payload.get("message") or "115 文件校验失败"
            raise UploadError(str(message))


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
