"""CLI 客户端装配测试。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from u115_uploader import cli
from u115_uploader.uploader import RemoteFile, RemoteFilePage, RemoteFolder


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


def test_collect_remote_folders_recursively_builds_paths() -> None:
    """递归目录列表应保留完整路径，并按父子关系深度优先输出。"""
    class FakeUploader:
        """按父 CID 返回固定目录树的客户端替身。"""

        def list_child_folders(self, parent_cid: int) -> list[RemoteFolder]:
            """返回测试目录树中指定父目录的直接子节点。"""
            return {
                0: [
                    RemoteFolder(cid=10, parent_cid=0, name="相册"),
                    RemoteFolder(cid=20, parent_cid=0, name="视频"),
                ],
                10: [RemoteFolder(cid=11, parent_cid=10, name="旅行")],
                11: [],
                20: [],
            }[parent_cid]

    rows = cli.collect_remote_folders(
        FakeUploader(),
        parent_cid=0,
        parent_path="/",
        recursive=True,
    )

    assert [(folder.cid, path) for folder, path in rows] == [
        (10, "/相册"),
        (11, "/相册/旅行"),
        (20, "/视频"),
    ]


def test_folders_parser_supports_path_recursive_and_search() -> None:
    """folders 子命令应暴露路径遍历、递归和全局搜索参数。"""
    parser = cli.build_parser()

    recursive = parser.parse_args(["folders", "/相册", "--recursive"])
    search = parser.parse_args(["folders", "--search", "旅行"])

    assert recursive.path == "/相册"
    assert recursive.recursive is True
    assert search.search == "旅行"


def test_files_parser_defaults_to_bounded_page_and_supports_search() -> None:
    """files 默认只读取 100 条，并允许显式指定分页或全量检索。"""
    parser = cli.build_parser()

    default_page = parser.parse_args(["files", "/视频"])
    search_all = parser.parse_args(
        ["files", "--search", "校验", "--offset", "100", "--limit", "200", "--all"]
    )

    assert default_page.path == "/视频"
    assert default_page.offset == 0
    assert default_page.limit == 100
    assert default_page.all is False
    assert search_all.search == "校验"
    assert search_all.offset == 100
    assert search_all.limit == 200
    assert search_all.all is True


def test_extended_command_parsers_expose_safe_workflow_options() -> None:
    """扩展命令应提供强校验、本地清理、冲突策略和显式回收站确认。"""
    parser = cli.build_parser()

    upload = parser.parse_args(
        [
            "upload",
            "file.bin",
            "--cid",
            "9",
            "--on-conflict",
            "rename",
            "--delete-source-after-verify",
            "--retry",
            "2",
        ]
    )
    sync = parser.parse_args(["sync", "folder", "--remote-dir", "/备份"])
    trash = parser.parse_args(["trash", "123", "--parent-cid", "9", "--yes"])

    assert upload.on_conflict == "rename"
    assert upload.delete_source_after_verify is True
    assert upload.retry == 2
    assert sync.on_conflict == "verify"
    assert trash.yes is True


def test_print_remote_file_page_escapes_names_and_deduplicates(
    capsys,
) -> None:
    """文件表格应转义控制字符，并避免跨页重复输出同一文件。"""
    page = RemoteFilePage(
        files=(
            RemoteFile(
                file_id=1,
                parent_cid=0,
                name="报告\t最终\n版.txt",
                size=12,
                sha1="ABC",
            ),
        ),
        offset=0,
        limit=100,
        total=1,
        next_offset=None,
    )
    seen: set[int] = set()

    assert cli.print_remote_file_page(page, seen=seen) == 1
    assert cli.print_remote_file_page(page, seen=seen) == 0

    output = capsys.readouterr().out
    assert output == "1\t0\t12\tABC\t报告\\t最终\\n版.txt\n"
