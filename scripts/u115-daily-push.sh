#!/bin/bash
# u115 每日上传通用脚本
#
# 参数：
#   --source-dir <path>      本地源目录（必须）
#   --remote-root <path>     115 网盘远程根目录（必须）
#   --log-dir <path>         日志输出目录（必须）
#   --name <slug>            任务短名，用于日志/文件命名（必须）
#   --display-name <name>    日志显示名，默认 name 首字母大写
#   --extensions <list>        逗号分隔扩展名，默认 m4a,mp4,ts
#   --include-root             同时上传根目录下的零散文件到 remote-root
#
# 所有上传决策由 u115 负责；本脚本只做路径映射、遍历和日志。

set -u
set -o pipefail

SOURCE_DIR=""
REMOTE_ROOT=""
LOG_DIR=""
NAME=""
DISPLAY_NAME=""
EXTENSIONS="m4a,mp4,ts"
INCLUDE_ROOT=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --source-dir)
            SOURCE_DIR="$2"; shift 2 ;;
        --remote-root)
            REMOTE_ROOT="$2"; shift 2 ;;
        --log-dir)
            LOG_DIR="$2"; shift 2 ;;
        --name)
            NAME="$2"; shift 2 ;;
        --display-name)
            DISPLAY_NAME="$2"; shift 2 ;;
        --extensions)
            EXTENSIONS="$2"; shift 2 ;;
        --include-root)
            INCLUDE_ROOT=true; shift ;;
        *)
            echo "未知参数: $1" >&2
            exit 1 ;;
    esac
done

for required in SOURCE_DIR REMOTE_ROOT LOG_DIR NAME; do
    if [ -z "${!required}" ]; then
        echo "缺少必填参数: --$(echo "$required" | tr '_' '-' | tr '[:upper:]' '[:lower:]')" >&2
        exit 1
    fi
done

if [ -z "$DISPLAY_NAME" ]; then
    # bash 4+: 首字母大写
    DISPLAY_NAME="${NAME^}"
fi

[ -f "$HOME/.hermes/.env" ] && set -a && . "$HOME/.hermes/.env" && set +a

# 单例锁：防止 cron 触发时上一个实例还没结束就并发运行
LOCK_FILE="$LOG_DIR/${NAME}.lock"
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ${DISPLAY_NAME} 另一个上传实例正在运行，本次跳过" | tee -a "$LOG_DIR/${NAME}-daily-push.log"
    exit 0
fi

TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
UPLOAD_LOG="$LOG_DIR/${NAME}-daily-push.log"
MANIFEST="$LOG_DIR/${NAME}-manifest-$TIMESTAMP.jsonl"

mkdir -p "$LOG_DIR"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$UPLOAD_LOG"; }

# 执行一次 u115 上传，并把「是否为已知非致命错误」的判断限定在本次输出内。
#
# 旧实现用 `grep ... "$UPLOAD_LOG"` 检查整份累积日志：只要历史上出现过一次
# 「未匹配」（或 PermissionError），之后任何真实失败（认证失效、命令缺失等）
# 都会被误判成非致命，最终 cron 仍报 ok，故障被静默掩盖。
# 这里把本次输出单独留存用于判定，并且显式取 u115 本身的退出码
# （管道里 `$?` 会拿到 tee 的状态，需用 PIPESTATUS[0]）。
run_upload() {
    local label="$1"; shift
    local out_tmp u115_exit
    out_tmp="$(mktemp)"
    u115 upload "$@" 2>&1 | tee -a "$UPLOAD_LOG" > "$out_tmp"
    u115_exit=${PIPESTATUS[0]}
    if [ "$u115_exit" -ne 0 ]; then
        if grep -qE "PermissionError|未匹配" "$out_tmp"; then
            log "[$label] 退出码 $u115_exit（已知非致命错误，继续）"
        else
            log "[$label] 退出码非零 ($u115_exit)，继续下一个目录"
            overall_exit=1
        fi
    fi
    rm -f "$out_tmp"
}

log "========== ${DISPLAY_NAME} 每日上传开始 =========="

if [ ! -d "$SOURCE_DIR" ]; then
    log "源目录不存在: $SOURCE_DIR"
    exit 1
fi

# 解析扩展名
IFS=',' read -ra EXTS <<< "$EXTENSIONS"

overall_exit=0

# 1) 根目录零散文件 → REMOTE_ROOT
if [ "$INCLUDE_ROOT" = true ]; then
    shopt -s nullglob
    loose_files=()
    for ext in "${EXTS[@]}"; do
        loose_files+=( "$SOURCE_DIR"/*.$ext )
    done
    shopt -u nullglob

    if [ ${#loose_files[@]} -gt 0 ]; then
        log "[根目录] 上传 ${#loose_files[@]} 个零散文件到 $REMOTE_ROOT"
        run_upload "根目录" \
            --remote-dir "$REMOTE_ROOT" \
            --create-remote-dir \
            --on-conflict dedupe \
            --delete-source-after-verify \
            --retry 2 \
            --manifest "$MANIFEST" \
            "${loose_files[@]}"
    fi
fi

# 2) 每个子目录 → REMOTE_ROOT/<同名目录>
shopt -s nullglob
for stream_dir in "$SOURCE_DIR"/*/; do
    dir_name="$(basename "$stream_dir")"
    remote_dir="$REMOTE_ROOT/$dir_name"
    candidates=()
    for ext in "${EXTS[@]}"; do
        candidates+=( "$stream_dir"*.$ext )
    done

    if [ ${#candidates[@]} -eq 0 ]; then
        log "[$dir_name] 子目录中没有 $EXTENSIONS 文件，跳过"
        continue
    fi

    log "[$dir_name] 上传 ${#candidates[@]} 个文件到 $remote_dir"
    run_upload "$dir_name" \
        --remote-dir "$remote_dir" \
        --create-remote-dir \
        --on-conflict dedupe \
        --delete-source-after-verify \
        --retry 2 \
        --manifest "$MANIFEST" \
        "${candidates[@]}"
done
shopt -u nullglob

# 统计剩余文件
if [ ${#EXTS[@]} -eq 1 ]; then
    local_count=$(find "$SOURCE_DIR" -type f -name "*.${EXTS[0]}" 2>/dev/null | wc -l)
else
    find_args=()
    for i in "${!EXTS[@]}"; do
        if [ "$i" -gt 0 ]; then
            find_args+=("-o")
        fi
        find_args+=("-name" "*.${EXTS[$i]}")
    done
    local_count=$(find "$SOURCE_DIR" -type f \( "${find_args[@]}" \) 2>/dev/null | wc -l)
fi

log "本地上传后剩余: $local_count 个文件"
log "manifest: $MANIFEST"

if [ $overall_exit -eq 0 ]; then
    log "========== ${DISPLAY_NAME} 上传结束 =========="
else
    log "========== ${DISPLAY_NAME} 上传异常结束 =========="
fi

exit $overall_exit
