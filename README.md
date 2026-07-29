# u115-uploader

一个轻量的 Python CLI，用 115 App 扫码登录后，把指定文件上传到 115 网盘。

## 功能

- 115 开放平台设备码登录、终端二维码和 OAuth token 自动刷新
- 文件、目录和 `*`、`?`、`**` 通配符批量上传
- 单 PUT、顺序分片上传、二次签名和 115 callback 入库确认
- 上传后按文件名、大小、SHA1 强校验
- 远端已存在相同文件时默认跳过，不重复上传
- 强校验成功后按需删除或归档本地源文件
- 统一列出、递归搜索、重复文件检查和精确 ID 回收站操作
- JSON Lines manifest 审计记录和网络错误有限重试

默认安全语义是：不存在则上传；同名且大小、SHA1 一致则跳过；同名但内容不一致则报错。
任何 callback、网络、列表或校验错误都不会被当作成功。

## 安装

需要 Python 3.12+。推荐使用 `uv`：

```bash
cd /path/to/u115-uploader
uv sync
```

将项目注册为当前用户的系统命令：

```bash
cd /path/to/u115-uploader
uv tool install --editable .
uv tool update-shell
```

重新打开终端后可直接使用：

```bash
u115 --help
u115 login
u115 upload "/path/to/input/sample.dat" --remote-dir "/remote-target"
```

`--editable` 让命令直接使用当前项目源码，修改代码后无需重复安装。取消注册：

```bash
uv tool uninstall u115-uploader
```

以上安装命令不会由项目测试自动执行，避免测试过程修改当前用户的 PATH 或工具目录。
不注册系统命令时，开发者仍可在项目目录使用 `uv run u115 ...`。

## 快速开始

```bash
u115 login
u115 upload "/path/to/input/*" --remote-dir "/remote-target"
u115 list "/remote-target" --type file
```

上传成功且远端强校验通过后删除本地源文件：

```bash
u115 upload "/path/to/input/*" \
  --remote-dir "/remote-target" \
  --delete-source-after-verify
```

## 登录

首次使用先运行：

```bash
u115 login
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
u115 login --force
```

将认证二维码同时导出为 PNG 图片：

```bash
u115 login --force --qr-output ./115-login.png
```

命令仍会在终端显示二维码，同时把同一个二维码写入指定图片。输出目录不存在时会自动创建。
如果已有有效登录态且未指定 `--force`，不会重新申请二维码，因此也不会生成图片。

使用自定义登录态文件：

```bash
u115 login --tokens /secure/path/115-tokens.json
```

也可以通过环境变量统一指定：

```bash
export U115_TOKEN_PATH=/secure/path/115-tokens.json
u115 login
```

## 上传

上传到根目录：

```bash
u115 upload /path/to/input/sample.dat
```

上传到指定的 115 目录路径：

```bash
u115 upload /path/to/input/sample.dat --remote-dir "/remote-target/subdirectory"
```

递归上传文件夹中的全部普通文件：

```bash
u115 upload "/path/to/input-directory" --remote-dir "/remote-target/subdirectory"
```

文件夹层级仅用于查找本地文件；所有文件仍上传到同一个 115 目标目录，不会自动创建远端子目录。

使用通配符上传；建议用引号阻止 Shell 提前展开，由程序统一处理 `*`、`?` 和
递归匹配 `**`：

```bash
u115 upload "/path/to/input/**/*.dat" --remote-dir "/remote-target"
```

同一个文件被多个参数或模式重复匹配时只会上传一次。通配符没有匹配项、文件夹为空或
输入路径不存在时，命令会明确报错。

空格、中文和通配符元字符等特殊字符文件名应使用引号。已存在的路径会优先按字面处理，
因此下例中的方括号、星号和问号不会被当作通配符：

```bash
u115 upload "/path/to/input/sample [final]*?.dat"
```

文件名以 `-` 开头时，在文件参数前使用 `--` 结束选项解析：

```bash
u115 upload -- "-特殊文件.txt"
```

也支持把远端目录放在最后：

```bash
u115 upload "/path/to/input/sample-*.dat" /remote-target
```

远端路径必须：

- 从根目录开始并以 `/` 开头。
- 每一级目录都已经存在。
- 目录名称完全匹配，包括空格和大小写。

如果某一级不存在或存在同名目录，命令会明确报错，不会擅自创建目录或上传到其他位置。

也可以直接指定目录 CID，并设置 64 MiB 分片：

```bash
u115 upload /path/to/input/sample.dat --cid 123456 --part-size 64
```

`--remote-dir` 与 `--cid` 不能同时使用。不指定时上传到根目录。

每个文件上传后都会重新读取目标目录，并同时核对远端文件名、大小和 SHA1。只有三项
完全一致才算完成。需要在强校验后删除本地源文件时，显式添加：

```bash
u115 upload "/path/to/input/*.dat" \
  --remote-dir "/remote-target" \
  --delete-source-after-verify
```

更保守的做法是把已校验文件移动到本地归档目录：

```bash
u115 upload "/path/to/input/*.dat" \
  --remote-dir "/remote-target" \
  --move-source-after-verify "/path/to/processed"
```

同名文件策略由 `--on-conflict` 控制：

- `verify`：默认策略；大小和 SHA1 一致则跳过上传，不一致则报错。
- `error`：发现同名文件立即报错。
- `skip`：不校验直接跳过；不能与本地删除或移动选项一起使用。
- `rename`：使用 `文件名 (N).扩展名` 上传，并对新名称执行强校验。

仅对网络传输错误进行有限重试，并把成功结果追加到 JSON Lines manifest：

```bash
u115 upload "/path/to/input/*.dat" \
  --remote-dir "/remote-target" \
  --retry 2 \
  --manifest "./upload-manifest.jsonl"
```

callback、SHA1、冲突或其他业务错误不会重试，避免掩盖协议问题。

如果没有提前执行 `login`，第一次执行 `upload` 时也会自动显示二维码并等待登录。

上传时使用自定义登录态文件：

```bash
u115 upload /path/to/input/sample.dat --tokens /secure/path/115-tokens.json
```

## 独立校验

不上传，只确认本地文件是否已经完整存在于指定 115 目录：

```bash
u115 verify "/path/to/input/sample.dat" --remote-dir "/remote-target"
```

`verify` 同样支持 `--cid`、通配符和 `--manifest`。

## 统一列表、搜索与重复文件

`list` 统一输出文件和文件夹：

```bash
u115 list "/remote-target"
u115 list --cid 123456
```

默认最多输出 100 条，达到上限后立即停止继续读取文件分页或后续递归目录。可调整单次
输出上限：

```bash
u115 list "/remote-target" --limit 500
```

确实需要读取当前范围内全部条目时，必须显式允许：

```bash
u115 list "/remote-target" --all
```

按类型筛选，或递归读取全部后代：

```bash
u115 list "/remote-target" --type file
u115 list "/remote-target" --type folder --recursive
u115 list "/remote-target" --type all --recursive
```

递归默认最多深入 20 层，可按目录结构收紧：

```bash
u115 list "/remote-target" --recursive --max-depth 5
```

为避免只有少量文件但目录树极大的场景产生无界请求，目录扫描默认最多 1000 个。确实
需要扫描更大的目录树时显式提高：

```bash
u115 list "/remote-target" --recursive --max-directories 5000
```

在整个账号中搜索：

```bash
u115 list --search "sample-keyword"
u115 list --search ".dat" --type file
```

按 SHA1 输出当前范围内的重复文件：

```bash
u115 list "/remote-target" --type file --duplicates --all
u115 list "/remote-target" --recursive --duplicates --all
```

`--duplicates` 需要完整结果才能正确分组，因此强制要求同时指定 `--all`；它只报告，
不自动删除。统一输出为 Tab 分隔的 `TYPE`、`ID`、
`PARENT_ID`、`SIZE`、`SHA1`、`NAME` 六列；文件夹的大小和 SHA1 为空。

## 删除到回收站

远端删除使用精确文件 ID，并移动到 115 回收站：

```bash
u115 delete 3483568169358984581 \
  --parent-cid 3392823418375834914 \
  --yes
```

`delete` 不接受文件名或模糊搜索，必须同时提供文件当前父 CID 和 `--yes`；它不会执行
回收站永久清空。可先通过 `list` 取得精确 ID 和父 CID。

查看完整参数：

```bash
u115 --help
u115 login --help
u115 upload --help
u115 verify --help
u115 list --help
u115 delete --help
```

## 测试

```bash
uv run pytest
```
