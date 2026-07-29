# u115-uploader

一个轻量的 Python CLI，用 115 App 扫码登录后，把指定文件上传到 115 网盘。

认证和上传协议均在本项目实现：

- 内置开放平台设备码登录、终端二维码和 OAuth token 持久化
- 单次读取计算整文件 SHA1 与前 128 KiB `preid`
- 严格处理 `700/6、701/7、702/8` 二次签名状态
- 使用 115 STS 凭证执行 OSS 单 PUT 或分片上传和完成回调

## 安装

需要 Python 3.12+。推荐使用 `uv`：

```bash
cd /Users/Gin/Workspace/115uploader
uv sync
```

## 登录

首次使用先运行：

```bash
uv run u115 login
```

终端会显示一个二维码，随后：

1. 打开手机上的 **115 App**。
2. 使用 115 App 的扫一扫扫描终端二维码。
3. 在手机上点击确认登录。
4. 等待终端显示 `登录成功`。

登录成功后，OAuth 登录态默认以 `0600` 权限保存在：

```text
~/.config/u115-uploader/tokens.json
```

后续上传会自动使用该登录态，不需要重复扫码。access token 临近过期时，
程序会使用 refresh token 刷新并写回文件。

需要切换账号或强制重新扫码时：

```bash
uv run u115 login --force
```

将认证二维码同时导出为 PNG 图片：

```bash
uv run u115 login --force --qr-output ./115-login.png
```

命令仍会在终端显示二维码，同时把同一个二维码写入指定图片。输出目录不存在时会自动创建。
如果已有有效登录态且未指定 `--force`，不会重新申请二维码，因此也不会生成图片。

使用自定义登录态文件：

```bash
uv run u115 login --tokens /secure/path/115-tokens.json
```

也可以通过环境变量统一指定：

```bash
export U115_TOKEN_PATH=/secure/path/115-tokens.json
uv run u115 login
```

## 上传

上传到根目录：

```bash
uv run u115 upload /path/to/file.bin
```

上传到指定的 115 目录路径：

```bash
uv run u115 upload /path/to/file.bin --remote-dir "/备份/照片"
```

递归上传文件夹中的全部普通文件：

```bash
uv run u115 upload "/path/to/照片文件夹" --remote-dir "/备份/照片"
```

文件夹层级仅用于查找本地文件；所有文件仍上传到同一个 115 目标目录，不会自动创建远端子目录。

使用通配符上传；建议用引号阻止 Shell 提前展开，由程序统一处理 `*`、`?` 和
递归匹配 `**`：

```bash
uv run u115 upload "~/Downloads/**/*.mp4" --remote-dir "/视频"
```

同一个文件被多个参数或模式重复匹配时只会上传一次。通配符没有匹配项、文件夹为空或
输入路径不存在时，命令会明确报错。

空格、中文和通配符元字符等特殊字符文件名应使用引号。已存在的路径会优先按字面处理，
因此下例中的方括号、星号和问号不会被当作通配符：

```bash
uv run u115 upload "/path/to/报告 [最终]*?.pdf"
```

文件名以 `-` 开头时，在文件参数前使用 `--` 结束选项解析：

```bash
uv run u115 upload -- "-特殊文件.txt"
```

也支持把远端目录放在最后：

```bash
uv run u115 upload "~/Downloads/漫喫*.mp4" /starvault-batch-test
```

远端路径必须：

- 从根目录开始并以 `/` 开头。
- 每一级目录都已经存在。
- 目录名称完全匹配，包括空格和大小写。

如果某一级不存在或存在同名目录，命令会明确报错，不会擅自创建目录或上传到其他位置。

也可以直接指定目录 CID，并设置 64 MiB 分片：

```bash
uv run u115 upload /path/to/file.bin --cid 123456 --part-size 64
```

`--remote-dir` 与 `--cid` 不能同时使用。不指定时上传到根目录。

每个文件上传后都会重新读取目标目录，并同时核对远端文件名、大小和 SHA1。只有三项
完全一致才算完成。需要在强校验后删除本地源文件时，显式添加：

```bash
uv run u115 upload "/archive/*.mp4" \
  --remote-dir "/视频" \
  --delete-source-after-verify
```

更保守的做法是把已校验文件移动到本地归档目录：

```bash
uv run u115 upload "/archive/*.mp4" \
  --remote-dir "/视频" \
  --move-source-after-verify "/archive/uploaded"
```

同名文件策略由 `--on-conflict` 控制：

- `error`：默认策略；发现同名文件立即报错。
- `verify`：大小和 SHA1 一致则跳过上传，不一致则报错。
- `skip`：不校验直接跳过；不能与本地删除或移动选项一起使用。
- `rename`：使用 `文件名 (N).扩展名` 上传，并对新名称执行强校验。

仅对网络传输错误进行有限重试，并把成功结果追加到 JSON Lines manifest：

```bash
uv run u115 upload "/archive/*.mp4" \
  --remote-dir "/视频" \
  --retry 2 \
  --manifest "./upload-manifest.jsonl"
```

callback、SHA1、冲突或其他业务错误不会重试，避免掩盖协议问题。

如果没有提前执行 `login`，第一次执行 `upload` 时也会自动显示二维码并等待登录。

上传时使用自定义登录态文件：

```bash
uv run u115 upload file.bin --tokens /secure/path/115-tokens.json
```

## 同步与独立校验

`sync` 使用与 `upload` 相同的上传流程，但默认冲突策略为 `verify`：远端已存在且
大小、SHA1 一致时跳过，缺失时上传，不一致时明确失败。

```bash
uv run u115 sync "/archive/*.mp4" --remote-dir "/视频"
```

不上传，只确认本地文件是否已经完整存在于指定 115 目录：

```bash
uv run u115 verify "/archive/file.mp4" --remote-dir "/视频"
```

`verify` 同样支持 `--cid`、通配符和 `--manifest`。

## 重复文件与回收站

按 SHA1 查找指定目录中的重复文件：

```bash
uv run u115 duplicates "/视频"
uv run u115 duplicates --cid 123456
```

该命令只报告重复组，不自动删除。远端删除使用精确文件 ID，并移动到 115 回收站：

```bash
uv run u115 trash 3483568169358984581 \
  --parent-cid 3392823418375834914 \
  --yes
```

`trash` 不接受文件名或模糊搜索，必须同时提供文件当前父 CID 和 `--yes`；它不会执行
回收站永久清空。

## 文件夹列表与检索

列出 115 根目录的直接子文件夹：

```bash
uv run u115 folders
```

列出指定路径或 CID 下的直接子文件夹：

```bash
uv run u115 folders "/备份/照片"
uv run u115 folders --cid 123456
```

递归列出指定路径下的全部后代文件夹：

```bash
uv run u115 folders "/备份" --recursive
```

按名称在整个 115 账号中搜索文件夹：

```bash
uv run u115 folders --search "旅行"
```

普通列表输出 `CID`、`PARENT_CID`、`PATH` 三列；搜索输出 `CID`、`PARENT_CID`、
`NAME` 三列，列之间使用 Tab 分隔。CID 可以直接用于上传：

```bash
uv run u115 upload "/path/to/file.bin" --cid 123456
```

`--search` 已经检索整个账号，不能与路径、`--cid` 或 `--recursive` 同时使用。
文件夹命令与登录、上传命令共用 OAuth 登录态，也支持 `--tokens` 指定其他登录态文件。

## 文件列表与校验

分页列出根目录中的文件；默认只读取前 100 条，避免大目录一次性加载：

```bash
uv run u115 files
```

列出指定路径或 CID 中的文件：

```bash
uv run u115 files "/备份/照片"
uv run u115 files --cid 123456
```

指定分页偏移和单页大小：

```bash
uv run u115 files --cid 123456 --offset 100 --limit 200
```

每页结束后，终端会在错误输出中显示匹配总数；如果还有下一页，也会给出可直接使用的
`--offset` 和 `--limit` 参数。`--limit` 范围为 1–1150。

确认确实需要读取剩余全部文件时，显式添加 `--all`：

```bash
uv run u115 files --cid 123456 --limit 500 --all
```

按文件名在整个 115 账号中分页搜索：

```bash
uv run u115 files --search "年度报告" --limit 100
```

也可以从指定 offset 开始读取全部剩余搜索结果：

```bash
uv run u115 files --search ".iso" --offset 500 --limit 500 --all
```

输出为 Tab 分隔的 `FILE_ID`、`PARENT_CID`、`SIZE`、`SHA1`、`NAME` 五列，
方便使用 `awk`、重定向或其他脚本进行数量、大小与 SHA1 校验。文件名中的 Tab、回车和
换行会转义，不会破坏表格结构。

`--search` 不能与目录路径或 `--cid` 同时使用。文件列表只返回指定目录的直接文件，
不会隐式递归进入子文件夹；需要核对其他目录时，可先使用 `u115 folders --recursive`
取得对应 CID。

查看完整参数：

```bash
uv run u115 --help
uv run u115 login --help
uv run u115 upload --help
uv run u115 sync --help
uv run u115 verify --help
uv run u115 duplicates --help
uv run u115 trash --help
uv run u115 folders --help
uv run u115 files --help
```

## 测试

```bash
uv run pytest
```
