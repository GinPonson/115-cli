"""u115 命令行入口。"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path
from typing import Sequence

from .auth import OpenAuthClient, TokenStore
from .uploader import OpenUploader, RemoteFolder, format_safe_error
from .workflow import (
    VerifiedUpload,
    append_failure_manifest,
    append_manifest,
    finalize_local_source,
    find_verified_remote,
    upload_and_verify,
)

DEFAULT_TOKEN_PATH = Path.home() / ".config" / "u115-uploader" / "tokens.json"


def load_tokens(token_path: Path):
    """通过本项目的 OAuth 流程取得有效令牌。

    Args:
        token_path: OAuth 令牌持久化文件。

    Returns:
        有效的 ``OAuthTokens``。

    Raises:
        认证相关异常: 扫码被拒绝、二维码过期或网络请求失败。
    """
    return OpenAuthClient().ensure_tokens(TokenStore(token_path))


def positive_mebibytes(value: str) -> int:
    """把正整数 MiB 参数转换成字节。

    Args:
        value: argparse 收到的字符串。

    Returns:
        对应字节数。

    Raises:
        argparse.ArgumentTypeError: 输入不是正整数。
    """
    try:
        size = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是正整数") from error
    if size <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return size * 1024 * 1024


def nonnegative_integer(value: str) -> int:
    """解析用于 CID 和 offset 的非负整数参数。

    Args:
        value: argparse 收到的字符串。

    Returns:
        大于等于零的整数。

    Raises:
        argparse.ArgumentTypeError: 输入不是非负整数。
    """
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("必须是整数") from error
    if number < 0:
        raise argparse.ArgumentTypeError("不能为负数")
    return number


def positive_integer(value: str) -> int:
    """解析严格大于零的整数参数。

    Args:
        value: argparse 收到的字符串。

    Returns:
        严格大于零的整数。

    Raises:
        argparse.ArgumentTypeError: 输入不是正整数。
    """
    number = nonnegative_integer(value)
    if number == 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def _files_in_directory(directory: Path) -> list[Path]:
    """递归列出目录中的全部普通文件。

    Args:
        directory: 已确认存在的本地目录。

    Returns:
        按路径稳定排序的绝对文件路径列表。

    Raises:
        ValueError: 目录中没有可上传的普通文件。
        OSError: 遍历目录时发生权限或文件系统错误。
    """
    files = sorted(
        (path.resolve() for path in directory.rglob("*") if path.is_file()),
        key=os.fspath,
    )
    if not files:
        raise ValueError(f"指定文件夹中没有可上传的文件：{directory}")
    return files


def expand_upload_sources(
    sources: Sequence[Path],
    *,
    unmatched_globs: list[Path] | None = None,
) -> list[Path]:
    """把文件、目录和通配符统一展开为去重后的普通文件列表。

    Args:
        sources: CLI 接收的本地路径或通配符表达式；已存在路径优先按字面处理。
        unmatched_globs: 可选输出列表；未匹配的通配符会追加到该列表并跳过。

    Returns:
        保持参数顺序、目录内稳定排序且去重后的绝对文件路径列表。

    Raises:
        FileNotFoundError: 不含通配符的普通路径不存在。
        ValueError: 输入不是普通文件/目录，或目录中没有普通文件。
        OSError: 读取路径或遍历目录失败。
    """
    expanded: list[Path] = []
    seen: set[Path] = set()

    for source in sources:
        literal = source.expanduser()
        if literal.exists():
            matches = [literal]
        else:
            pattern = os.path.expanduser(os.fspath(source))
            if not glob.has_magic(pattern):
                raise FileNotFoundError(f"指定路径不存在：{literal.resolve()}")
            # include_hidden=True 让显式目录上传与 ** 通配符对隐藏文件的行为保持一致。
            matches = [
                Path(match)
                for match in glob.glob(
                    pattern,
                    recursive=True,
                    include_hidden=True,
                )
            ]
            if not matches:
                if unmatched_globs is not None:
                    unmatched_globs.append(source)
                continue

        for match in matches:
            resolved = match.resolve()
            if resolved.is_dir():
                candidates = _files_in_directory(resolved)
            elif resolved.is_file():
                candidates = [resolved]
            else:
                raise ValueError(f"指定路径不是普通文件或文件夹：{resolved}")

            for candidate in candidates:
                # 同一文件可能同时被目录和通配符命中；只上传一次以避免副作用。
                if candidate not in seen:
                    seen.add(candidate)
                    expanded.append(candidate)

    return expanded


def collect_remote_folders(
    uploader: OpenUploader,
    *,
    parent_cid: int,
    parent_path: str,
    recursive: bool,
    max_depth: int | None = None,
    max_folders: int | None = None,
) -> list[tuple[RemoteFolder, str]]:
    """列出指定远端目录，并按需递归生成每个文件夹的完整路径。

    Args:
        uploader: 已认证的 115 上传/文件目录客户端。
        parent_cid: 起始父目录 CID。
        parent_path: 起始父目录的绝对显示路径。
        recursive: 是否递归遍历全部后代目录。
        max_depth: 相对起始目录的最大递归深度；``None`` 表示不限制。
        max_folders: 最大目录扫描数；超过时明确失败，``None`` 表示不限制。

    Returns:
        ``(文件夹, 绝对路径)`` 元组列表，顺序与 115 的名称排序一致。

    Raises:
        RuntimeError: 115 返回重复 CID，形成无法安全遍历的目录环。
        OSError: 目录请求失败。
    """
    rows: list[tuple[RemoteFolder, str]] = []
    visited = {parent_cid}

    def visit(current_cid: int, current_path: str, depth: int) -> None:
        """深度优先访问当前目录，并阻止异常 CID 环导致无限请求。"""
        remaining = (
            None
            if max_folders is None
            else max(max_folders - len(rows), 0)
        )
        child_folders = (
            uploader.list_child_folders(current_cid)
            if max_folders is None
            else uploader.list_child_folders(
                current_cid,
                max_results=remaining,
            )
        )
        for folder in child_folders:
            if max_folders is not None and len(rows) >= max_folders:
                raise RuntimeError(
                    f"目录扫描超过安全上限 {max_folders}；"
                    "请提高 --max-directories 后重试"
                )
            if folder.cid in visited:
                raise RuntimeError(f"115 目录结构包含重复 CID：{folder.cid}")
            visited.add(folder.cid)
            folder_path = (
                f"/{folder.name}"
                if current_path == "/"
                else f"{current_path.rstrip('/')}/{folder.name}"
            )
            rows.append((folder, folder_path))
            if recursive and (max_depth is None or depth < max_depth):
                visit(folder.cid, folder_path, depth + 1)

    visit(parent_cid, parent_path, 1)
    return rows


def _terminal_field(value: object) -> str:
    """转义终端表格中的控制字符，保证每个文件夹固定占一行。

    Args:
        value: 要输出的 CID、名称或路径。

    Returns:
        已转义制表符、回车和换行的文本。
    """
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def _add_destination_arguments(parser: argparse.ArgumentParser) -> None:
    """为需要目标目录的命令添加互斥路径/CID 参数。

    Args:
        parser: 要扩展的子命令解析器。
    """
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--cid", type=nonnegative_integer, help="115 目标目录 CID")
    destination.add_argument(
        "--remote-dir",
        metavar="PATH",
        help="115 目标目录绝对路径，例如 /备份/照片；默认根目录",
    )


def _add_upload_options(parser: argparse.ArgumentParser) -> None:
    """为 upload 添加强校验、冲突与本地清理选项。

    Args:
        parser: upload 子命令解析器。
    """
    parser.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="要上传的文件、文件夹或通配符；文件夹将递归上传",
    )
    _add_destination_arguments(parser)
    parser.add_argument(
        "--part-size",
        type=positive_mebibytes,
        default=32 * 1024 * 1024,
        metavar="MiB",
        help="OSS 分片大小，默认 32 MiB",
    )
    parser.add_argument(
        "--on-conflict",
        choices=("error", "skip", "verify", "rename"),
        default="verify",
        help="远端同名文件策略，默认 verify",
    )
    parser.add_argument(
        "--create-remote-dir",
        action="store_true",
        help="逐级创建 --remote-dir 中缺失的文件夹",
    )
    parser.add_argument(
        "--retry",
        type=nonnegative_integer,
        default=0,
        metavar="N",
        help="仅对网络传输错误额外重试 N 次",
    )
    local_action = parser.add_mutually_exclusive_group()
    local_action.add_argument(
        "--delete-source-after-verify",
        action="store_true",
        help="远端大小和 SHA1 强校验成功后删除本地源文件",
    )
    local_action.add_argument(
        "--move-source-after-verify",
        type=Path,
        metavar="DIR",
        help="远端强校验成功后把本地源文件移入 DIR",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        metavar="JSONL",
        help="把每个成功结果追加到 JSON Lines 审计文件",
    )


def build_parser() -> argparse.ArgumentParser:
    """创建 CLI 参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="u115",
        description="115 开放平台二维码登录与文件上传工具。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    login_parser = subparsers.add_parser("login", help="生成二维码并登录 115")
    login_parser.add_argument(
        "--force",
        action="store_true",
        help="忽略已有登录态，强制重新扫码",
    )
    login_parser.add_argument(
        "--qr-output",
        type=Path,
        metavar="PNG",
        help="将认证二维码同时导出为 PNG 图片",
    )
    upload_parser = subparsers.add_parser("upload", help="上传或校验并跳过已存在文件")
    _add_upload_options(upload_parser)
    upload_parser.set_defaults(on_conflict="verify")
    verify_parser = subparsers.add_parser("verify", help="强校验本地文件是否已存在于 115")
    verify_parser.add_argument("files", nargs="+", type=Path, help="要校验的本地文件或通配符")
    _add_destination_arguments(verify_parser)
    verify_parser.add_argument("--manifest", type=Path, metavar="JSONL", help="追加校验结果")
    list_parser = subparsers.add_parser(
        "list",
        help="统一列出、搜索或检查重复的 115 文件与文件夹",
    )
    list_parser.add_argument("path", nargs="?", default="/", help="115 目录路径")
    list_parser.add_argument("--cid", type=nonnegative_integer, help="115 目录 CID")
    list_parser.add_argument(
        "--type",
        choices=("all", "file", "folder"),
        default="all",
        help="条目类型，默认 all",
    )
    list_parser.add_argument("--recursive", action="store_true", help="递归列出全部后代")
    list_parser.add_argument("--search", metavar="KEYWORD", help="在整个账号中按名称搜索")
    list_parser.add_argument(
        "--limit",
        type=positive_integer,
        default=100,
        help="最多输出的条目数，默认 100",
    )
    list_parser.add_argument(
        "--all",
        action="store_true",
        help="显式允许读取并输出当前范围内的全部条目",
    )
    list_parser.add_argument(
        "--max-depth",
        type=positive_integer,
        default=20,
        help="递归目录最大深度，默认 20",
    )
    list_parser.add_argument(
        "--max-directories",
        type=positive_integer,
        default=1000,
        help="最多扫描的目录数，默认 1000",
    )
    list_parser.add_argument(
        "--duplicates",
        action="store_true",
        help="只输出当前范围内 SHA1 重复的文件",
    )
    delete_parser = subparsers.add_parser("delete", help="按精确文件 ID 移入 115 回收站")
    delete_parser.add_argument("file_ids", nargs="+", type=nonnegative_integer, help="文件 ID")
    delete_parser.add_argument(
        "--parent-cid",
        required=True,
        type=nonnegative_integer,
        help="这些文件当前所在的父目录 CID",
    )
    delete_parser.add_argument(
        "--yes",
        action="store_true",
        help="确认执行回收站操作；缺少时拒绝执行",
    )
    for command_parser in (
        login_parser,
        upload_parser,
        verify_parser,
        list_parser,
        delete_parser,
    ):
        command_parser.add_argument(
            "--tokens",
            type=Path,
            default=Path(os.environ.get("U115_TOKEN_PATH", DEFAULT_TOKEN_PATH)),
            help="OAuth 登录态文件，可由 U115_TOKEN_PATH 覆盖",
        )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    """执行 CLI。

    Args:
        argv: 参数列表；为 ``None`` 时读取 ``sys.argv``。

    Returns:
        进程退出码：成功为 0，失败为 1 或 argparse 的 2。
    """
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    # 兼容用户习惯的简写：
    # u115 upload file1 file2 /远端目录
    # 最后一个参数必须是不存在的绝对本地路径，才会被解释为 115 路径，避免误判本地文件。
    if (
        raw_argv
        and raw_argv[0] == "upload"
        and "--remote-dir" not in raw_argv
        and "--cid" not in raw_argv
        and len(raw_argv) >= 3
    ):
        positional_remote = Path(raw_argv[-1]).expanduser()
        if raw_argv[-1].startswith("/") and not positional_remote.exists():
            raw_argv = [*raw_argv[:-1], "--remote-dir", raw_argv[-1]]
    args = build_parser().parse_args(raw_argv)
    token_path: Path = args.tokens.expanduser().resolve()
    token_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if args.command == "login":
            store = TokenStore(token_path)
            auth = OpenAuthClient(qr_output=args.qr_output)
            if args.force:
                tokens = auth.login()
                store.save(tokens)
            else:
                tokens = auth.ensure_tokens(store)
            identity = tokens.user_name or str(tokens.user_id) or "当前账号"
            print(f"登录成功：{identity}")
            print(f"登录态已保存：{token_path}")
            return 0

        if args.command == "list":
            if args.cid is not None and args.path != "/":
                raise ValueError("目录路径与 --cid 不能同时指定")
            if args.search is not None and (args.cid is not None or args.path != "/"):
                raise ValueError("--search 不能与目录路径或 --cid 同时使用")
            if args.search is not None and args.recursive:
                raise ValueError("--search 已检索整个账号，不能再指定 --recursive")
            if args.duplicates and args.type == "folder":
                raise ValueError("--duplicates 只适用于文件")
            if args.duplicates and not args.all:
                raise ValueError("--duplicates 需要完整扫描，必须显式指定 --all")
            tokens = load_tokens(token_path)
            uploader = OpenUploader(tokens.access_token)

            folders: list[tuple[RemoteFolder, str]] = []
            remote_files = []
            output_limit: int | None = None if args.all else args.limit

            def remaining_capacity() -> int | None:
                """返回普通列表还能接收的文件数；全量或重复模式不设上限。"""
                if output_limit is None or args.duplicates:
                    return None
                return max(output_limit - len(folders) - len(remote_files), 0)

            def append_file_pages(parent_cid: int | None = None) -> bool:
                """分页追加文件，达到输出预算时立即停止继续请求。

                Args:
                    parent_cid: 普通目录 CID；为 ``None`` 时执行全局搜索。

                Returns:
                    当前输出预算是否已经耗尽。
                """
                offset = 0
                while True:
                    remaining = remaining_capacity()
                    if remaining == 0:
                        return True
                    page_limit = 1150 if remaining is None else min(remaining, 1150)
                    page = (
                        uploader.search_files_page(
                            args.search,
                            offset=offset,
                            limit=page_limit,
                        )
                        if parent_cid is None
                        else uploader.list_files_page(
                            parent_cid,
                            offset=offset,
                            limit=page_limit,
                        )
                    )
                    remote_files.extend(page.files)
                    if page.next_offset is None:
                        return False
                    if page.next_offset <= offset:
                        raise RuntimeError("115 文件列表分页偏移未前进")
                    offset = page.next_offset

            if args.search is not None:
                if args.type in {"all", "folder"} and not args.duplicates:
                    folder_limit = (
                        args.max_directories
                        if output_limit is None
                        else remaining_capacity()
                    )
                    folders = [
                        (folder, folder.name)
                        for folder in uploader.search_folders(
                            args.search,
                            max_results=folder_limit,
                        )
                    ]
                if args.type in {"all", "file"}:
                    append_file_pages()
            else:
                parent_cid = (
                    args.cid
                    if args.cid is not None
                    else uploader.resolve_remote_dir(args.path)
                )
                parent_path = args.path if args.cid is None else f"cid:{parent_cid}"
                if args.type in {"all", "folder"} and not args.duplicates:
                    folders = collect_remote_folders(
                        uploader,
                        parent_cid=parent_cid,
                        parent_path=parent_path,
                        recursive=args.recursive,
                        max_depth=args.max_depth,
                        max_folders=(
                            args.max_directories
                            if output_limit is None
                            else min(args.max_directories, output_limit)
                        ),
                    )
                    if output_limit is not None:
                        folders = folders[:output_limit]
                file_cids = [parent_cid]
                if args.recursive and args.type in {"all", "file"}:
                    # 文件递归需要完整目录树；当 --type=file 时前面没有收集目录。
                    file_folders = (
                        folders
                        if folders
                        else collect_remote_folders(
                            uploader,
                            parent_cid=parent_cid,
                            parent_path=parent_path,
                            recursive=True,
                            max_depth=args.max_depth,
                            max_folders=args.max_directories,
                        )
                    )
                    file_cids.extend(folder.cid for folder, _path in file_folders)
                if args.type in {"all", "file"}:
                    for file_cid in file_cids:
                        if append_file_pages(file_cid):
                            break

            if args.duplicates:
                groups: dict[str, list] = {}
                for remote_file in remote_files:
                    if remote_file.sha1:
                        groups.setdefault(remote_file.sha1, []).append(remote_file)
                remote_files = [
                    remote_file
                    for group in groups.values()
                    if len(group) > 1
                    for remote_file in group
                ]

            print("TYPE\tID\tPARENT_ID\tSIZE\tSHA1\tNAME")
            for folder, display_path in folders:
                if not args.duplicates:
                    print(
                        f"folder\t{folder.cid}\t{folder.parent_cid}\t\t\t"
                        f"{_terminal_field(display_path)}"
                    )
            for remote_file in remote_files:
                print(
                    f"file\t{remote_file.file_id}\t{remote_file.parent_cid}\t"
                    f"{remote_file.size}\t{remote_file.sha1}\t"
                    f"{_terminal_field(remote_file.name)}"
                )
            if args.duplicates:
                duplicate_sha1s = {
                    remote_file.sha1 for remote_file in remote_files
                }
                print(f"发现 {len(duplicate_sha1s)} 组重复文件", file=sys.stderr)
            else:
                limit_message = (
                    ""
                    if args.all
                    else f"；最多输出 {args.limit} 条，使用 --all 读取全部"
                )
                print(
                    f"已显示 {len(folders)} 个文件夹、{len(remote_files)} 个文件"
                    f"{limit_message}",
                    file=sys.stderr,
                )
            return 0

        if args.command == "delete":
            if not args.yes:
                raise ValueError("回收站操作必须显式指定 --yes")
            if any(file_id <= 0 for file_id in args.file_ids):
                raise ValueError("文件 ID 必须为正整数")
            tokens = load_tokens(token_path)
            uploader = OpenUploader(tokens.access_token)
            for file_id in args.file_ids:
                uploader.trash_file(file_id, args.parent_cid)
                print(f"已移入 115 回收站：{file_id}")
            return 0

        if args.command == "verify":
            unmatched_globs: list[Path] = []
            sources = expand_upload_sources(
                args.files,
                unmatched_globs=unmatched_globs,
            )
            for pattern in unmatched_globs:
                print(f"已跳过未匹配通配符：{pattern}", file=sys.stderr)
            if not sources:
                print("没有匹配到可校验的文件，未执行任何操作。", file=sys.stderr)
                return 3
            tokens = load_tokens(token_path)
            uploader = OpenUploader(tokens.access_token)
            parent_cid = (
                args.cid
                if args.cid is not None
                else uploader.resolve_remote_dir(args.remote_dir or "/")
            )
            for source in sources:
                remote = find_verified_remote(
                    uploader,
                    source,
                    parent_cid=parent_cid,
                )
                print(
                    f"校验通过：{source} -> "
                    f"file_id={remote.file_id}, sha1={remote.sha1}"
                )
                if args.manifest is not None:
                    append_manifest(
                        args.manifest,
                        VerifiedUpload(source, remote, False, False),
                        action="verified",
                    )
            return 0

        # 登录态不存在时主动生成二维码；登录成功后 OAuth tokens 会写回文件。
        # 已有 access token 临近过期时，认证模块会先刷新并原子保存新令牌。
        unmatched_globs = []
        sources = expand_upload_sources(
            args.files,
            unmatched_globs=unmatched_globs,
        )
        for pattern in unmatched_globs:
            print(f"已跳过未匹配通配符：{pattern}", file=sys.stderr)
        if not sources:
            print("没有匹配到可上传的文件，未执行任何操作。", file=sys.stderr)
            return 3
        if args.on_conflict == "skip" and (
            args.delete_source_after_verify
            or args.move_source_after_verify is not None
        ):
            raise ValueError("skip 未执行 SHA1 校验，不能据此删除或移动本地源文件")
        tokens = load_tokens(token_path)
        uploader = OpenUploader(tokens.access_token)
        if args.cid is not None:
            parent_cid = args.cid
        elif args.create_remote_dir:
            parent_cid = uploader.ensure_remote_dir(args.remote_dir or "/")
        else:
            parent_cid = uploader.resolve_remote_dir(args.remote_dir or "/")
        failed_count = 0
        for index, source in enumerate(sources, start=1):
            if len(sources) > 1:
                print(f"[{index}/{len(sources)}] 准备上传：{source}")
            stage = "upload"
            try:
                verified = upload_and_verify(
                    uploader,
                    source,
                    parent_cid=parent_cid,
                    part_size=args.part_size,
                    conflict=args.on_conflict,
                    retries=args.retry,
                    # print 默认换行；进度模块自身使用 \r，保持无额外第三方 UI 依赖。
                    progress_output=lambda text: print(text, end="", flush=True),
                )
                if verified.uploaded:
                    print()
                    mode = "秒传并校验" if verified.instant else "上传并校验"
                    print(f"{mode}完成：{source} -> file_id={verified.remote.file_id}")
                else:
                    print(f"远端已存在并处理：{source} -> file_id={verified.remote.file_id}")
                stage = "finalize"
                moved = finalize_local_source(
                    source,
                    delete_after_verify=args.delete_source_after_verify,
                    move_after_verify=args.move_source_after_verify,
                )
                if args.delete_source_after_verify:
                    print(f"已删除本地源文件：{source}")
                elif moved is not None:
                    print(f"已移动本地源文件：{source} -> {moved}")
                if args.manifest is not None:
                    append_manifest(
                        args.manifest,
                        verified,
                        action="uploaded" if verified.uploaded else "skipped",
                    )
            except (OSError, RuntimeError, ValueError) as error:
                failed_count += 1
                if args.manifest is not None:
                    append_failure_manifest(
                        args.manifest,
                        source,
                        error,
                        stage=stage,
                        remote_dir=args.remote_dir,
                        parent_cid=parent_cid,
                    )
                print(
                    f"\n文件处理失败：{source}：{format_safe_error(error)}",
                    file=sys.stderr,
                )
        return 1 if failed_count else 0
    except KeyboardInterrupt:
        print("\n操作已取消；已完成的 OSS 分片可供本次上传对象续传。", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        # 不输出响应 JSON、令牌、cookies 或 OSS 签名信息，只展示可读错误。
        print(f"\n操作失败：{format_safe_error(error)}", file=sys.stderr)
        return 1


def main() -> None:
    """控制台脚本入口。"""
    raise SystemExit(run())


if __name__ == "__main__":
    main()
