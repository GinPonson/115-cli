"""CLI 客户端装配测试。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_expand_upload_sources_supports_files_directories_and_globs(
    tmp_path: Path,
) -> None:
    """文件、递归目录和带引号通配符应展开、稳定排序并按绝对路径去重。"""
    direct = tmp_path / "直接文件.txt"
    direct.write_text("direct", encoding="utf-8")
    folder = tmp_path / "文件夹"
    nested = folder / "子目录"
    nested.mkdir(parents=True)
    first = folder / "a.txt"
    second = nested / "b.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")

    result = cli.expand_upload_sources(
        [
            direct,
            folder,
            Path(f"{folder}/**/*.txt"),
        ]
    )

    assert result == [direct.resolve(), first.resolve(), second.resolve()]


def test_expand_upload_sources_treats_existing_special_name_as_literal(
    tmp_path: Path,
) -> None:
    """已存在的特殊字符文件名必须按字面处理，不能再次执行通配符展开。"""
    special = tmp_path / "报告 [最终]*?.txt"
    special.write_text("content", encoding="utf-8")

    assert cli.expand_upload_sources([special]) == [special.resolve()]


def test_expand_upload_sources_rejects_unmatched_glob(tmp_path: Path) -> None:
    """通配符无匹配项时必须明确报错，不能静默跳过输入。"""
    pattern = Path(f"{tmp_path}/**/*.missing")

    with pytest.raises(FileNotFoundError, match="通配符未匹配任何路径"):
        cli.expand_upload_sources([pattern])


def test_expand_upload_sources_rejects_empty_directory(tmp_path: Path) -> None:
    """空目录没有可执行的上传工作，应向调用方暴露错误。"""
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(ValueError, match="没有可上传的文件"):
        cli.expand_upload_sources([empty])
