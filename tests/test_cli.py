"""CLI 客户端装配测试。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from u115_uploader import cli
from u115_uploader.uploader import RemoteFile, RemoteFilePage, RemoteFolder, UploadError
from u115_uploader.workflow import VerifiedUpload


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


def test_expand_upload_sources_records_and_skips_unmatched_glob(
    tmp_path: Path,
) -> None:
    """通配符无匹配项时应记录并跳过，不影响其他已匹配输入。"""
    pattern = Path(f"{tmp_path}/**/*.missing")
    existing = tmp_path / "existing.dat"
    existing.write_bytes(b"data")
    unmatched: list[Path] = []

    result = cli.expand_upload_sources(
        [pattern, existing],
        unmatched_globs=unmatched,
    )

    assert result == [existing.resolve()]
    assert unmatched == [pattern]


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


def test_list_parser_supports_types_recursive_search_and_duplicates() -> None:
    """list 子命令应统一提供类型、递归、搜索和重复文件参数。"""
    parser = cli.build_parser()

    recursive = parser.parse_args(
        ["list", "/相册", "--type", "file", "--recursive"]
    )
    search = parser.parse_args(
        ["list", "--search", "旅行", "--type", "all"]
    )
    duplicates = parser.parse_args(["list", "/视频", "--duplicates"])

    assert recursive.path == "/相册"
    assert recursive.recursive is True
    assert recursive.type == "file"
    assert search.search == "旅行"
    assert duplicates.duplicates is True


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
    default_upload = parser.parse_args(["upload", "folder", "--remote-dir", "/备份"])
    delete = parser.parse_args(["delete", "123", "--parent-cid", "9", "--yes"])

    assert upload.on_conflict == "rename"
    assert upload.delete_source_after_verify is True
    assert upload.retry == 2
    assert default_upload.on_conflict == "verify"
    assert delete.yes is True


def test_list_recursively_reports_only_duplicate_files(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """list 递归重复模式应跨子目录按 SHA1 分组，且不输出文件夹行。"""
    tokens = SimpleNamespace(access_token="access")

    class FakeUploader:
        """返回一个子目录及跨目录重复文件的客户端替身。"""

        def __init__(self, access_token: str) -> None:
            assert access_token == "access"

        def list_child_folders(
            self,
            parent_cid: int,
            *,
            max_results: int | None = None,
        ) -> list[RemoteFolder]:
            """返回固定的单层目录树。"""
            folders = {
                9: [RemoteFolder(cid=10, parent_cid=9, name="子目录")],
                10: [],
            }[parent_cid]
            return folders if max_results is None else folders[:max_results]

        def list_files_page(
            self,
            parent_cid: int,
            *,
            offset: int,
            limit: int,
        ) -> RemoteFilePage:
            """在父子目录分别返回相同 SHA1 的单页文件。"""
            files = {
                9: [RemoteFile(1, 9, "a.bin", 3, "SAME")],
                10: [RemoteFile(2, 10, "b.bin", 3, "SAME")],
            }[parent_cid]
            return RemoteFilePage(
                files=tuple(files),
                offset=offset,
                limit=limit,
                total=len(files),
                next_offset=None,
            )

    monkeypatch.setattr(cli, "load_tokens", lambda _path: tokens)
    monkeypatch.setattr(cli, "OpenUploader", FakeUploader)

    exit_code = cli.run(
        [
            "list",
            "--cid",
            "9",
            "--recursive",
            "--duplicates",
            "--all",
            "--tokens",
            str(tmp_path / "tokens.json"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "folder\t" not in captured.out
    assert "file\t1\t9\t3\tSAME\ta.bin" in captured.out
    assert "file\t2\t10\t3\tSAME\tb.bin" in captured.out
    assert "发现 1 组重复文件" in captured.err


def test_list_defaults_to_one_bounded_page(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """普通 list 默认最多请求并输出 100 条，不应继续读取下一页。"""
    tokens = SimpleNamespace(access_token="access")
    requested_limits: list[int] = []

    class FakeUploader:
        """记录文件分页请求且声明远端仍有后续数据。"""

        def __init__(self, access_token: str) -> None:
            assert access_token == "access"

        def list_files_page(
            self,
            parent_cid: int,
            *,
            offset: int,
            limit: int,
        ) -> RemoteFilePage:
            """返回恰好填满默认预算的一页文件。"""
            assert parent_cid == 9
            requested_limits.append(limit)
            files = tuple(
                RemoteFile(index, 9, f"file-{index}.dat", 1, f"SHA{index}")
                for index in range(1, 101)
            )
            return RemoteFilePage(files, offset, limit, 200, 100)

    monkeypatch.setattr(cli, "load_tokens", lambda _path: tokens)
    monkeypatch.setattr(cli, "OpenUploader", FakeUploader)

    exit_code = cli.run(
        [
            "list",
            "--cid",
            "9",
            "--type",
            "file",
            "--tokens",
            str(tmp_path / "tokens.json"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert requested_limits == [100]
    assert captured.out.count("\n") == 101
    assert "最多输出 100 条" in captured.err


def test_upload_returns_no_work_when_every_glob_is_unmatched(
    tmp_path: Path,
    capsys,
) -> None:
    """全部通配符均未匹配时不应登录或报失败，并返回无工作退出码 3。"""
    exit_code = cli.run(
        [
            "upload",
            str(tmp_path / "*.missing"),
            "--remote-dir",
            "/remote-target",
            "--tokens",
            str(tmp_path / "tokens.json"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "已跳过未匹配通配符" in captured.err
    assert "未执行任何操作" in captured.err
    assert "操作失败" not in captured.err


def test_upload_continues_after_single_file_failure_and_returns_nonzero(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """单文件失败后应继续处理后续文件，并以非零状态汇总批次结果。"""
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    manifest = tmp_path / "manifest.jsonl"
    tokens = SimpleNamespace(access_token="access")
    processed: list[Path] = []

    class FakeUploader:
        """只负责解析测试目标目录的上传器替身。"""

        def __init__(self, access_token: str) -> None:
            assert access_token == "access"

        def resolve_remote_dir(self, remote_dir: str) -> int:
            """返回固定测试 CID。"""
            assert remote_dir == "/target"
            return 9

    def fake_upload_and_verify(_uploader, source: Path, **_kwargs):
        """首文件失败，次文件返回已校验结果。"""
        processed.append(source)
        if source == first.resolve():
            raise UploadError("模拟单文件失败")
        remote = RemoteFile(2, 9, source.name, source.stat().st_size, "SHA1")
        return VerifiedUpload(source, remote, True, False)

    monkeypatch.setattr(cli, "load_tokens", lambda _path: tokens)
    monkeypatch.setattr(cli, "OpenUploader", FakeUploader)
    monkeypatch.setattr(cli, "upload_and_verify", fake_upload_and_verify)

    exit_code = cli.run([
        "upload",
        str(first),
        str(second),
        "--remote-dir",
        "/target",
        "--manifest",
        str(manifest),
        "--tokens",
        str(tmp_path / "tokens.json"),
    ])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert processed == [first.resolve(), second.resolve()]
    assert "文件处理失败" in captured.err
    content = manifest.read_text(encoding="utf-8")
    assert '"action": "failed"' in content
    assert '"action": "uploaded"' in content
