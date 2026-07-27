"""CLI 客户端装配测试。"""

from pathlib import Path
from types import SimpleNamespace

from u115_uploader import cli


def test_load_tokens_uses_local_auth_flow(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """CLI 必须使用本项目认证模块取得 token，再创建开放平台客户端。"""
    tokens = SimpleNamespace(access_token="access", refresh_token="refresh")
    captured = {}

    class FakeAuth:
        """返回固定 token 的认证替身。"""

        def ensure_tokens(self, store):
            captured["store"] = store
            return tokens

    monkeypatch.setattr(cli, "OpenAuthClient", FakeAuth)

    token_path = tmp_path / "tokens.json"
    result = cli.load_tokens(token_path)

    assert result is tokens
    assert captured["store"].path == token_path.resolve()
