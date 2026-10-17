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
from typing import Any, Callable, Sequence

import httpx

PRO_API = "https://proapi.115.com"
PREFIX_SIZE = 128 * 1024
# 对齐 StarVault 实测：downurl 端点按 UA 签发 CDN 直链，python-httpx 默认 UA 拿不到有效签名。
_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/120.0",
    "Referer": "https://115.com/",
    "Origin": "https://115.com",
}


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


@dataclass(frozen=True, slots=True)
class UserInfo:
    """115 账号身份、空间与会员概要。

    Attributes:
        user_id: 115 用户 ID。
        user_name: 115 用户昵称。
        total_size: 空间总容量，单位为字节。
        remain_size: 空间剩余容量，单位为字节。
        use_size: 已用空间，单位为字节。
        vip_level: 会员等级名称；非会员或接口未提供时为空字符串。
        vip_expire: 会员到期 Unix 时间戳；未知为 0。
    """

    user_id: int
    user_name: str
    total_size: int
    remain_size: int
    use_size: int
    vip_level: str
    vip_expire: int


@dataclass(frozen=True, slots=True)
class RemoteEntry:
    """115 单个文件或文件夹的详情。

    Attributes:
        file_id: 条目自身 ID。
        parent_id: 父目录 CID；接口未提供时为 0。
        name: 条目名称。
        is_folder: 是否为文件夹。
        size: 文件大小，单位为字节；文件夹为 0。
        sha1: 大写 SHA1；文件夹或接口未提供时为空字符串。
        pick_code: 文件提取码，用于下载直链等场景。
    """

    file_id: int
    parent_id: int
    name: str
    is_folder: bool
    size: int
    sha1: str
    pick_code: str


@dataclass(frozen=True, slots=True)
class DownloadLink:
    """115 文件下载直链。

    Attributes:
        file_id: 文件 ID。
        name: 文件名称。
        size: 文件大小，单位为字节。
        sha1: 大写 SHA1。
        url: 按浏览器 UA 签名的 CDN 下载直链。
    """

    file_id: int
    name: str
    size: int
    sha1: str
    url: str


@dataclass(frozen=True, slots=True)
class TrashItem:
    """115 回收站中的单个文件或文件夹条目。

    Attributes:
        trash_id: 回收站条目 ID，用于恢复与彻底删除。
        name: 条目名称。
        is_folder: 是否为文件夹。
        size: 文件大小，单位为字节；文件夹为 0。
        deleted_time: 删除时间 Unix 时间戳；接口未提供时为 0。
        parent_cid: 原父目录 CID。
        parent_name: 原父目录名称。
        pick_code: 文件提取码。
        status: 还原状态；-1 表示还原中，0 表示正常。
    """

    trash_id: str
    name: str
    is_folder: bool
    size: int
    deleted_time: int
    parent_cid: int
    parent_name: str
    pick_code: str
    status: int


@dataclass(frozen=True, slots=True)
class TrashPage:
    """115 回收站的分页结果。

    Attributes:
        items: 当前页的回收站条目。
        offset: 当前页请求偏移。
        limit: 当前页请求大小。
        total: 回收站条目总数。
        next_offset: 下一页偏移；没有下一页时为 ``None``。
    """

    items: tuple[TrashItem, ...]
    offset: int
    limit: int
    total: int
    next_offset: int | None


@dataclass(frozen=True, slots=True)
class OfflineUrlResult:
    """单个链接离线任务的提交结果。

    Attributes:
        url: 提交的链接原文。
        info_hash: 任务 SHA1；仅提交成功时非空。
        state: 115 是否接受该链接。
        message: 失败原因；成功时为空字符串。
    """

    url: str
    info_hash: str
    state: bool
    message: str


@dataclass(frozen=True, slots=True)
class OfflineTask:
    """115 云下载（离线下载）任务。

    Attributes:
        info_hash: 任务 SHA1，用于删除任务。
        name: 任务名称。
        size: 任务总大小，单位为字节。
        status: 任务状态；-1 失败、0 分配中、1 下载中、2 成功。
        percent_done: 下载进度百分比。
        add_time: 任务添加时间 Unix 时间戳。
        last_update: 最后更新时间 Unix 时间戳。
        file_id: 任务源文件（夹）ID。
        delete_file_id: 删除任务源文件时需要的 ID。
        url: 链接任务的原始 URL。
        wp_path_id: 任务源文件所在父文件夹 ID。
    """

    info_hash: str
    name: str
    size: int
    status: int
    percent_done: int
    add_time: int
    last_update: int
    file_id: str
    delete_file_id: str
    url: str
    wp_path_id: str


@dataclass(frozen=True, slots=True)
class OfflineTaskPage:
    """115 云下载任务的分页结果。

    Attributes:
        page: 当前页码。
        page_count: 总页数。
        count: 任务总数。
        tasks: 当前页的任务列表。
    """

    page: int
    page_count: int
    count: int
    tasks: tuple[OfflineTask, ...]


@dataclass(frozen=True, slots=True)
class OfflineQuota:
    """115 云下载配额概要。

    Attributes:
        surplus: 剩余配额次数。
        used: 已用配额次数。
        count: 配额总量次数。
    """

    surplus: int
    used: int
    count: int


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


def _tolerant_int(value: Any, default: int = 0) -> int:
    """把 115 返回的数字或数字字符串安全转换为整数。

    官方文档多处把数值字段标为 string、实测又返回 number，调用方
    无法依赖单一形态；无法解析时返回默认值而不是让整个响应失败。

    Args:
        value: 接口返回的原始值。
        default: 解析失败时的默认值。

    Returns:
        转换后的整数。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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
        segments = self._remote_dir_segments(remote_dir)
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

    def ensure_remote_dir(self, remote_dir: str) -> int:
        """逐级解析远端目录，并创建缺失的中间层级。

        Args:
            remote_dir: 从根目录开始的绝对路径；``/`` 表示根目录。

        Returns:
            最后一层目录的 CID。

        Raises:
            ValueError: 路径或目录名称非法。
            UploadError: 目录重名、创建失败或响应字段无效。
        """
        segments = self._remote_dir_segments(remote_dir)
        current_cid = 0
        traversed: list[str] = []
        for segment in segments:
            matches = self._find_child_folders(current_cid, segment)
            traversed.append(segment)
            current_path = "/" + "/".join(traversed)
            if len(matches) > 1:
                raise UploadError(f"115 目录存在同名项，无法唯一解析：{current_path}")
            if matches:
                current_cid = matches[0]
                continue
            created = self.create_folder(current_cid, segment)
            if created.name != segment:
                raise UploadError(
                    f"115 创建目录后名称发生变化：{current_path} -> {created.name}"
                )
            current_cid = created.cid
        return current_cid

    def create_folder(self, parent_cid: int, name: str) -> RemoteFolder:
        """在指定父目录下创建一个空文件夹。

        Args:
            parent_cid: 父目录 CID；根目录为 0。
            name: 单层文件夹名称。

        Returns:
            115 返回的新文件夹条目。

        Raises:
            ValueError: CID 或文件夹名称非法。
            UploadError: 创建请求失败或响应字段无效。
        """
        if parent_cid < 0:
            raise ValueError("父目录 CID 不能为负数")
        if not name or name in {".", ".."} or "/" in name or "\x00" in name:
            raise ValueError("文件夹名称不能为空、.、..，且不能包含 / 或 NUL")
        data = self._request_data(
            "POST",
            f"{PRO_API}/open/folder/add",
            data={"pid": str(parent_cid), "file_name": name},
        )
        try:
            folder_id = int(data["file_id"])
            returned_name = str(data.get("file_name") or name)
        except (KeyError, TypeError, ValueError) as error:
            raise UploadError("115 新建文件夹响应字段无效") from error
        if folder_id <= 0:
            raise UploadError("115 新建文件夹返回了无效 CID")
        return RemoteFolder(folder_id, parent_cid, returned_name)

    def user_info(self) -> UserInfo:
        """获取当前账号的身份、空间用量与会员概要。

        Returns:
            标准化后的用户信息。

        Raises:
            UploadError: 请求失败或响应字段无效。
        """
        data = self._request_api_data(
            "GET",
            f"{PRO_API}/open/user/info",
            error_hint="读取 115 用户信息",
        )
        if not isinstance(data, dict):
            raise UploadError("115 用户信息响应格式无效")
        space = data.get("rt_space_info")
        vip = data.get("vip_info")
        if not isinstance(space, dict):
            space = {}
        if not isinstance(vip, dict):
            vip = {}

        def space_size(key: str) -> int:
            """读取 rt_space_info 单项中的字节数，缺失时按 0 处理。"""
            entry = space.get(key)
            return _tolerant_int(entry.get("size")) if isinstance(entry, dict) else 0

        return UserInfo(
            user_id=_tolerant_int(data.get("user_id")),
            user_name=str(data.get("user_name") or ""),
            total_size=space_size("all_total"),
            remain_size=space_size("all_remain"),
            use_size=space_size("all_use"),
            vip_level=str(vip.get("level_name") or ""),
            vip_expire=_tolerant_int(vip.get("expire")),
        )

    def folder_info(self, file_id: int) -> RemoteEntry:
        """按 ID 获取单个文件或文件夹的详情。

        Args:
            file_id: 文件或文件夹 ID，必须为正整数。

        Returns:
            标准化后的条目详情。

        Raises:
            ValueError: ID 非法。
            UploadError: 请求失败或响应字段无效。
        """
        if file_id <= 0:
            raise ValueError("文件 ID 必须为正整数")
        data = self._request_api_data(
            "GET",
            f"{PRO_API}/open/folder/get_info",
            error_hint="读取 115 文件详情",
            params={"file_id": str(file_id)},
        )
        if not isinstance(data, dict):
            raise UploadError("115 文件详情响应格式无效")
        try:
            name = str(data["file_name"] or "")
            entry_id = int(data["file_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise UploadError("115 文件详情响应字段无效") from error
        if not name or entry_id <= 0:
            raise UploadError("115 文件详情响应字段无效")
        # file_category 为字符串："0" 是文件夹，"1" 是文件。
        is_folder = str(data.get("file_category", "1")) == "0"
        paths = data.get("paths")
        parent_id = 0
        if isinstance(paths, list) and paths and isinstance(paths[-1], dict):
            parent_id = _tolerant_int(paths[-1].get("file_id"))
        return RemoteEntry(
            file_id=entry_id,
            parent_id=parent_id,
            name=name,
            is_folder=is_folder,
            size=0 if is_folder else _tolerant_int(data.get("file_size")),
            sha1=str(data.get("sha1") or "").upper(),
            pick_code=str(data.get("pick_code") or ""),
        )

    def move_entries(self, file_ids: Sequence[int], to_cid: int) -> None:
        """批量移动文件或文件夹到目标目录。

        Args:
            file_ids: 要移动的文件（夹） ID，至少一个且均为正整数。
            to_cid: 目标目录 CID；根目录为 0。

        Raises:
            ValueError: ID 或目标目录 CID 非法。
            UploadError: 115 拒绝移动或响应无效。
        """
        payload_ids = self._validate_entry_ids(file_ids)
        if to_cid < 0:
            raise ValueError("目标目录 CID 不能为负数")
        self._request_envelope(
            "POST",
            f"{PRO_API}/open/ufile/move",
            error_hint="移动 115 文件",
            data={"file_ids": ",".join(payload_ids), "to_cid": str(to_cid)},
        )

    def copy_entries(
        self,
        file_ids: Sequence[int],
        to_cid: int,
        *,
        no_dupli: bool = False,
    ) -> None:
        """批量复制文件或文件夹到目标目录。

        Args:
            file_ids: 要复制的文件（夹） ID，至少一个且均为正整数。
            to_cid: 目标目录 CID；根目录为 0。
            no_dupli: 目标目录存在同名项时是否拒绝复制；默认允许 115 自动改名。

        Raises:
            ValueError: ID 或目标目录 CID 非法。
            UploadError: 115 拒绝复制或响应无效。
        """
        payload_ids = self._validate_entry_ids(file_ids)
        if to_cid < 0:
            raise ValueError("目标目录 CID 不能为负数")
        self._request_envelope(
            "POST",
            f"{PRO_API}/open/ufile/copy",
            error_hint="复制 115 文件",
            data={
                "file_ids": ",".join(payload_ids),
                "pid": str(to_cid),
                "no_dupli": "1" if no_dupli else "0",
            },
        )

    def rename_entry(self, file_id: int, new_name: str) -> None:
        """重命名单个文件或文件夹。

        Args:
            file_id: 文件（夹） ID，必须为正整数。
            new_name: 新名称；不能为空、``.``、``..``，且不能包含 ``/`` 或 NUL。

        Raises:
            ValueError: ID 或新名称非法。
            UploadError: 115 拒绝重命名或响应无效。
        """
        if file_id <= 0:
            raise ValueError("文件 ID 必须为正整数")
        if not new_name or new_name in {".", ".."} or "/" in new_name or "\x00" in new_name:
            raise ValueError("新名称不能为空、.、..，且不能包含 / 或 NUL")
        self._request_envelope(
            "POST",
            f"{PRO_API}/open/ufile/update",
            error_hint="重命名 115 文件",
            data={"file_id": str(file_id), "file_name": new_name},
        )

    def download_url(self, file_id: int) -> DownloadLink:
        """获取单个文件的下载直链。

        先读取文件详情取得提取码，再按浏览器 UA 换取 CDN 签名直链；
        文件夹没有下载直链。

        Args:
            file_id: 文件 ID，必须为正整数。

        Returns:
            包含名称、大小与直链的结果。

        Raises:
            ValueError: ID 非法。
            UploadError: 文件不存在、是文件夹或响应无效。
        """
        entry = self.folder_info(file_id)
        if entry.is_folder:
            raise UploadError(f"文件夹没有下载直链：{entry.name}")
        if not entry.pick_code:
            raise UploadError(f"115 未返回文件提取码：{entry.name}")
        data = self._request_api_data(
            "POST",
            f"{PRO_API}/open/ufile/downurl",
            error_hint="获取 115 下载直链",
            data={"pick_code": entry.pick_code},
            headers=_BROWSER_HEADERS,
        )
        if not isinstance(data, dict) or not data:
            raise UploadError("115 下载直链响应格式无效")
        # data 以 file_id 为键；找不到对应键时兜底取唯一条目。
        item = data.get(str(file_id)) or next(iter(data.values()))
        if not isinstance(item, dict):
            raise UploadError("115 下载直链响应格式无效")
        url_container = item.get("url")
        url = url_container.get("url") if isinstance(url_container, dict) else None
        if not url:
            raise UploadError("115 未返回有效的下载直链")
        return DownloadLink(
            file_id=file_id,
            name=str(item.get("file_name") or entry.name),
            size=_tolerant_int(item.get("file_size")),
            sha1=str(item.get("sha1") or entry.sha1).upper(),
            url=str(url),
        )

    def list_trash_page(self, *, offset: int = 0, limit: int = 100) -> TrashPage:
        """分页读取 115 回收站条目。

        Args:
            offset: 从零开始的分页偏移。
            limit: 单页条数，范围为 1 到 200。

        Returns:
            包含总数和下一页偏移的回收站分页。

        Raises:
            ValueError: offset 或 limit 超出允许范围。
            UploadError: 请求失败或响应字段无效。
        """
        if offset < 0:
            raise ValueError("回收站 offset 不能为负数")
        if not 1 <= limit <= 200:
            raise ValueError("回收站 limit 必须在 1 到 200 之间")
        data = self._request_api_data(
            "GET",
            f"{PRO_API}/open/rb/list",
            error_hint="读取 115 回收站",
            params={"offset": offset, "limit": limit},
        )
        if not isinstance(data, dict):
            raise UploadError("115 回收站响应格式无效")
        # data 是「分页元信息 + 动态回收站 ID 键」的混合对象；保留键为官方分页字段。
        items: list[TrashItem] = []
        for key, value in data.items():
            if key in {"offset", "limit", "count", "rb_pass"}:
                continue
            if not isinstance(value, dict):
                raise UploadError("115 回收站条目响应格式无效")
            items.append(self._parse_trash_item(key, value))
        total = _tolerant_int(data.get("count"))
        consumed = len(items)
        next_offset = offset + consumed if consumed and offset + consumed < total else None
        return TrashPage(
            items=tuple(items),
            offset=offset,
            limit=limit,
            total=total,
            next_offset=next_offset,
        )

    def list_all_trash(self) -> list[TrashItem]:
        """读取回收站全部分页并返回去重后的全部条目。

        Returns:
            按回收站返回顺序去重后的全部条目。

        Raises:
            UploadError: 分页未前进或响应无效。
        """
        items: list[TrashItem] = []
        seen: set[str] = set()
        offset = 0
        while True:
            page = self.list_trash_page(offset=offset, limit=200)
            for item in page.items:
                if item.trash_id not in seen:
                    seen.add(item.trash_id)
                    items.append(item)
            if page.next_offset is None:
                return items
            if page.next_offset <= offset:
                raise UploadError("115 回收站分页偏移未前进")
            offset = page.next_offset

    def restore_trash(self, trash_ids: Sequence[str]) -> None:
        """把回收站条目恢复到原位置。

        Args:
            trash_ids: 回收站条目 ID，至少一个且均为纯数字字符串。

        Raises:
            ValueError: ID 非法。
            UploadError: 请求失败，或任一条目恢复失败。
        """
        tids = self._validate_trash_ids(trash_ids)
        envelope = self._request_envelope(
            "POST",
            f"{PRO_API}/open/rb/revert",
            error_hint="恢复 115 回收站条目",
            data={"tid": ",".join(tids)},
        )
        # 整体 state=true 时仍可能存在逐条失败；data 以 tid 为键回传各条结果。
        # 部分成功场景下 115 可能省略 data，此时只能信任整体状态，不能误报失败。
        data = envelope.get("data")
        if not isinstance(data, dict):
            return
        failures = [
            f"{tid}: {result.get('error') or '恢复失败'}"
            for tid, result in data.items()
            if isinstance(result, dict) and result.get("state") not in (True, 1)
        ]
        if failures:
            raise UploadError("部分回收站条目恢复失败：" + "；".join(failures))

    def purge_trash(self, trash_ids: Sequence[str]) -> None:
        """彻底删除回收站条目，不可恢复。

        Args:
            trash_ids: 回收站条目 ID，至少一个且均为纯数字字符串。

        Raises:
            ValueError: ID 非法。
            UploadError: 请求失败或 115 拒绝删除。
        """
        tids = self._validate_trash_ids(trash_ids)
        self._request_envelope(
            "POST",
            f"{PRO_API}/open/rb/del",
            error_hint="彻底删除 115 回收站条目",
            data={"tid": ",".join(tids)},
        )

    def add_offline_urls(
        self,
        urls: Sequence[str],
        *,
        wp_path_id: int = 0,
    ) -> list[OfflineUrlResult]:
        """添加链接离线下载任务。

        Args:
            urls: 一个或多个 HTTP/FTP/磁力/ED2K 链接；整体按行提交给 115。
            wp_path_id: 任务文件保存目录 CID；根目录为 0。

        Returns:
            与输入顺序一致的逐链接结果；单个链接失败不抛异常，
            由调用方根据 ``state`` 决定退出码。

        Raises:
            ValueError: 链接为空或目录 CID 非法。
            UploadError: 请求失败或响应格式无效。
        """
        cleaned = [str(url).strip() for url in urls]
        if not cleaned or any(not url for url in cleaned):
            raise ValueError("离线链接不能为空")
        if wp_path_id < 0:
            raise ValueError("保存目录 CID 不能为负数")
        data = self._request_api_data(
            "POST",
            f"{PRO_API}/open/offline/add_task_urls",
            error_hint="添加 115 离线任务",
            data={"urls": "\n".join(cleaned), "wp_path_id": str(wp_path_id)},
        )
        if not isinstance(data, list) or len(data) != len(cleaned):
            # 请求已发出，任务可能已按链接部分创建；不能让调用方误以为什么都没发生。
            raise UploadError(
                "115 离线任务响应与链接数不一致；"
                "任务可能已部分创建，请用 offline list 确认"
            )
        results: list[OfflineUrlResult] = []
        for url, item in zip(cleaned, data):
            if not isinstance(item, dict):
                raise UploadError("115 离线任务响应格式无效")
            results.append(
                OfflineUrlResult(
                    url=url,
                    info_hash=str(item.get("info_hash") or ""),
                    state=item.get("state") in (True, 1),
                    message=str(item.get("message") or ""),
                )
            )
        return results

    def list_offline_tasks_page(self, page: int = 1) -> OfflineTaskPage:
        """分页读取云下载（离线下载）任务列表。

        Args:
            page: 从 1 开始的页码。

        Returns:
            包含分页信息的标准化任务页。

        Raises:
            ValueError: 页码非法。
            UploadError: 请求失败或响应字段无效。
        """
        if page <= 0:
            raise ValueError("任务页码必须大于 0")
        data = self._request_api_data(
            "GET",
            f"{PRO_API}/open/offline/get_task_list",
            error_hint="读取 115 离线任务",
            params={"page": page},
        )
        if not isinstance(data, dict):
            raise UploadError("115 离线任务响应格式无效")
        raw_tasks = data.get("tasks")
        if not isinstance(raw_tasks, list):
            raise UploadError("115 离线任务响应缺少 tasks 列表")
        tasks: list[OfflineTask] = []
        for item in raw_tasks:
            if not isinstance(item, dict):
                raise UploadError("115 离线任务条目响应格式无效")
            tasks.append(
                OfflineTask(
                    info_hash=str(item.get("info_hash") or ""),
                    name=str(item.get("name") or ""),
                    size=_tolerant_int(item.get("size")),
                    status=_tolerant_int(item.get("status")),
                    percent_done=_tolerant_int(item.get("percentDone")),
                    add_time=_tolerant_int(item.get("add_time")),
                    last_update=_tolerant_int(item.get("last_update")),
                    file_id=str(item.get("file_id") or ""),
                    delete_file_id=str(item.get("delete_file_id") or ""),
                    url=str(item.get("url") or ""),
                    wp_path_id=str(item.get("wp_path_id") or ""),
                )
            )
        return OfflineTaskPage(
            page=_tolerant_int(data.get("page"), page),
            page_count=_tolerant_int(data.get("page_count"), 1),
            count=_tolerant_int(data.get("count")),
            tasks=tuple(tasks),
        )

    def offline_quota(self) -> OfflineQuota:
        """查询云下载（离线下载）配额。

        Returns:
            剩余、已用与总量配额。

        Raises:
            UploadError: 请求失败或响应格式无效。
        """
        data = self._request_api_data(
            "GET",
            f"{PRO_API}/open/offline/get_quota_info",
            error_hint="读取 115 离线配额",
        )
        if not isinstance(data, dict):
            raise UploadError("115 离线配额响应格式无效")
        return OfflineQuota(
            surplus=_tolerant_int(data.get("surplus")),
            used=_tolerant_int(data.get("used")),
            count=_tolerant_int(data.get("count")),
        )

    def delete_offline_task(self, info_hash: str, *, delete_files: bool = False) -> None:
        """删除一个云下载任务。

        Args:
            info_hash: 任务 SHA1。
            delete_files: 是否同时删除任务已下载到网盘的源文件；默认仅移除任务。

        Raises:
            ValueError: info_hash 为空。
            UploadError: 请求失败或 115 拒绝删除。
        """
        text = str(info_hash).strip()
        if not text:
            raise ValueError("任务 info_hash 不能为空")
        self._request_envelope(
            "POST",
            f"{PRO_API}/open/offline/del_task",
            error_hint="删除 115 离线任务",
            data={"info_hash": text, "del_source_file": "1" if delete_files else "0"},
        )

    def clear_offline_tasks(self, *, flag: int = 1) -> None:
        """按类型清空云下载任务。

        Args:
            flag: 清除类型；0 已完成、1 全部、2 失败、3 进行中、
                4 已完成并删源文件、5 全部并删源文件。

        Raises:
            ValueError: flag 超出允许范围。
            UploadError: 请求失败或 115 拒绝清空。
        """
        if flag not in (0, 1, 2, 3, 4, 5):
            raise ValueError("清除类型 flag 必须在 0 到 5 之间")
        self._request_envelope(
            "POST",
            f"{PRO_API}/open/offline/clear_task",
            error_hint="清空 115 离线任务",
            data={"flag": str(flag)},
        )

    def add_offline_torrent(
        self,
        torrent: Path,
        *,
        wp_path_id: int = 0,
        wanted: str = "",
        progress_output: Callable[[str], None] = print,
    ) -> str:
        """上传种子文件并创建 BT 离线任务。

        流程：把 ``.torrent`` 上传到保存目录 → 确认远端文件并读取提取码
        → 解析种子 → 提交任务。种子文件会作为普通文件保留在保存目录。

        Args:
            torrent: 本地 ``.torrent`` 种子文件。
            wp_path_id: 任务保存目录 CID；种子文件也上传到该目录。
            wanted: 仅下载的文件下标，逗号分隔；空字符串表示全部文件。
            progress_output: 种子上传进度输出函数。

        Returns:
            新任务的 info_hash。

        Raises:
            ValueError: 种子路径或参数非法。
            UploadError: 上传确认、种子解析或任务创建失败。
        """
        torrent = torrent.expanduser().resolve()
        if not torrent.exists():
            raise FileNotFoundError(f"指定种子文件不存在：{torrent}")
        if not torrent.is_file():
            raise ValueError(f"指定路径不是普通文件：{torrent}")
        if wanted and not re.fullmatch(r"\d+(,\d+)*", wanted):
            raise ValueError("wanted 必须是逗号分隔的文件下标，或留空表示全部")
        result = self.upload(
            torrent,
            cid=wp_path_id,
            progress_output=progress_output,
        )
        uploaded = next(
            (
                item
                for item in self.list_all_files(wp_path_id)
                if item.name == result.remote_name and item.sha1 == result.file_sha1
            ),
            None,
        )
        if uploaded is None:
            raise UploadError("种子文件上传后未能在目标目录中确认，已中止 BT 任务创建")
        entry = self.folder_info(uploaded.file_id)
        if entry.is_folder or not entry.pick_code:
            raise UploadError("115 未返回种子文件提取码")
        parsed = self._request_api_data(
            "POST",
            f"{PRO_API}/open/offline/torrent",
            error_hint="解析 BT 种子",
            data={"torrent_sha1": result.file_sha1, "pick_code": entry.pick_code},
        )
        if not isinstance(parsed, dict):
            raise UploadError("BT 种子解析响应格式无效")
        info_hash = str(parsed.get("info_hash") or "")
        if not info_hash:
            raise UploadError("BT 种子解析响应缺少 info_hash")
        self._request_envelope(
            "POST",
            f"{PRO_API}/open/offline/add_task_bt",
            error_hint="添加 115 BT 离线任务",
            data={
                "info_hash": info_hash,
                "wanted": wanted,
                "save_path": str(wp_path_id),
                "torrent_sha1": result.file_sha1,
                "pick_code": entry.pick_code,
                "wp_path_id": str(wp_path_id),
            },
        )
        return info_hash

    @staticmethod
    def _validate_entry_ids(file_ids: Sequence[int]) -> list[str]:
        """校验批量文件（夹） ID 并转换为去重保序的字符串列表。

        Args:
            file_ids: 调用方传入的 ID 序列。

        Returns:
            去重保序后的字符串 ID 列表。

        Raises:
            ValueError: 列表为空或存在非法 ID。
        """
        unique: list[str] = []
        for file_id in file_ids:
            if file_id <= 0:
                raise ValueError("文件 ID 必须为正整数")
            text = str(file_id)
            if text not in unique:
                unique.append(text)
        if not unique:
            raise ValueError("至少需要一个文件 ID")
        return unique

    @staticmethod
    def _validate_trash_ids(trash_ids: Sequence[str]) -> list[str]:
        """校验回收站条目 ID 并转换为去重保序的字符串列表。

        Args:
            trash_ids: 调用方传入的回收站条目 ID 序列。

        Returns:
            去重保序后的字符串 ID 列表。

        Raises:
            ValueError: 列表为空或存在非法 ID。
        """
        unique: list[str] = []
        for trash_id in trash_ids:
            text = str(trash_id).strip()
            if not text or not text.isdigit():
                raise ValueError("回收站条目 ID 必须是纯数字字符串")
            if text not in unique:
                unique.append(text)
        if not unique:
            raise ValueError("至少需要一个回收站条目 ID")
        return unique

    @staticmethod
    def _parse_trash_item(trash_id: str, item: dict[str, Any]) -> TrashItem:
        """把回收站动态键条目转换为统一模型。

        Args:
            trash_id: 回收站条目在响应对象中的动态键。
            item: 条目原始字段。

        Returns:
            标准化后的回收站条目。

        Raises:
            UploadError: 条目缺少名称等必需字段。
        """
        try:
            name = str(item["file_name"] or "")
        except (KeyError, TypeError) as error:
            raise UploadError("115 回收站条目响应字段无效") from error
        if not name:
            raise UploadError("115 回收站条目响应字段无效")
        # type 为字符串："1" 是文件，"2" 是文件夹。
        is_folder = str(item.get("type", "1")) == "2"
        return TrashItem(
            trash_id=str(item.get("id") or trash_id),
            name=name,
            is_folder=is_folder,
            size=0 if is_folder else _tolerant_int(item.get("file_size")),
            deleted_time=_tolerant_int(item.get("dtime")),
            parent_cid=_tolerant_int(item.get("cid")),
            parent_name=str(item.get("parent_name") or ""),
            pick_code=str(item.get("pick_code") or ""),
            status=_tolerant_int(item.get("status")),
        )

    @staticmethod
    def _remote_dir_segments(remote_dir: str) -> list[str]:
        """校验 115 绝对目录路径并返回有效层级。"""
        if not remote_dir.startswith("/"):
            raise ValueError("115 目录路径必须以 / 开头")
        segments = [segment for segment in remote_dir.split("/") if segment not in ("", ".")]
        if any(segment == ".." for segment in segments):
            raise ValueError("115 目录路径不能包含 ..")
        return segments

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

    def _request_envelope(
        self,
        method: str,
        url: str,
        *,
        error_hint: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """执行 Bearer 管理端点请求并返回通过状态校验的完整信封。

        Args:
            method: HTTP 方法。
            url: 115 开放平台端点。
            error_hint: 组装错误消息用的动作描述，例如 ``移动 115 文件``。
            **kwargs: 透传给 HTTP 客户端的其余参数。

        Returns:
            ``state`` 为真的响应信封；可能没有 ``data`` 字段。

        Raises:
            UploadError: 网络、HTTP 或业务状态失败。
        """
        try:
            # 允许调用方追加端点专属请求头（如 downurl 的浏览器 UA），与默认 Bearer 头合并。
            headers = {**self.headers, **kwargs.pop("headers", {})}
            response = self.http.request(method, url, headers=headers, **kwargs)
            response.raise_for_status()
            envelope = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise UploadError(f"{error_hint}请求失败") from error
        if not isinstance(envelope, dict) or envelope.get("state") not in (True, 1):
            message = envelope.get("message") if isinstance(envelope, dict) else None
            raise UploadError(str(message or f"{error_hint}失败"))
        return envelope

    def _request_api_data(
        self,
        method: str,
        url: str,
        *,
        error_hint: str,
        **kwargs: Any,
    ) -> Any:
        """执行 Bearer 管理端点请求并返回成功响应的 ``data`` 字段。

        Args:
            method: HTTP 方法。
            url: 115 开放平台端点。
            error_hint: 组装错误消息用的动作描述。
            **kwargs: 透传给 HTTP 客户端的其余参数。

        Returns:
            信封中的 ``data`` 字段；可能是对象或数组，由调用方校验具体形态。

        Raises:
            UploadError: 网络、HTTP、业务状态失败或缺少 ``data`` 字段。
        """
        envelope = self._request_envelope(method, url, error_hint=error_hint, **kwargs)
        if "data" not in envelope:
            raise UploadError(f"{error_hint}响应缺少 data 字段")
        return envelope["data"]

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
            if upload_error is error:
                # 回调业务失败时 error 本身就是 UploadError，两者是同一对象；
                # raise error from error 会让 __cause__ 指向自身形成自环，
                # 上层 _is_retryable 沿 __cause__ 遍历将永不终止（纯用户态死循环）。
                # 此处必须裸 raise，同时保留原始 traceback。
                raise
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
