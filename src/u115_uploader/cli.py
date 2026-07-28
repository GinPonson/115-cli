"""u115 命令行入口。"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path
from typing import Sequence

from .uploader import OpenUploader, RemoteFolder, upload_file
from .auth import OpenAuthClient, TokenStore

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


def expand_upload_sources(sources: Sequence[Path]) -> list[Path]:
    """把文件、目录和通配符统一展开为去重后的普通文件列表。

    Args:
        sources: CLI 接收的本地路径或通配符表达式；已存在路径优先按字面处理。

    Returns:
        保持参数顺序、目录内稳定排序且去重后的绝对文件路径列表。

    Raises:
        FileNotFoundError: 普通路径不存在，或通配符没有匹配任何路径。
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
                raise FileNotFoundError(f"通配符未匹配任何路径：{source}")

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
) -> list[tuple[RemoteFolder, str]]:
    """列出指定远端目录，并按需递归生成每个文件夹的完整路径。

    Args:
        uploader: 已认证的 115 上传/文件目录客户端。
        parent_cid: 起始父目录 CID。
        parent_path: 起始父目录的绝对显示路径。
        recursive: 是否递归遍历全部后代目录。

    Returns:
        ``(文件夹, 绝对路径)`` 元组列表，顺序与 115 的名称排序一致。

    Raises:
        RuntimeError: 115 返回重复 CID，形成无法安全遍历的目录环。
        OSError: 目录请求失败。
    """
    rows: list[tuple[RemoteFolder, str]] = []
    visited = {parent_cid}

    def visit(current_cid: int, current_path: str) -> None:
        """深度优先访问当前目录，并阻止异常 CID 环导致无限请求。"""
        for folder in uploader.list_child_folders(current_cid):
            if folder.cid in visited:
                raise RuntimeError(f"115 目录结构包含重复 CID：{folder.cid}")
            visited.add(folder.cid)
            folder_path = (
                f"/{folder.name}"
                if current_path == "/"
                else f"{current_path.rstrip('/')}/{folder.name}"
            )
            rows.append((folder, folder_path))
            if recursive:
                visit(folder.cid, folder_path)

    visit(parent_cid, parent_path)
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
    upload_parser = subparsers.add_parser("upload", help="上传一个或多个本地文件")
    upload_parser.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="要上传的文件、文件夹或通配符；文件夹将递归上传",
    )
    destination = upload_parser.add_mutually_exclusive_group()
    destination.add_argument(
        "--cid",
        type=int,
        help="115 目标目录 CID",
    )
    destination.add_argument(
        "--remote-dir",
        metavar="PATH",
        help="115 目标目录绝对路径，例如 /备份/照片；默认根目录",
    )
    upload_parser.add_argument(
        "--part-size",
        type=positive_mebibytes,
        default=32 * 1024 * 1024,
        metavar="MiB",
        help="OSS 分片大小，默认 32 MiB",
    )
    folders_parser = subparsers.add_parser(
        "folders",
        help="列出或搜索 115 文件夹",
    )
    folders_parser.add_argument(
        "path",
        nargs="?",
        default="/",
        help="要列出的 115 绝对目录路径，默认根目录",
    )
    folders_parser.add_argument(
        "--cid",
        type=int,
        help="直接指定要列出的父目录 CID",
    )
    folders_parser.add_argument(
        "--recursive",
        action="store_true",
        help="递归列出全部后代文件夹",
    )
    folders_parser.add_argument(
        "--search",
        metavar="KEYWORD",
        help="按名称在整个 115 账号中搜索文件夹",
    )
    for command_parser in (login_parser, upload_parser, folders_parser):
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

        if args.command == "folders":
            if args.cid is not None and args.path != "/":
                raise ValueError("文件夹路径与 --cid 不能同时指定")
            if args.cid is not None and args.cid < 0:
                raise ValueError("父目录 CID 不能为负数")
            if args.search is not None and (args.cid is not None or args.path != "/"):
                raise ValueError("--search 不能与文件夹路径或 --cid 同时使用")
            if args.search is not None and args.recursive:
                raise ValueError("--search 已检索整个账号，不能再指定 --recursive")

            tokens = load_tokens(token_path)
            uploader = OpenUploader(tokens.access_token)
            if args.search is not None:
                folders = uploader.search_folders(args.search)
                print("CID\tPARENT_CID\tNAME")
                for folder in folders:
                    print(
                        f"{folder.cid}\t{folder.parent_cid}\t"
                        f"{_terminal_field(folder.name)}"
                    )
            else:
                parent_cid = (
                    args.cid
                    if args.cid is not None
                    else uploader.resolve_remote_dir(args.path)
                )
                parent_path = args.path if args.cid is None else f"cid:{parent_cid}"
                rows = collect_remote_folders(
                    uploader,
                    parent_cid=parent_cid,
                    parent_path=parent_path,
                    recursive=args.recursive,
                )
                print("CID\tPARENT_CID\tPATH")
                for folder, folder_path in rows:
                    print(
                        f"{folder.cid}\t{folder.parent_cid}\t"
                        f"{_terminal_field(folder_path)}"
                    )
            return 0

        # 登录态不存在时主动生成二维码；登录成功后 OAuth tokens 会写回文件。
        # 已有 access token 临近过期时，认证模块会先刷新并原子保存新令牌。
        sources = expand_upload_sources(args.files)
        tokens = load_tokens(token_path)
        for index, source in enumerate(sources, start=1):
            if len(sources) > 1:
                print(f"[{index}/{len(sources)}] 准备上传：{source}")
            result = upload_file(
                tokens.access_token,
                source,
                cid=args.cid,
                remote_dir=args.remote_dir,
                part_size=args.part_size,
                # print 默认换行；进度模块自身使用 \r，保持无额外第三方 UI 依赖。
                progress_output=lambda text: print(text, end="", flush=True),
            )
            print()
            if result.instant:
                print(f"上传完成（秒传）：{source}")
            else:
                print(f"上传完成：{source}")
        return 0
    except KeyboardInterrupt:
        print("\n操作已取消；已完成的 OSS 分片可供本次上传对象续传。", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError) as error:
        # 不输出响应 JSON、令牌、cookies 或 OSS 签名信息，只展示可读错误。
        print(f"\n操作失败：{error}", file=sys.stderr)
        return 1


def main() -> None:
    """控制台脚本入口。"""
    raise SystemExit(run())


if __name__ == "__main__":
    main()
