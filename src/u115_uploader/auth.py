"""115 开放平台 OAuth 设备码登录。

流程与 StarVault 保持一致：

1. ``POST /open/authDeviceCode`` 申请设备码；
2. 在终端本地渲染服务端返回的扫码 URL；
3. ``GET /get/status/`` 轮询扫码与确认状态；
4. ``POST /open/deviceCodeToToken`` 换取访问令牌；
5. 令牌过期前通过 ``POST /open/refreshToken`` 刷新并原子持久化。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
import qrcode

@dataclass(frozen=True, slots=True)
class ClientApp:
    """115 开放平台应用身份。

    应用身份决定设备码授权时使用的 ``client_id``，不同应用拥有独立的
    授权记录与权限范围；令牌签发后刷新不再依赖该值。

    Attributes:
        client_id: 开放平台 AppID，作为设备码接口的 ``client_id`` 参数。
        name: 面向用户展示的应用名称。
    """

    client_id: str
    name: str


# 115 开放平台已公开的应用身份。名称与 AppID 对齐 p115client 的 OPEN_APP_IDS，
# 与 Sirius（CLIENT_APPS）和 StarVault（OpenAuthApps.All）保持一致，便于跨端识别同一身份。
CLIENT_APPS: tuple[ClientApp, ...] = (
    ClientApp("100195125", "媒体播放器"),
    ClientApp("100195127", "蒙太奇播放器"),
    ClientApp("100195129", "飞牛私有云-挂载"),
    ClientApp("100195133", "恒星播放器"),
    ClientApp("100195135", "网易爆米花"),
    ClientApp("100195137", "VidHub 播放器"),
    ClientApp("100195145", "Filebar"),
    ClientApp("100195149", "DS Cloud"),
    ClientApp("100195151", "PurePhoto"),
    ClientApp("100195177", "CloudDrive2"),
    ClientApp("100195181", "Infuse"),
    ClientApp("100195199", "极空间"),
    ClientApp("100195201", "Moon Player"),
    ClientApp("100195213", "当贝播放器"),
    ClientApp("100195225", "115生活 App"),
    ClientApp("100195271", "飞牛私有云"),
    ClientApp("100195295", "rclone 修改版"),
    ClientApp("100195303", "OpenList"),
    ClientApp("100195307", "115FileSync"),
    ClientApp("100195313", "CloudDrive"),
    ClientApp("100195389", "115开放平台"),
)

# 默认沿用 VidHub 播放器，保持既有登录行为不变。
DEFAULT_CLIENT_ID = "100195137"

CODE_VERIFIER = "0" * 64
CODE_CHALLENGE = base64.b64encode(
    hashlib.sha256(CODE_VERIFIER.encode("ascii")).digest()
).decode("ascii")
AUTH_BASE_URL = "https://qrcodeapi.115.com"
REFRESH_URL = "https://passportapi.115.com/open/refreshToken"
DEFAULT_QR_TTL_SECONDS = 5 * 60
REFRESH_EARLY_SECONDS = 5 * 60


def resolve_client_app(value: str) -> ClientApp:
    """按 AppID 或应用名称解析开放平台应用身份。

    Args:
        value: 用户输入，可以是 ``100195125`` 这类 AppID，也可以是
            ``媒体播放器`` 这类应用名称（忽略首尾空白，名称不区分大小写）。

    Returns:
        唯一匹配的应用身份。

    Raises:
        ValueError: 输入为空或未匹配任何已知应用；错误消息会列出全部可选 AppID，
            避免用户凭猜测传入非法值后才在 115 侧看到晦涩错误。
    """
    candidate = value.strip()
    if not candidate:
        raise ValueError("client_id 不能为空")
    for app in CLIENT_APPS:
        if app.client_id == candidate:
            return app
    folded = candidate.casefold()
    for app in CLIENT_APPS:
        if app.name.casefold() == folded:
            return app
    available = "、".join(app.client_id for app in CLIENT_APPS)
    raise ValueError(f"未知的 115 开放平台应用：{value}；可用 AppID：{available}")


def find_client_app(client_id: str) -> ClientApp | None:
    """按 AppID 精确查找应用身份。

    Args:
        client_id: 开放平台 AppID。

    Returns:
        匹配的应用身份；未收录时返回 ``None``，便于调用方展示原始 AppID。
    """
    for app in CLIENT_APPS:
        if app.client_id == client_id:
            return app
    return None


class AuthenticationError(RuntimeError):
    """115 登录协议或业务状态错误。"""


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    """可持久化的 115 OAuth 令牌。

    Attributes:
        access_token: Bearer 访问令牌。
        refresh_token: 一次性轮换的刷新令牌。
        expires_at: access token 的 Unix 过期时间。
        user_id: 115 用户 ID。
        user_name: 115 用户昵称。
    """

    access_token: str
    refresh_token: str
    expires_at: int
    user_id: int = 0
    user_name: str = ""

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> "OAuthTokens":
        """从换取或刷新响应构造严格校验后的令牌。"""
        access_token = str(data.get("access_token") or "")
        refresh_token = str(data.get("refresh_token") or "")
        if not access_token or not refresh_token:
            raise AuthenticationError("115 登录响应缺少 access_token 或 refresh_token")
        expires_in = int(data.get("expires_in") or 7200)
        if expires_in <= 0:
            raise AuthenticationError("115 登录响应的 expires_in 无效")
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=int(time.time()) + expires_in,
            user_id=int(data.get("user_id") or 0),
            user_name=str(data.get("user_name") or ""),
        )


class TokenStore:
    """以仅当前用户可读写的 JSON 文件保存 OAuth 令牌。"""

    def __init__(self, path: Path) -> None:
        """初始化令牌存储。

        Args:
            path: JSON 文件路径。
        """
        self.path = path.expanduser().resolve()

    def load(self) -> OAuthTokens | None:
        """读取令牌；文件不存在时返回 ``None``，损坏时明确报错。"""
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return OAuthTokens(
                access_token=str(payload["access_token"]),
                refresh_token=str(payload["refresh_token"]),
                expires_at=int(payload["expires_at"]),
                user_id=int(payload.get("user_id") or 0),
                user_name=str(payload.get("user_name") or ""),
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise AuthenticationError(f"登录态文件无效：{self.path}") from error

    def save(self, tokens: OAuthTokens) -> None:
        """以 0600 权限原子写入令牌，避免中断留下半个 JSON。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(asdict(tokens), stream, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except BaseException:
            # 临时文件不含其他用户数据，失败时清理，原令牌文件仍保持完整。
            temporary.unlink(missing_ok=True)
            raise


class OpenAuthClient:
    """115 OAuth 设备码与刷新令牌客户端。"""

    def __init__(
        self,
        *,
        http_client: httpx.Client | None = None,
        output: Callable[[str], None] = print,
        qr_output: Path | None = None,
    ) -> None:
        """初始化认证客户端。

        Args:
            http_client: 可注入的同步 HTTP 客户端。
            output: 状态与二维码输出函数。
            qr_output: 可选的二维码 PNG 导出路径。
        """
        self._http = http_client or httpx.Client(timeout=httpx.Timeout(65.0, connect=15.0))
        self._output = output
        self._qr_output = qr_output.expanduser().resolve() if qr_output else None

    def ensure_tokens(
        self,
        store: TokenStore,
        *,
        client_id: str = DEFAULT_CLIENT_ID,
    ) -> OAuthTokens:
        """复用有效令牌、刷新即将过期令牌，或启动二维码登录。

        Args:
            store: 令牌持久化文件。
            client_id: 仅在需要新登录时使用的开放平台 AppID；已有令牌直接复用，
                不会因为该参数变化而重新授权。

        Returns:
            有效的 ``OAuthTokens``。
        """
        tokens = store.load()
        if tokens is None:
            tokens = self.login(client_id=client_id)
            store.save(tokens)
            return tokens
        if tokens.expires_at <= int(time.time()) + REFRESH_EARLY_SECONDS:
            tokens = self.refresh(tokens)
            store.save(tokens)
        return tokens

    def login(
        self,
        ttl_seconds: int = DEFAULT_QR_TTL_SECONDS,
        *,
        client_id: str = DEFAULT_CLIENT_ID,
    ) -> OAuthTokens:
        """申请二维码并阻塞轮询，直到确认、拒绝或过期。

        Args:
            ttl_seconds: 二维码有效期，单位为秒。
            client_id: 开放平台 AppID，决定本次授权记录归属的应用身份。

        Returns:
            换取到的 OAuth 令牌。

        Raises:
            ValueError: 有效期非正数。
            AuthenticationError: 协议或业务状态错误。
        """
        if ttl_seconds <= 0:
            raise ValueError("二维码有效期必须大于零")
        device = self._request_json(
            "POST",
            f"{AUTH_BASE_URL}/open/authDeviceCode",
            data={
                "client_id": client_id,
                "code_challenge": CODE_CHALLENGE,
                "code_challenge_method": "sha256",
            },
        )
        uid = str(device.get("uid") or "")
        qrcode_url = str(device.get("qrcode") or "")
        sign = str(device.get("sign") or "")
        server_time = int(device.get("time") or 0)
        if not uid or not qrcode_url or not sign:
            raise AuthenticationError("115 未返回完整的二维码登录信息")

        self._output("请使用 115 App 扫描二维码并确认登录：")
        # 保留标准要求的 4 模块静区，避免二维码紧贴终端字符而无法被相机定位。
        qr = qrcode.QRCode(border=4)
        qr.add_data(qrcode_url)
        # TTY 模式会显式设置前景色与背景色，避免深色终端把二维码显示成反色。
        qr.print_ascii(out=None, tty=sys.stdout.isatty())
        if self._qr_output is not None:
            self._save_qr_image(qr, self._qr_output)
            self._output(f"二维码图片已保存：{self._qr_output}")

        deadline = time.monotonic() + ttl_seconds
        scanned = False
        while time.monotonic() < deadline:
            status_data = self._request_json(
                "GET",
                f"{AUTH_BASE_URL}/get/status/",
                params={
                    "uid": uid,
                    "time": server_time,
                    "sign": sign,
                    "_": int(time.time() * 1000),
                },
            )
            status = int(status_data.get("status") or 0)
            if status == 0:
                # 状态接口通常会长轮询；若服务端提前返回，主动限频避免忙循环。
                time.sleep(2)
                continue
            if status == 1:
                if not scanned:
                    self._output("二维码已扫描，请在 115 App 中确认登录。")
                    scanned = True
                time.sleep(2)
                continue
            if status == 2:
                token_data = self._request_json(
                    "POST",
                    f"{AUTH_BASE_URL}/open/deviceCodeToToken",
                    data={"uid": uid, "code_verifier": CODE_VERIFIER},
                )
                return OAuthTokens.from_response(token_data)
            if status == -1:
                raise AuthenticationError("二维码已过期")
            if status == -2:
                raise AuthenticationError("用户取消了二维码登录")
            raise AuthenticationError(f"115 返回未知二维码状态：{status}")
        raise AuthenticationError("二维码登录超时")

    @staticmethod
    def _save_qr_image(qr: qrcode.QRCode, output_path: Path) -> None:
        """将认证二维码保存为 PNG。

        Args:
            qr: 已包含认证 URL 的二维码对象。
            output_path: 输出路径，扩展名必须为 ``.png``。

        Raises:
            ValueError: 输出扩展名不是 PNG。
            AuthenticationError: 目录创建或图片写入失败。
        """
        if output_path.suffix.lower() != ".png":
            raise ValueError("认证二维码导出路径必须使用 .png 扩展名")
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            image = qr.make_image(fill_color="black", back_color="white")
            image.save(output_path, format="PNG")
        except OSError as error:
            raise AuthenticationError(f"认证二维码图片写入失败：{output_path}") from error

    def refresh(self, tokens: OAuthTokens) -> OAuthTokens:
        """使用一次性 refresh token 换取新令牌。"""
        data = self._request_json(
            "POST",
            REFRESH_URL,
            data={"refresh_token": tokens.refresh_token},
        )
        refreshed = OAuthTokens.from_response(data)
        # 刷新响应通常不含用户资料，保留既有信息。
        return OAuthTokens(
            access_token=refreshed.access_token,
            refresh_token=refreshed.refresh_token,
            expires_at=refreshed.expires_at,
            user_id=tokens.user_id,
            user_name=tokens.user_name,
        )

    def _request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """调用 115 JSON 接口并只返回成功响应的 ``data`` 对象。"""
        try:
            response = self._http.request(method, url, **kwargs)
            response.raise_for_status()
            envelope = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise AuthenticationError("115 登录网络请求失败") from error
        if not isinstance(envelope, dict):
            raise AuthenticationError("115 登录响应格式无效")
        state = envelope.get("state")
        if state not in (True, 1):
            message = envelope.get("message") or envelope.get("error") or "115 登录失败"
            raise AuthenticationError(str(message))
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise AuthenticationError("115 登录响应缺少 data 对象")
        return data
