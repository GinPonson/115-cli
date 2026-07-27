"""u115 命令行入口。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from .uploader import upload_file
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
    upload_parser.add_argument("files", nargs="+", type=Path, help="要上传的本地文件")
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
    for command_parser in (login_parser, upload_parser):
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

        # 登录态不存在时主动生成二维码；登录成功后 OAuth tokens 会写回文件。
        # 已有 access token 临近过期时，认证模块会先刷新并原子保存新令牌。
        tokens = load_tokens(token_path)
        for index, source in enumerate(args.files, start=1):
            if len(args.files) > 1:
                print(f"[{index}/{len(args.files)}] 准备上传：{source}")
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
        print(f"\n上传失败：{error}", file=sys.stderr)
        return 1


def main() -> None:
    """控制台脚本入口。"""
    raise SystemExit(run())


if __name__ == "__main__":
    main()
