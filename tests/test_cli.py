"""CLI 客户端装配测试。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from u115_uploader import cli
from u115_uploader.uploader import (
    DownloadLink,
    OfflineUrlResult,
    RemoteFile,
    RemoteFilePage,
    RemoteFolder,
    TrashItem,
    TrashPage,
    UploadError,
    UserInfo,
)
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
            "--create-remote-dir",
        ]
    )
    default_upload = parser.parse_args(["upload", "folder", "--remote-dir", "/备份"])
    delete = parser.parse_args(["delete", "123", "--parent-cid", "9", "--yes"])

    assert upload.on_conflict == "rename"
    assert upload.delete_source_after_verify is True
    assert upload.retry == 2
    assert upload.create_remote_dir is True
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

        def ensure_remote_dir(self, remote_dir: str) -> int:
            """模拟自动创建并返回固定测试 CID。"""
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
        "--create-remote-dir",
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


def test_whoami_prints_account_summary(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """whoami 应输出身份、空间与会员概要。"""
    tokens = SimpleNamespace(access_token="access")

    class FakeUploader:
        """返回固定用户信息的客户端替身。"""

        def __init__(self, access_token: str) -> None:
            assert access_token == "access"

        def user_info(self) -> UserInfo:
            """返回覆盖字符串与典型数值的用户信息。"""
            return UserInfo(
                user_id=12345,
                user_name="测试用户",
                total_size=5 * 1024**4,
                remain_size=1024**4,
                use_size=4 * 1024**4,
                vip_level="年费VIP",
                vip_expire=1800000000,
            )

    monkeypatch.setattr(cli, "load_tokens", lambda _path: tokens)
    monkeypatch.setattr(cli, "OpenUploader", FakeUploader)

    exit_code = cli.run(["whoami", "--tokens", str(tmp_path / "tokens.json")])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "测试用户（ID 12345）" in captured.out
    assert "5.00 TiB" in captured.out
    assert "1.00 TiB" in captured.out
    assert "年费VIP" in captured.out


def test_mkdir_ensures_remote_dir(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """mkdir 应逐级创建目录并输出最终 CID。"""
    tokens = SimpleNamespace(access_token="access")

    class FakeUploader:
        """记录目录创建请求的客户端替身。"""

        def __init__(self, access_token: str) -> None:
            assert access_token == "access"

        def ensure_remote_dir(self, path: str) -> int:
            """校验并返回固定测试 CID。"""
            assert path == "/备份/照片"
            return 7

    monkeypatch.setattr(cli, "load_tokens", lambda _path: tokens)
    monkeypatch.setattr(cli, "OpenUploader", FakeUploader)

    exit_code = cli.run(
        ["mkdir", "/备份/照片", "--tokens", str(tmp_path / "tokens.json")]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "目录已就绪：/备份/照片（CID 7）" in captured.out


def test_move_resolves_destination_and_moves_entries(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """move 应先解析目标目录再提交移动，并输出结果。"""
    tokens = SimpleNamespace(access_token="access")
    calls: list[tuple[list[int], int]] = []

    class FakeUploader:
        """记录移动请求的客户端替身。"""

        def __init__(self, access_token: str) -> None:
            assert access_token == "access"

        def resolve_remote_dir(self, remote_dir: str) -> int:
            """校验并返回固定目标 CID。"""
            assert remote_dir == "/目标"
            return 9

        def move_entries(self, file_ids, to_cid: int) -> None:
            """记录批量移动调用。"""
            calls.append((list(file_ids), to_cid))

    monkeypatch.setattr(cli, "load_tokens", lambda _path: tokens)
    monkeypatch.setattr(cli, "OpenUploader", FakeUploader)

    exit_code = cli.run(
        ["move", "1", "2", "--remote-dir", "/目标", "--tokens", str(tmp_path / "t.json")]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert calls == [([1, 2], 9)]
    assert "已移动 1, 2 -> CID 9" in captured.out


def test_rename_rejects_invalid_name(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """包含路径分隔符的新名称应作为失败退出，并给出可读错误。"""
    tokens = SimpleNamespace(access_token="access")

    class FakeUploader:
        """模拟 SDK 对非法名称的拒绝。"""

        def __init__(self, access_token: str) -> None:
            assert access_token == "access"

        def rename_entry(self, file_id: int, new_name: str) -> None:
            """非法名称必须在这里被拒绝，不能到达 115。"""
            raise ValueError("新名称不能为空、.、..，且不能包含 / 或 NUL")

    monkeypatch.setattr(cli, "load_tokens", lambda _path: tokens)
    monkeypatch.setattr(cli, "OpenUploader", FakeUploader)

    exit_code = cli.run(["rename", "5", "a/b", "--tokens", str(tmp_path / "t.json")])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "新名称" in captured.err


def test_trash_list_prints_entries(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """trash list 应输出标准化表格列并附带条目计数。"""
    tokens = SimpleNamespace(access_token="access")

    class FakeUploader:
        """返回单条回收站记录的客户端替身。"""

        def __init__(self, access_token: str) -> None:
            assert access_token == "access"

        def list_trash_page(self, *, offset: int = 0, limit: int = 100) -> TrashPage:
            """校验分页参数并返回固定条目。"""
            assert (offset, limit) == (0, 100)
            item = TrashItem(
                trash_id="301",
                name="删除的文件.bin",
                is_folder=False,
                size=2048,
                deleted_time=1700000000,
                parent_cid=9,
                parent_name="媒体",
                pick_code="pc",
                status=0,
            )
            return TrashPage(items=(item,), offset=0, limit=limit, total=1, next_offset=None)

    monkeypatch.setattr(cli, "load_tokens", lambda _path: tokens)
    monkeypatch.setattr(cli, "OpenUploader", FakeUploader)

    exit_code = cli.run(["trash", "list", "--tokens", str(tmp_path / "t.json")])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "TRASH_ID\tTYPE\tSIZE\tDELETED_AT\tPARENT\tNAME" in captured.out
    assert "301\tfile\t2048\t" in captured.out
    assert "媒体\t删除的文件.bin" in captured.out
    assert "共 1 个回收站条目" in captured.err


def test_trash_purge_requires_explicit_yes(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """彻底删除回收站条目必须显式确认 --yes。"""
    tokens = SimpleNamespace(access_token="access")

    class FakeUploader:
        """仅校验客户端装配，不执行删除。"""

        def __init__(self, access_token: str) -> None:
            assert access_token == "access"

        def purge_trash(self, trash_ids) -> None:
            """未确认时不应到达这里。"""
            raise AssertionError("不应在缺少 --yes 时执行彻底删除")

    monkeypatch.setattr(cli, "load_tokens", lambda _path: tokens)
    monkeypatch.setattr(cli, "OpenUploader", FakeUploader)

    exit_code = cli.run(["trash", "purge", "123", "--tokens", str(tmp_path / "t.json")])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "--yes" in captured.err


def test_download_url_prints_only_the_url(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """download-url 的标准输出应只有直链本身，便于脚本取用。"""
    tokens = SimpleNamespace(access_token="access")

    class FakeUploader:
        """返回固定直链的客户端替身。"""

        def __init__(self, access_token: str) -> None:
            assert access_token == "access"

        def download_url(self, file_id: int) -> DownloadLink:
            """校验文件 ID 并返回固定直链。"""
            assert file_id == 42
            return DownloadLink(42, "a.bin", 3, "AA", "https://cdn/a.bin")

    monkeypatch.setattr(cli, "load_tokens", lambda _path: tokens)
    monkeypatch.setattr(cli, "OpenUploader", FakeUploader)

    exit_code = cli.run(["download-url", "42", "--tokens", str(tmp_path / "t.json")])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out.strip() == "https://cdn/a.bin"


def test_offline_add_reports_failures_and_returns_nonzero(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """部分链接失败时应逐条报告并以非零退出码汇总。"""
    tokens = SimpleNamespace(access_token="access")

    class FakeUploader:
        """记录目标目录并返回混合结果的客户端替身。"""

        def __init__(self, access_token: str) -> None:
            assert access_token == "access"

        def resolve_remote_dir(self, remote_dir: str) -> int:
            """校验并返回固定保存目录。"""
            assert remote_dir == "/下载"
            return 9

        def add_offline_urls(self, urls, *, wp_path_id: int):
            """校验保存目录并返回一成一败的结果。"""
            assert wp_path_id == 9
            return [
                OfflineUrlResult("magnet:a", "hash-a", True, ""),
                OfflineUrlResult("magnet:b", "", False, "链接无效"),
            ]

    monkeypatch.setattr(cli, "load_tokens", lambda _path: tokens)
    monkeypatch.setattr(cli, "OpenUploader", FakeUploader)

    exit_code = cli.run(
        [
            "offline",
            "add",
            "magnet:a",
            "magnet:b",
            "--remote-dir",
            "/下载",
            "--tokens",
            str(tmp_path / "t.json"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "hash-a" in captured.out
    assert "离线任务添加失败：magnet:b：链接无效" in captured.err


def test_offline_clear_requires_explicit_yes(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """清空离线任务必须显式确认 --yes。"""
    tokens = SimpleNamespace(access_token="access")

    class FakeUploader:
        """仅校验客户端装配，不执行清空。"""

        def __init__(self, access_token: str) -> None:
            assert access_token == "access"

        def clear_offline_tasks(self, *, flag: int = 1) -> None:
            """未确认时不应到达这里。"""
            raise AssertionError("不应在缺少 --yes 时清空离线任务")

    monkeypatch.setattr(cli, "load_tokens", lambda _path: tokens)
    monkeypatch.setattr(cli, "OpenUploader", FakeUploader)

    exit_code = cli.run(["offline", "clear", "--tokens", str(tmp_path / "t.json")])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "--yes" in captured.err


def test_offline_rm_delete_files_requires_yes(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    """删除任务并连带删源文件时必须显式确认 --yes。"""
    tokens = SimpleNamespace(access_token="access")

    class FakeUploader:
        """仅校验客户端装配，不执行删除。"""

        def __init__(self, access_token: str) -> None:
            assert access_token == "access"

        def delete_offline_task(self, info_hash: str, *, delete_files: bool = False) -> None:
            """未确认时不应到达这里。"""
            raise AssertionError("不应在缺少 --yes 时删除源文件")

    monkeypatch.setattr(cli, "load_tokens", lambda _path: tokens)
    monkeypatch.setattr(cli, "OpenUploader", FakeUploader)

    exit_code = cli.run(
        ["offline", "rm", "hash", "--delete-files", "--tokens", str(tmp_path / "t.json")]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "--yes" in captured.err
