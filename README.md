# u115-uploader

A lightweight Python CLI that logs in via the 115 App QR code and uploads
specified files to your 115 cloud drive.

> Chinese version: [README.zh-CN.md](./README.zh-CN.md)

## Features

- 115 Open Platform device-code login with terminal QR code and automatic
  OAuth token refresh
- Batch upload of files, directories, and `*`, `?`, `**` glob patterns
- Single PUT, sequential multipart upload, secondary signature, and 115
  callback completion confirmation
- Strong post-upload verification by file name, size, and SHA1
- Skips files that already exist remotely with identical content; never
  uploads duplicates
- Optionally creates missing remote directory levels explicitly
- Optional deletion or archival of local source files after strong
  verification succeeds
- Unified listing, recursive search, duplicate-file detection, and
  precise ID-based trash operations
- Account, space, and VIP summary; explicit directory creation; and
  ID-based move, copy, and rename
- Full trash lifecycle: listing, restore to the original location, and
  explicit permanent purge
- Browser-UA-signed download direct links for scripting
- 115 cloud download (offline) tasks: URL and BT batch adding, listing,
  deletion, quota, and typed clearing
- JSON Lines manifest audit trail and bounded retry on transient network
  errors

The default semantics are safe by design: upload if the file is missing;
skip if a remote file with the same name, size, and SHA1 already exists;
raise an error if the name matches but the content differs. No callback,
network, listing, or verification error is ever treated as success.

## Installation

Requires Python 3.12 or newer. `uv` is the recommended installer:

```bash
cd /path/to/u115-uploader
uv sync
```

To register the project as a system-wide command for the current user:

```bash
cd /path/to/u115-uploader
uv tool install --editable .
uv tool update-shell
```

After opening a new terminal the command is available directly:

```bash
u115 --help
u115 login
u115 upload "/path/to/input/sample.dat" --remote-dir "/remote-target"
```

`--editable` makes the command run straight from the project source, so
edits do not require reinstallation. To unregister:

```bash
uv tool uninstall u115-uploader
```

The installation commands above are not exercised by the test suite, so
they cannot mutate the current user's `PATH` or tool directory during
testing. Without installing, developers can still run `uv run u115 ...`
from inside the project directory.

## Quick Start

```bash
u115 login
u115 upload "/path/to/input/*" --remote-dir "/remote-target"
u115 list "/remote-target" --type file
```

Delete local source files after a successful upload and strong remote
verification:

```bash
u115 upload "/path/to/input/*" \
  --remote-dir "/remote-target" \
  --delete-source-after-verify
```

## Login

Run this on first use:

```bash
u115 login
```

A QR code appears in the terminal. Then:

1. Open the **115 App** on your phone.
2. Use the scanner inside the 115 App to scan the QR code in the terminal.
3. Tap confirm on the phone.
4. Wait until the terminal reports `登录成功` (Login successful).

After a successful login the OAuth session is persisted with `0600`
permissions to:

```text
~/.config/u115-uploader/tokens.json
```

Future uploads reuse this session automatically; no further scan is needed.
When the access token nears expiry the program refreshes it with the
refresh token and writes the new value back to the file.

To switch accounts or force a fresh scan:

```bash
u115 login --force
```

To export the login QR code as a PNG image at the same time:

```bash
u115 login --force --qr-output ./115-login.png
```

The QR code is still rendered in the terminal and additionally written to
the requested image. The output directory is created if it does not
exist. If a valid session already exists and `--force` is omitted, no
fresh QR code is requested and therefore no image is produced.

To use a custom token file:

```bash
u115 login --tokens /secure/path/115-tokens.json
```

The path can also be set globally via an environment variable:

```bash
export U115_TOKEN_PATH=/secure/path/115-tokens.json
u115 login
```

### Choosing an Open Platform app identity

The 115 Open Platform authorizes per *app*, and each AppID keeps its own
authorization record and permission scope. List the built-in catalog first:

```bash
u115 list-apps
```

Each line is `AppID<TAB>app name`, and the default app is marked `(default)`:

```text
100195125	媒体播放器
100195137	VidHub 播放器 (default)
100195303	OpenList
...
```

Select an app for login with `--client-id`. It accepts either the AppID or the
app name (case-insensitive):

```bash
u115 login --force --client-id 100195303
u115 login --force --client-id OpenList
```

Two behaviours are worth calling out:

- `--client-id` **only takes effect when a new login is required**. An existing
  valid session is reused as-is, so pair it with `--force` when you actually
  want to switch identity.
- An unknown AppID fails immediately and lists every valid value; it never
  silently falls back to the default app.

The command echoes the app identity actually used, so the result is verifiable:

```text
登录成功：your-name
应用身份：OpenList（100195303）
登录态已保存：/home/user/.config/u115-uploader/tokens.json
```

Keep different identities isolated with separate token files:

```bash
u115 login --force --client-id OpenList --tokens ~/.config/u115-uploader/openlist.json
u115 whoami --tokens ~/.config/u115-uploader/openlist.json
```

## Upload

Upload to the root directory:

```bash
u115 upload /path/to/input/sample.dat
```

Upload to a specific 115 directory path:

```bash
u115 upload /path/to/input/sample.dat --remote-dir "/remote-target/subdirectory"
```

Recursively upload every regular file inside a directory:

```bash
u115 upload "/path/to/input-directory" --remote-dir "/remote-target/subdirectory"
```

The local directory hierarchy is used only for file discovery; every
file is uploaded to the same 115 target directory and no remote
subdirectories are created automatically.

Glob patterns are supported. Quote them to prevent the shell from
expanding them early so the program can handle `*`, `?`, and recursive
`**` consistently:

```bash
u115 upload "/path/to/input/**/*.dat" --remote-dir "/remote-target"
```

When the same file matches multiple arguments or patterns it is uploaded
once. Patterns that match nothing are skipped with a notice and other
inputs continue to be processed. If every pattern is unmatched, no
login or upload occurs and the command exits with code `3` to indicate
"no work". Paths without wildcards that simply do not exist, or empty
directories, still produce an explicit error.

File names that contain spaces, CJK characters, or glob metacharacters
should be quoted. Paths that already exist on disk are resolved
literally first, so brackets, asterisks, and question marks in the
example below are not treated as wildcards:

```bash
u115 upload "/path/to/input/sample [final]*?.dat"
```

For file names that begin with `-`, place `--` before the file argument
to end option parsing:

```bash
u115 upload -- "-special-file.txt"
```

You can also place the remote directory as the last argument:

```bash
u115 upload "/path/to/input/sample-*.dat" /remote-target
```

Remote paths must:

- Start at the root and begin with `/`.
- Have every directory level already exist unless `--create-remote-dir`
  is explicitly enabled.
- Match directory names exactly, including spaces and case.

By default, a missing level or name collision is an explicit error. For
automated upload jobs that should create missing levels beneath an
existing or newly created parent path, enable it explicitly:

```bash
u115 upload "/path/to/input/*.dat" \
  --remote-dir "/withny/streamer-name" \
  --create-remote-dir
```

The command reuses existing directories level by level and creates only
missing levels. Duplicate sibling names, API failures, or a directory
name rewritten by 115 remain hard errors; the upload never silently
targets a different directory.

You can also target a directory directly by CID with 64 MiB parts:

```bash
u115 upload /path/to/input/sample.dat --cid 123456 --part-size 64
```

`--remote-dir` and `--cid` cannot be combined. When neither is given the
file is uploaded to the root directory.

After every upload the target directory is re-read and the remote file
name, size, and SHA1 are re-checked. Only when all three match is the
upload considered complete. To delete the local source after strong
verification passes, add the flag explicitly:

```bash
u115 upload "/path/to/input/*.dat" \
  --remote-dir "/remote-target" \
  --delete-source-after-verify
```

A more conservative approach is to move verified files into a local
archive directory:

```bash
u115 upload "/path/to/input/*.dat" \
  --remote-dir "/remote-target" \
  --move-source-after-verify "/path/to/processed"
```

The same-name policy is controlled by `--on-conflict`:

- `verify` (default): skip the upload if the size and SHA1 already
  match; raise an error otherwise.
- `error`: raise immediately when a same-name file is found.
- `skip`: skip without verifying; cannot be combined with the
  delete/move-source options.
- `rename`: upload as `name (N).ext` and run strong verification on the
  new name.

Network transport errors are retried a bounded number of times, and
successful results are appended to a JSON Lines manifest:

```bash
u115 upload "/path/to/input/*.dat" \
  --remote-dir "/remote-target" \
  --retry 2 \
  --manifest "./upload-manifest.jsonl"
```

Callback, SHA1, conflict, or other protocol errors are never retried so
the underlying problem is never hidden.

If `login` was not run beforehand, the first `upload` will automatically
render the QR code and wait for login.

To use a custom token file when uploading:

```bash
u115 upload /path/to/input/sample.dat --tokens /secure/path/115-tokens.json
```

## Verification Only

Without uploading, confirm whether a local file already exists
completely in the target 115 directory:

```bash
u115 verify "/path/to/input/sample.dat" --remote-dir "/remote-target"
```

`verify` also supports `--cid`, glob patterns, and `--manifest`.

## Unified Listing, Search, and Duplicates

`list` outputs files and folders together:

```bash
u115 list "/remote-target"
u115 list --cid 123456
```

By default at most 100 entries are emitted, and the command stops as
soon as that limit is reached without reading further file pages or
recursing into more directories. Adjust the per-call cap with:

```bash
u115 list "/remote-target" --limit 500
```

To deliberately read every entry in the current scope, opt in
explicitly:

```bash
u115 list "/remote-target" --all
```

Filter by type or recurse into all descendants:

```bash
u115 list "/remote-target" --type file
u115 list "/remote-target" --type folder --recursive
u115 list "/remote-target" --type all --recursive
```

Recursive listings default to 20 levels deep; tighten that for shallow
trees:

```bash
u115 list "/remote-target" --recursive --max-depth 5
```

To prevent unbounded requests when a tree is enormous but contains few
files, directory scans default to 1000 directories. Raise that cap
explicitly when needed:

```bash
u115 list "/remote-target" --recursive --max-directories 5000
```

Search across the entire account:

```bash
u115 list --search "sample-keyword"
u115 list --search ".dat" --type file
```

Report duplicate files in the current scope by SHA1:

```bash
u115 list "/remote-target" --type file --duplicates --all
u115 list "/remote-target" --recursive --duplicates --all
```

`--duplicates` requires the full result set to group correctly, so it
forces `--all`. It only reports duplicates; it never deletes them. The
output is always six tab-separated columns: `TYPE`, `ID`, `PARENT_ID`,
`SIZE`, `SHA1`, `NAME`. Folders have empty size and SHA1 columns.

## Trash Deletion

Remote deletion uses precise file IDs and moves files into the 115
trash:

```bash
u115 delete <FILE_ID> \
  --parent-cid <PARENT_CID> \
  --yes
```

`delete` accepts neither file names nor fuzzy searches. It requires the
file's current parent CID and `--yes`. Use `list` first to look up the
exact ID and parent CID.

The `trash` command group manages what `delete` produced:

```bash
u115 trash list --all
u115 trash list --limit 50
u115 trash recover <TRASH_ID> [<TRASH_ID> ...]
u115 trash purge <TRASH_ID> [<TRASH_ID> ...] --yes
```

`trash list` prints `TRASH_ID`, `TYPE`, `SIZE`, `DELETED_AT`, `PARENT`,
and `NAME` columns. `trash recover` restores entries to their original
location; per-entry failures are reported and the command exits `1` if
any entry could not be restored. `trash purge` permanently deletes
entries and is therefore gated behind `--yes`.

## Account and Directory Management

Show the current identity, space usage, and VIP status:

```bash
u115 whoami
```

Create missing remote directory levels explicitly (existing levels are
reused, never duplicated):

```bash
u115 mkdir "/backup/photos/2026"
```

## Move, Copy, and Rename

These commands operate on precise file or folder IDs obtained from
`list`; they accept neither names nor fuzzy searches:

```bash
u115 move <FILE_ID> [<FILE_ID> ...] --remote-dir "/target"
u115 move <FILE_ID> --cid <CID>
u115 copy <FILE_ID> [<FILE_ID> ...] --remote-dir "/target"
u115 copy <FILE_ID> --cid <CID> --no-dupli
u115 rename <FILE_ID> "new-name.ext"
```

`--remote-dir` and `--cid` are mutually exclusive and the destination
directory must already exist. By default 115 resolves same-name
collisions during `copy` automatically; `--no-dupli` rejects the copy
instead. `rename` rejects empty names, `.`/`..`, and names containing
`/`.

## Download Links

Print a signed CDN download URL for one file ID. The URL is the only
stdout output, so it can be piped directly:

```bash
u115 download-url <FILE_ID>
```

Folders have no download URL and produce an explicit error.

## Offline Downloads

The `offline` command group drives 115 cloud download tasks:

```bash
u115 offline add "https://example.com/file.iso" "magnet:?xt=..." \
  --remote-dir "/downloads"
u115 offline add-bt "/path/to/sample.torrent" --remote-dir "/downloads"
u115 offline list --page 1
u115 offline rm <INFO_HASH> [<INFO_HASH> ...]
u115 offline rm <INFO_HASH> --delete-files --yes
u115 offline clear --yes
u115 offline clear --flag 0 --yes
u115 offline quota
```

- `offline add` submits one or more HTTP/FTP/magnet/ED2K links; each
  link is reported individually and any rejected link makes the command
  exit `1`.
- `offline add-bt` uploads the `.torrent` file into the save directory,
  parses it remotely, and creates the BT task. Pass `--wanted` with
  comma-separated file indexes to download only part of the torrent.
- `offline list` prints `STATUS`, `PERCENT`, `SIZE`, `INFO_HASH`, and
  `NAME` columns for one page.
- `offline rm` removes tasks; combine with `--delete-files --yes` to
  also delete the downloaded source files.
- `offline clear` empties tasks by type: `--flag` accepts `0`
  (completed), `1` (all, default), `2` (failed), `3` (running), and `4`
  / `5` (same as `0` / `1` but also deleting source files).
- `offline quota` shows remaining, used, and total quota.

## Exit Codes

- `0`: every requested operation completed, or a remote strong
  verification succeeded and the upload was skipped.
- `1`: filesystem, network, 115 protocol, or strong verification
  failure.
- `2`: invalid command arguments.
- `3`: every glob was unmatched and nothing was done.
- `130`: interrupted by the user.

See the full option list with:

```bash
u115 --help
u115 login --help
u115 list-apps --help
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

## Testing

```bash
uv run pytest
```

## License

Released under the [MIT License](./LICENSE).
