"""115 OAuth 设备码认证测试。"""

import time
from pathlib import Path

import httpx

import pytest

from u115_uploader.auth import (
    CLIENT_APPS,
    DEFAULT_CLIENT_ID,
    OAuthTokens,
    OpenAuthClient,
    TokenStore,
    find_client_app,
    resolve_client_app,
)


def test_token_store_roundtrip_uses_private_permissions(tmp_path: Path) -> None:
    """令牌应完整读回，且文件只允许当前用户读写。"""
    store = TokenStore(tmp_path / "tokens.json")
    tokens = OAuthTokens("access", "refresh", 123456789, 1, "tester")

    store.save(tokens)

    assert store.load() == tokens
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_login_runs_device_code_status_and_exchange(monkeypatch) -> None:
    """确认状态为 2 时必须用 code verifier 换取 token。"""
    requests = []
    responses = iter(
        [
            {"state": 1, "data": {
                "uid": "uid",
                "time": 1,
                "sign": "sign",
                "qrcode": "https://115.com/scan/dg-uid",
            }},
            {"state": 1, "data": {"status": 2}},
            {"state": 1, "data": {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 7200,
            }},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=next(responses))

    # 测试不渲染真实二维码矩阵，只验证 OAuth 请求顺序。
    monkeypatch.setattr(
        "qrcode.QRCode.print_ascii",
        lambda self, out=None, tty=False: None,
    )
    client = OpenAuthClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        output=lambda _message: None,
    )

    tokens = client.login(ttl_seconds=30)

    assert tokens.access_token == "access"
    assert [request.url.path for request in requests] == [
        "/open/authDeviceCode",
        "/get/status/",
        "/open/deviceCodeToToken",
    ]


def test_login_exports_qr_as_png(monkeypatch, tmp_path: Path) -> None:
    """指定导出路径时，应生成可读取的 PNG 二维码图片。"""
    responses = iter(
        [
            {"state": 1, "data": {
                "uid": "uid",
                "time": 1,
                "sign": "sign",
                "qrcode": "https://115.com/scan/dg-uid",
            }},
            {"state": 1, "data": {"status": 2}},
            {"state": 1, "data": {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 7200,
            }},
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    monkeypatch.setattr(
        "qrcode.QRCode.print_ascii",
        lambda self, out=None, tty=False: None,
    )
    output_path = tmp_path / "nested" / "login.png"
    client = OpenAuthClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        output=lambda _message: None,
        qr_output=output_path,
    )

    client.login(ttl_seconds=30)

    assert output_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_client_app_catalog_covers_default_and_is_unique() -> None:
    """应用清单必须包含默认应用，且 AppID 与名称都不重复。"""
    assert DEFAULT_CLIENT_ID in {app.client_id for app in CLIENT_APPS}
    assert len({app.client_id for app in CLIENT_APPS}) == len(CLIENT_APPS)
    assert len({app.name for app in CLIENT_APPS}) == len(CLIENT_APPS)


def test_resolve_client_app_accepts_appid_and_name() -> None:
    """AppID 与应用名称都应解析到同一个应用身份。"""
    by_id = resolve_client_app("100195125")
    by_name = resolve_client_app("媒体播放器")

    assert by_id == by_name
    assert by_id.client_id == "100195125"
    # 名称匹配不区分大小写，并忽略首尾空白。
    assert resolve_client_app("  openlist ").client_id == "100195303"


def test_resolve_client_app_rejects_unknown_value() -> None:
    """未知应用必须明确报错，并列出可用 AppID 帮助用户纠正。"""
    with pytest.raises(ValueError) as captured:
        resolve_client_app("999999")

    message = str(captured.value)
    assert "未知的 115 开放平台应用" in message
    assert "100195125" in message


def test_find_client_app_returns_none_for_unlisted_id() -> None:
    """未收录的 AppID 不应抛错，交由调用方回显原始值。"""
    assert find_client_app("100195125").name == "媒体播放器"
    assert find_client_app("123456789") is None


def test_login_uses_given_client_id(monkeypatch) -> None:
    """login 必须把指定的 client_id 传给设备码接口，而不是硬编码默认值。"""
    captured: list[str] = []
    responses = iter(
        [
            {"state": 1, "data": {
                "uid": "uid",
                "time": 1,
                "sign": "sign",
                "qrcode": "https://115.com/scan/dg-uid",
            }},
            {"state": 1, "data": {"status": 2}},
            {"state": 1, "data": {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 7200,
            }},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open/authDeviceCode":
            captured.append(request.content.decode())
        return httpx.Response(200, json=next(responses))

    monkeypatch.setattr(
        "qrcode.QRCode.print_ascii",
        lambda self, out=None, tty=False: None,
    )
    client = OpenAuthClient(
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        output=lambda _message: None,
    )

    client.login(ttl_seconds=30, client_id="100195303")

    assert len(captured) == 1
    assert "client_id=100195303" in captured[0]


def test_ensure_tokens_reuses_existing_token_regardless_of_client_id(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """已有有效令牌时必须直接复用，不因传入 client_id 而触发重新扫码。"""
    store = TokenStore(tmp_path / "tokens.json")
    # 有效期充足，确保走"直接复用"分支。
    store.save(OAuthTokens("access", "refresh", int(time.time()) + 3600, 1, "tester"))

    client = OpenAuthClient(
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: pytest.fail("不应发起任何网络请求")
            )
        ),
        output=lambda _message: None,
    )

    tokens = client.ensure_tokens(store, client_id="100195303")

    assert tokens.access_token == "access"
