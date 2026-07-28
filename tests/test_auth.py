"""115 OAuth 设备码认证测试。"""

from pathlib import Path

import httpx

from u115_uploader.auth import OAuthTokens, OpenAuthClient, TokenStore


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
