# u115-uploader

一个轻量的 Python CLI，用 115 App 扫码登录后，把指定文件上传到 115 网盘。

> English version: [README.md](./README.md)

## 功能

- 115 开放平台设备码登录、终端二维码和 OAuth token 自动刷新
- 文件、目录和 `*`、`?`、`**` 通配符批量上传
- 单 PUT、顺序分片上传、二次签名和 115 callback 入库确认
- 上传后按文件名、大小、SHA1 强校验
- 远端已存在相同文件时默认跳过，不重复上传
- 可显式逐级创建缺失的远端目录
- 强校验成功后按需删除或归档本地源文件
- 统一列出、递归搜索、重复文件检查和精确 ID 回收站操作
- 账号、空间与会员概览；显式创建目录；按精确 ID 移动、复制、重命名
- 回收站完整生命周期：列出、恢复原位置和显式彻底删除
- 浏览器 UA 签名的下载直链，方便脚本取用
- 115 云下载（离线下载）任务：链接与 BT 批量添加、列表、删除、配额和分类清空
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

同一个文件被多个参数或模式重复匹配时只会上传一次。未匹配的通配符会显示跳过提示，
并继续处理其他输入；如果全部通配符都未匹配，命令不会登录或上传，并返回退出码 `3`
表示“没有工作”。不含通配符的路径不存在、文件夹为空时仍会明确报错。

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
默认要求 `--remote-dir` 已存在；需要为自动任务创建缺失层级时，显式添加：

```bash
u115 upload "/path/to/input/*.dat" \
  --remote-dir "/withny/主播名" \
  --create-remote-dir
```

创建时会从根目录逐级复用已有文件夹，只创建缺失层级；同层重名、
名称被 115 改写或创建接口失败仍会明确报错。

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
u115 delete <FILE_ID> \
  --parent-cid <PARENT_CID> \
  --yes
```

`delete` 不接受文件名或模糊搜索，必须同时提供文件当前父 CID 和 `--yes`。可先通过
`list` 取得精确 ID 和父 CID。

`trash` 命令组管理 `delete` 产生的回收站条目：

```bash
u115 trash list --all
u115 trash list --limit 50
u115 trash recover <TRASH_ID> [<TRASH_ID> ...]
u115 trash purge <TRASH_ID> [<TRASH_ID> ...] --yes
```

`trash list` 输出 `TRASH_ID`、`TYPE`、`SIZE`、`DELETED_AT`、`PARENT` 和 `NAME` 列。
`trash recover` 把条目恢复到原位置；单个条目恢复失败会逐条报告，任一失败退出码为
`1`。`trash purge` 彻底删除条目且不可恢复，因此必须显式指定 `--yes`。

## 账号与目录管理

查看当前身份、空间用量与会员状态：

```bash
u115 whoami
```

逐级创建缺失的远端目录（已存在的层级直接复用，不会重复创建）：

```bash
u115 mkdir "/备份/照片/2026"
```

## 移动、复制与重命名

这些命令操作 `list` 取得的精确文件（夹） ID，不接受文件名或模糊搜索：

```bash
u115 move <FILE_ID> [<FILE_ID> ...] --remote-dir "/目标"
u115 move <FILE_ID> --cid <CID>
u115 copy <FILE_ID> [<FILE_ID> ...] --remote-dir "/目标"
u115 copy <FILE_ID> --cid <CID> --no-dupli
u115 rename <FILE_ID> "新名字.ext"
```

`--remote-dir` 与 `--cid` 互斥，目标目录必须已存在。`copy` 默认由 115 自动处理同名
冲突；`--no-dupli` 表示同名时拒绝复制。`rename` 拒绝空名称、`.`/`..` 以及包含 `/`
的名称。

## 下载直链

按文件 ID 输出经 CDN 签名的下载直链，stdout 只包含 URL 本身，可直接进入管道：

```bash
u115 download-url <FILE_ID>
```

文件夹没有下载直链，会明确报错。

## 离线下载

`offline` 命令组管理 115 云下载任务：

```bash
u115 offline add "https://example.com/file.iso" "magnet:?xt=..." \
  --remote-dir "/下载"
u115 offline add-bt "/path/to/sample.torrent" --remote-dir "/下载"
u115 offline list --page 1
u115 offline rm <INFO_HASH> [<INFO_HASH> ...]
u115 offline rm <INFO_HASH> --delete-files --yes
u115 offline clear --yes
u115 offline clear --flag 0 --yes
u115 offline quota
```

- `offline add` 提交一个或多个 HTTP/FTP/磁力/ED2K 链接；每个链接独立报告结果，
  任一链接被拒绝则退出码为 `1`。
- `offline add-bt` 把 `.torrent` 种子上传到保存目录，远端解析后创建 BT 任务；
  可用 `--wanted` 传逗号分隔的文件下标以下载种子中的部分文件。
- `offline list` 按页输出 `STATUS`、`PERCENT`、`SIZE`、`INFO_HASH` 和 `NAME` 列。
- `offline rm` 删除任务；配合 `--delete-files --yes` 可同时删除已下载到网盘的源文件。
- `offline clear` 按类型清空任务：`--flag` 支持 `0`（已完成）、`1`（全部，默认）、
  `2`（失败）、`3`（进行中）以及 `4` / `5`（同 `0` / `1`，但同时删除对应源文件）。
- `offline quota` 显示剩余、已用与总量配额。

## 退出码

- `0`：请求的操作全部完成，或远端强校验一致后跳过上传。
- `1`：文件系统、网络、115 协议或强校验失败。
- `2`：命令参数错误。
- `3`：通配符全部未匹配，没有执行任何操作。
- `130`：用户中断。

查看完整参数：

```bash
u115 --help
u115 login --help
u115 upload --help
u115 verify --help
u115 list --help
u115 delete --help
u115 whoami --help
u115 mkdir --help
u115 move --help
u115 copy --help
u115 rename --help
u115 trash --help
u115 download-url --help
u115 offline --help
```

## 测试

```bash
uv run pytest
```
