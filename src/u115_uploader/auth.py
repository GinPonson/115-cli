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

CLIENT_ID = "100195137"
CODE_VERIFIER = "0" * 64
CODE_CHALLENGE = base64.b64encode(
    hashlib.sha256(CODE_VERIFIER.encode("ascii")).digest()
).decode("ascii")
AUTH_BASE_URL = "https://qrcodeapi.115.com"
REFRESH_URL = "https://passportapi.115.com/open/refreshToken"
DEFAULT_QR_TTL_SECONDS = 5 * 60
REFRESH_EARLY_SECONDS = 5 * 60


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

    def ensure_tokens(self, store: TokenStore) -> OAuthTokens:
        """复用有效令牌、刷新即将过期令牌，或启动二维码登录。"""
        tokens = store.load()
        if tokens is None:
            tokens = self.login()
            store.save(tokens)
            return tokens
        if tokens.expires_at <= int(time.time()) + REFRESH_EARLY_SECONDS:
            tokens = self.refresh(tokens)
            store.save(tokens)
        return tokens

    def login(self, ttl_seconds: int = DEFAULT_QR_TTL_SECONDS) -> OAuthTokens:
        """申请二维码并阻塞轮询，直到确认、拒绝或过期。"""
        if ttl_seconds <= 0:
            raise ValueError("二维码有效期必须大于零")
        device = self._request_json(
            "POST",
            f"{AUTH_BASE_URL}/open/authDeviceCode",
            data={
                "client_id": CLIENT_ID,
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
