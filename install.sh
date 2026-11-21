#!/bin/sh
# hi-hive per-user installer for macOS and Ubuntu-tested Linux.
# Designed to run as: curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL URL | sh

say() { printf '%s\n' "$*"; }

warn() { printf 'warning: %s\n' "$*" >&2; }

die() { printf 'error: %s\n' "$*" >&2; exit "${2:-1}"; }

usage() {
  cat <<'EOF'
Install or update hi-hive from the exact current main commit.

Usage: install.sh [options]
  --plain               Plain non-interactive output
  --no-modify-path      Do not add uv's tool-bin directory to shell startup
  --no-service          Do not install a background service
  --port PORT           Server port (default: 4567)
  --repair              Reinstall even when the recorded commit is current
  --force               Reinstall and allow replacing an editable development install
  --uninstall           Remove the service, tool, installer PATH block and record
  --help                Show this help

Uninstall preserves ~/.hihive (or HIVE_HOME), including tasks, settings and token.
EOF
}

cleanup() {
  code=$?
  trap - 0 HUP INT TERM
  if [ -n "${PROFILE_TMP:-}" ]; then rm -f -- "$PROFILE_TMP"; fi
  if [ -n "${RECORD_TMP:-}" ]; then rm -f -- "$RECORD_TMP"; fi
  if [ -n "${RESTORE_TMP:-}" ]; then rm -f -- "$RESTORE_TMP"; fi
  if [ -n "${UV_STAGE:-}" ]; then rm -f -- "$UV_STAGE"; fi
  if [ -n "${UVX_STAGE:-}" ]; then rm -f -- "$UVX_STAGE"; fi
  if [ -n "${BOOTSTRAP_UV_CREATED:-}" ]; then rm -f -- "$BOOTSTRAP_UV_CREATED" "${BOOTSTRAP_UV_CREATED}x"; fi
  if [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ]; then
    case "$TMP_DIR" in "${TMPDIR:-/tmp}"/hi-hive-install.*) rm -rf -- "$TMP_DIR" ;; esac
  fi
  exit "$code"
}

make_temp() {
  base=${TMPDIR:-/tmp}
  case "$base" in /*) ;; *) die 'TMPDIR must be an absolute path' ;; esac
  TMP_DIR=$(mktemp -d "$base/hi-hive-install.XXXXXX") || die 'could not create a private temporary directory'
  case "$TMP_DIR" in /*) ;; *) die 'mktemp returned a non-absolute path' ;; esac
  [ -d "$TMP_DIR" ] && [ ! -L "$TMP_DIR" ] || die 'temporary directory is unsafe'
  tmp_uid=$(ls -dn "$TMP_DIR" 2>/dev/null | awk '{print $3}')
  tmp_mode=$(LC_ALL=C ls -ld "$TMP_DIR" 2>/dev/null | cut -c1-10)
  [ "$tmp_uid" = "$(id -u)" ] && [ "$tmp_mode" = drwx------ ] || die 'temporary directory is not private and user-owned'
}

need_curl() {
  command -v curl >/dev/null 2>&1 || die 'curl is required'
}

fetch() {
  url=$1
  destination=$2
  need_curl
  curl --proto '=https' --proto-redir '=https' --tlsv1.2 --location --fail --silent --show-error \
    --output "$destination" -- "$url"
}

normal_bin_dir() {
  if [ -n "${XDG_BIN_HOME:-}" ]; then printf '%s\n' "$XDG_BIN_HOME"
  elif [ -n "${XDG_DATA_HOME:-}" ]; then printf '%s\n' "$(dirname -- "$XDG_DATA_HOME")/bin"
  else printf '%s\n' "$HOME/.local/bin"
  fi
}

require_safe_user_directory() {
  safe_directory=$1
  safe_label=$2
  [ -d "$safe_directory" ] && [ ! -L "$safe_directory" ] || die "$safe_label must be a real directory: $safe_directory"
  safe_uid=$(ls -dn "$safe_directory" 2>/dev/null | awk '{print $3}')
  safe_mode=$(LC_ALL=C ls -ld "$safe_directory" 2>/dev/null | cut -c1-10)
  safe_write_bits=$(printf '%s' "$safe_mode" | cut -c6,9)
  [ "$safe_uid" = "$(id -u)" ] || die "$safe_label must be owned by the current user: $safe_directory"
  case "$safe_write_bits" in *w*) die "$safe_label must not be group/world-writable: $safe_directory" ;; esac
  [ -w "$safe_directory" ] || die "$safe_label is not writable: $safe_directory"
}

require_safe_destination_parent() {
  safe_parent=$1
  case "$safe_parent" in
    "$HOME"|"$HOME"/*)
      while [ "$safe_parent" != "$HOME" ]; do
        if [ -e "$safe_parent" ] || [ -L "$safe_parent" ]; then
          require_safe_user_directory "$safe_parent" 'destination ancestor'
        fi
        safe_parent=$(dirname -- "$safe_parent")
      done
      require_safe_user_directory "$HOME" 'HOME'
      ;;
    *)
      while [ ! -e "$safe_parent" ] && [ ! -L "$safe_parent" ]; do
        next_parent=$(dirname -- "$safe_parent")
        [ "$next_parent" != "$safe_parent" ] || break
        safe_parent=$next_parent
      done
      require_safe_user_directory "$safe_parent" 'destination ancestor'
      ;;
  esac
}

require_safe_regular_file() {
  safe_file=$1
  safe_label=$2
  [ ! -L "$safe_file" ] || die "$safe_label must not be a symbolic link: $safe_file"
  [ ! -e "$safe_file" ] && return 0
  [ -f "$safe_file" ] || die "$safe_label must be a regular file: $safe_file"
  safe_uid=$(ls -dn "$safe_file" 2>/dev/null | awk '{print $3}')
  safe_mode=$(LC_ALL=C ls -ld "$safe_file" 2>/dev/null | cut -c1-10)
  safe_write_bits=$(printf '%s' "$safe_mode" | cut -c6,9)
  [ "$safe_uid" = "$(id -u)" ] || die "$safe_label must be owned by the current user: $safe_file"
  case "$safe_write_bits" in *w*) die "$safe_label must not be group/world-writable: $safe_file" ;; esac
}

validate_install_paths() {
  case "$UV$TOOL_BIN" in *\\*|*\"*) die 'uv or tool-bin path contains a character unsupported by the install record' ;; esac
  case "$UV$TOOL_BIN" in *"
"*) die 'uv or tool-bin path contains a newline' ;; esac
}

atomic_restore_file() {
  restore_source=$1
  restore_destination=$2
  restore_parent=$(dirname -- "$restore_destination")
  mkdir -p "$restore_parent"
  require_safe_user_directory "$restore_parent" 'restore directory'
  require_safe_regular_file "$restore_destination" 'restore target'
  RESTORE_TMP=$(mktemp "$restore_destination.hi-hive.XXXXXX") || die 'could not create a private restore temporary file'
  cp -p "$restore_source" "$RESTORE_TMP"
  mv -f "$RESTORE_TMP" "$restore_destination"
  RESTORE_TMP=
}

write_record() {
  commit=$1
  validate_install_paths
  mkdir -p "$state_dir"
  require_safe_user_directory "$state_dir" 'install state directory'
  require_safe_regular_file "$record_file" 'install record'
  RECORD_TMP=$(mktemp "$state_dir/.install.json.XXXXXX") || die 'could not create a private install-record temporary file'
  printf '{"version":"%s","commit":"%s","source":"main","uv":"%s","tool_bin":"%s"}\n' \
    "$PACKAGE_VERSION" "$commit" "$UV" "$TOOL_BIN" > "$RECORD_TMP"
  chmod 600 "$RECORD_TMP"
  mv -f "$RECORD_TMP" "$record_file"
  RECORD_TMP=
}

read_record_commit() {
  [ -f "$record_file" ] || return 0
  sed -n 's/.*"commit"[[:space:]]*:[[:space:]]*"\([0-9A-Fa-f]*\)".*/\1/p' "$record_file" | head -n 1 | tr 'A-F' 'a-f'
}

find_uv() {
  found=$(command -v uv 2>/dev/null || true)
  if [ -z "$found" ] && [ -n "${SHELL:-}" ] && [ -x "$SHELL" ]; then
    marker=HIHIVE_UV_PATH_
    output=$("$SHELL" -l -i -c "printf '\n${marker}%s\n' \"\$(command -v uv 2>/dev/null || true)\"" </dev/null 2>/dev/null || true)
    found=$(printf '%s\n' "$output" | sed -n "s/^${marker}//p" | tail -n 1)
  fi
  if [ -z "$found" ]; then
    standard_uv=$(normal_bin_dir)/uv
    [ ! -x "$standard_uv" ] || found=$standard_uv
  fi
  case "$found" in /*) [ -x "$found" ] && printf '%s\n' "$found" ;; esac
}

uv_compatible() {
  output=$("$1" --version 2>/dev/null || true)
  version=$(printf '%s\n' "$output" | sed -n 's/^uv \([0-9][0-9.]*\).*/\1/p' | head -n 1)
  [ -n "$version" ] || return 1
  major=$(printf '%s' "$version" | cut -d. -f1)
  minor=$(printf '%s' "$version" | cut -d. -f2)
  [ "$major" -gt 0 ] || [ "$minor" -ge 7 ]
}

editable_install_source() {
  tool_root=$("$UV" tool dir 2>/dev/null || true)
  case "$tool_root" in /*) ;; *) return 1 ;; esac
  tool_python=$tool_root/hi-hive/bin/python
  [ -x "$tool_python" ] || return 1
  "$tool_python" -c '
import json
from importlib.metadata import distribution
from urllib.parse import unquote, urlparse

try:
    direct = json.loads(distribution("hi-hive").read_text("direct_url.json") or "{}")
except Exception:
    raise SystemExit(1)
if not direct.get("dir_info", {}).get("editable"):
    raise SystemExit(1)
source = urlparse(direct.get("url", ""))
if source.scheme != "file":
    raise SystemExit(1)
print(unquote(source.path))
' 2>/dev/null
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  else die 'sha256sum or shasum is required to verify uv'
  fi
}

install_uv() {
  bin_dir=$(normal_bin_dir)
  case "$bin_dir" in /*) ;; *) die 'uv executable directory must be absolute' ;; esac
  [ ! -e "$bin_dir/uv" ] && [ ! -L "$bin_dir/uv" ] \
    || die "refusing to overwrite existing $bin_dir/uv; put a compatible uv on PATH or move that file"
  [ ! -e "$bin_dir/uvx" ] && [ ! -L "$bin_dir/uvx" ] \
    || die "refusing to overwrite existing $bin_dir/uvx; put a compatible uv on PATH or move that file"
  case "$OS-$ARCH" in
    macos-aarch64) target=aarch64-apple-darwin; expected=82d4b99dc6ea686695b5ee142ceba03dd3e3eda2b414e94215ab7bce94972fbb ;;
    macos-x86_64) target=x86_64-apple-darwin; expected=664aed584c276f8d79cdc3b7685cd48f5d64657bd6840b06b4b2b0db731b9c99 ;;
    linux-aarch64) target=aarch64-unknown-linux-gnu; expected=c300afd5f2d31df039fe6a26a2d68a76b62832098c272a43e1e74ab9efd4fbd7 ;;
    linux-x86_64) target=x86_64-unknown-linux-gnu; expected=230e328948c92dd1ebad83949c4d56e83813dfe9c6362a4c519e6a227973f1ae ;;
    *) die "unsupported uv platform: $OS-$ARCH" 2 ;;
  esac
  archive=uv-$target.tar.gz
  url=https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$archive
  say "uv           downloading $UV_VERSION from $url"
  fetch "$url" "$TMP_DIR/$archive"
  actual=$(sha256_file "$TMP_DIR/$archive")
  [ "$actual" = "$expected" ] || die 'downloaded uv archive failed SHA-256 verification'
  tar -xzf "$TMP_DIR/$archive" -C "$TMP_DIR"
  require_safe_destination_parent "$bin_dir"
  mkdir -p "$bin_dir"
  require_safe_user_directory "$bin_dir" 'uv executable directory'
  UV_STAGE=$(mktemp "$bin_dir/.uv.hi-hive.XXXXXX") || die 'could not stage uv in its executable directory'
  cp "$TMP_DIR/uv-$target/uv" "$UV_STAGE"
  chmod 755 "$UV_STAGE"
  if ! ln "$UV_STAGE" "$bin_dir/uv"; then
    rm -f "$UV_STAGE"
    UV_STAGE=
    die "refusing to overwrite existing $bin_dir/uv"
  fi
  rm -f "$UV_STAGE"
  UV_STAGE=
  BOOTSTRAP_UV_CREATED=$bin_dir/uv
  if [ -f "$TMP_DIR/uv-$target/uvx" ]; then
    UVX_STAGE=$(mktemp "$bin_dir/.uvx.hi-hive.XXXXXX") || { rm -f "$bin_dir/uv"; BOOTSTRAP_UV_CREATED=; die 'could not stage uvx in its executable directory'; }
    cp "$TMP_DIR/uv-$target/uvx" "$UVX_STAGE"
    chmod 755 "$UVX_STAGE"
    if ! ln "$UVX_STAGE" "$bin_dir/uvx"; then
      rm -f "$UVX_STAGE" "$bin_dir/uv"
      UVX_STAGE=
      BOOTSTRAP_UV_CREATED=
      die "refusing to overwrite existing $bin_dir/uvx"
    fi
    rm -f "$UVX_STAGE"
    UVX_STAGE=
  elif ! ln "$bin_dir/uv" "$bin_dir/uvx"; then
    rm -f "$bin_dir/uv"
    BOOTSTRAP_UV_CREATED=
    die "refusing to overwrite existing $bin_dir/uvx"
  fi
  BOOTSTRAP_UV_CREATED=
  UV=$bin_dir/uv
}

profile_for_shell() {
  shell_name=$(basename -- "${SHELL:-sh}")
  case "$shell_name" in
    zsh) printf '%s\n' "$HOME/.zshrc" ;;
    bash) printf '%s\n' "$HOME/.bashrc" ;;
    sh|dash) printf '%s\n' "$HOME/.profile" ;;
    *) return 1 ;;
  esac
}

path_contains() {
  case ":$PATH:" in *":$1:"*) return 0 ;; *) return 1 ;; esac
}

validate_tool_bin() {
  case "$TOOL_BIN" in /*) ;; *) die 'uv tool-bin directory must be absolute' ;; esac
  [ ! -L "$TOOL_BIN" ] || die "uv tool-bin directory must not be a symbolic link: $TOOL_BIN"
  require_safe_destination_parent "$TOOL_BIN"
  if [ -e "$TOOL_BIN" ]; then
    require_safe_user_directory "$TOOL_BIN" 'uv tool-bin directory'
  fi
}

validate_profile_bin() {
  case "$1" in *[!A-Za-z0-9_./\ -]*|'') die "tool-bin path cannot be written safely to a shell profile: $1" ;; esac
  case "$1" in *"
"*) die 'tool-bin path contains a newline' ;; esac
}

validate_path_block_target() {
  profile=$(profile_for_shell) || return 0
  require_safe_destination_parent "$(dirname -- "$profile")"
  require_safe_regular_file "$profile" 'shell profile'
  validate_profile_bin "$TOOL_BIN"
  [ -e "$profile" ] || return 0
  begins=$(grep -Fxc "$BEGIN_MARKER" "$profile" 2>/dev/null || true)
  ends=$(grep -Fxc "$END_MARKER" "$profile" 2>/dev/null || true)
  if [ "$begins" -eq 1 ] && [ "$ends" -eq 1 ]; then
    actual=$(awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
      $0 == begin { inside=1 } inside { print } $0 == end && inside { exit }
    ' "$profile")
    expected=$(printf "%s\ncase ':\$PATH:' in *':%s:'*) ;; *) export PATH='%s':\"\$PATH\" ;; esac\n%s\n" \
      "$BEGIN_MARKER" "$TOOL_BIN" "$TOOL_BIN" "$END_MARKER")
    [ "$actual" = "$expected" ] || die "profile contains a modified hi-hive PATH block: $profile"
    return 0
  fi
  [ "$begins" -eq 0 ] && [ "$ends" -eq 0 ] \
    || die "profile contains a partial or duplicate hi-hive PATH block: $profile"
}

add_path_block() {
  profile=$(profile_for_shell) || { warn "unsupported shell ${SHELL:-unknown}; add $TOOL_BIN to PATH yourself"; return 0; }
  validate_path_block_target
  [ -e "$profile" ] || : > "$profile"
  begins=$(grep -Fxc "$BEGIN_MARKER" "$profile" 2>/dev/null || true)
  ends=$(grep -Fxc "$END_MARKER" "$profile" 2>/dev/null || true)
  if [ "$begins" -eq 1 ] && [ "$ends" -eq 1 ]; then return 0; fi
  [ "$begins" -eq 0 ] && [ "$ends" -eq 0 ] || die "profile contains a partial or duplicate hi-hive PATH block: $profile"
  cp "$profile" "$TMP_DIR/profile.before"
  {
    cat "$profile"
    [ ! -s "$profile" ] || printf '\n'
    printf '%s\n' "$BEGIN_MARKER"
    printf "case ':\$PATH:' in *':%s:'*) ;; *) export PATH='%s':\"\$PATH\" ;; esac\n" "$TOOL_BIN" "$TOOL_BIN"
    printf '%s\n' "$END_MARKER"
  } > "$TMP_DIR/profile.new"
  PROFILE_TMP=$(mktemp "$profile.hi-hive.XXXXXX") || die 'could not create a private temporary shell profile'
  if [ -e "$profile" ]; then cp -p "$profile" "$PROFILE_TMP"; fi
  cat "$TMP_DIR/profile.new" > "$PROFILE_TMP"
  mv -f "$PROFILE_TMP" "$profile"
  PROFILE_TMP=
  say "PATH         added $TOOL_BIN in $profile"
}

remove_path_block() {
  profile=$(profile_for_shell 2>/dev/null || true)
  [ -n "$profile" ] && [ -f "$profile" ] || return 0
  validate_path_block_target
  begins=$(grep -Fxc "$BEGIN_MARKER" "$profile" 2>/dev/null || true)
  ends=$(grep -Fxc "$END_MARKER" "$profile" 2>/dev/null || true)
  if [ "$begins" -eq 0 ] && [ "$ends" -eq 0 ]; then return 0; fi
  [ "$begins" -eq 1 ] && [ "$ends" -eq 1 ] || die "refusing to remove a partial or duplicate PATH block in $profile"
  awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
    $0 == begin { inside=1; next }
    $0 == end && inside { inside=0; next }
    !inside { print }
  ' "$profile" > "$TMP_DIR/profile.remove"
  PROFILE_TMP=$(mktemp "$profile.hi-hive.XXXXXX") || die 'could not create a private temporary shell profile'
  cp -p "$profile" "$PROFILE_TMP"
  cat "$TMP_DIR/profile.remove" > "$PROFILE_TMP"
  mv -f "$PROFILE_TMP" "$profile"
  PROFILE_TMP=
  say "PATH         removed installer block from $profile"
}

ask_yes() {
  question=$1
  if [ "$PLAIN" -eq 1 ]; then return 0; fi
  printf '%s [Y/n] ' "$question" > "$TTY_DEVICE"
  IFS= read -r answer < "$TTY_DEVICE" || answer=
  case "$answer" in n|N|no|NO|No) return 1 ;; *) return 0 ;; esac
}

ask_run_mode() {
  if [ "$PLAIN" -eq 1 ]; then RUN_BACKGROUND=1; return; fi
  {
    say '? How should hi-hive run?'
    say '  1  In the background (recommended): starts now and at every login, no terminal needed.'
    say "  2  Only while a terminal runs it: nothing runs until you type '$DISPLAY_COMMAND serve'."
    printf '  Choose 1 or 2 [1]: '
  } > "$TTY_DEVICE"
  IFS= read -r answer < "$TTY_DEVICE" || answer=
  case "$answer" in 2) RUN_BACKGROUND=0 ;; *) RUN_BACKGROUND=1 ;; esac
}

service_possible() {
  if [ "$OS" = macos ]; then
    command -v launchctl >/dev/null 2>&1 && launchctl print "gui/$(id -u)" >/dev/null 2>&1
  else
    command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
  fi
}

query_existing_service() {
  service_command=$1
  if "$service_command" service installed; then
    service_query_status=0
  else
    service_query_status=$?
  fi
  case "$service_query_status" in
    0) EXISTING_SERVICE=1 ;;
    3) EXISTING_SERVICE=0 ;;
    *) die 'could not determine whether the manager-visible hi-hive service is installed' ;;
  esac
  SERVICE_STATE_KNOWN=1
  SERVICE_QUERIED=1
}

main() {
set -eu

umask 077

PACKAGE_VERSION=0.2.0
REPOSITORY=https://github.com/ertygiq/hi-hive
API_URL=https://api.github.com/repos/ertygiq/hi-hive/git/ref/heads/main
UV_VERSION=0.10.0
BEGIN_MARKER='# >>> hi-hive installer PATH >>>'
END_MARKER='# <<< hi-hive installer PATH <<<'

PLAIN=0
MODIFY_PATH=1
INSTALL_SERVICE=1
FORCE=0
REPLACE_EDITABLE=0
UNINSTALL=0
PORT=4567
TMP_DIR=
PROFILE_TMP=
RECORD_TMP=
RESTORE_TMP=
UV_STAGE=
UVX_STAGE=
BOOTSTRAP_UV_CREATED=
TTY_DEVICE=



while [ "$#" -gt 0 ]; do
  case "$1" in
    --plain) PLAIN=1 ;;
    --no-modify-path) MODIFY_PATH=0 ;;
    --no-service) INSTALL_SERVICE=0 ;;
    --repair) FORCE=1 ;;
    --force) FORCE=1; REPLACE_EDITABLE=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --port)
      shift
      [ "$#" -gt 0 ] || die 'missing value after --port'
      PORT=$1
      ;;
    --help|-h) usage; exit 0 ;;
    --) shift; break ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done
[ "$#" -eq 0 ] || die "unexpected argument: $1"

case "$PORT" in ''|*[!0-9]*) die "invalid port: $PORT" ;; esac
[ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "invalid port: $PORT"

command -v id >/dev/null 2>&1 || die 'id is required'
[ "$(id -u)" -ne 0 ] || die 'do not run the hi-hive installer as root; it is a per-user install'
[ -n "${HOME:-}" ] || die 'HOME is not set'
case "$HOME" in /*) ;; *) die 'HOME must be an absolute path' ;; esac
require_safe_user_directory "$HOME" 'HOME'
if [ -n "${HIVE_HOME:-}" ]; then case "$HIVE_HOME" in /*) ;; *) die 'HIVE_HOME must be an absolute path' ;; esac; fi
if [ -n "${XDG_STATE_HOME:-}" ]; then case "$XDG_STATE_HOME" in /*) ;; *) die 'XDG_STATE_HOME must be an absolute path' ;; esac; fi
if [ -n "${XDG_BIN_HOME:-}" ]; then case "$XDG_BIN_HOME" in /*) ;; *) die 'XDG_BIN_HOME must be an absolute path' ;; esac; fi
if [ -n "${XDG_DATA_HOME:-}" ]; then case "$XDG_DATA_HOME" in /*) ;; *) die 'XDG_DATA_HOME must be an absolute path' ;; esac; fi
if [ -n "${XDG_CONFIG_HOME:-}" ]; then case "$XDG_CONFIG_HOME" in /*) ;; *) die 'XDG_CONFIG_HOME must be an absolute path' ;; esac; fi

if [ -t 1 ] && [ -r /dev/tty ] && [ "$PLAIN" -eq 0 ]; then
  TTY_DEVICE=/dev/tty
else
  PLAIN=1
fi
if [ -n "${NO_COLOR:-}" ] || [ "${TERM:-}" = dumb ]; then PLAIN=1; fi

trap cleanup 0 HUP INT TERM

make_temp

case "$(uname -s 2>/dev/null || true)" in
  Darwin) OS=macos ;;
  Linux) OS=linux ;;
  *) die 'hi-hive supports macOS and Ubuntu-tested Linux' 2 ;;
esac
case "$(uname -m 2>/dev/null || true)" in
  arm64|aarch64) ARCH=aarch64 ;;
  x86_64|amd64) ARCH=x86_64 ;;
  *) die "unsupported CPU architecture: $(uname -m 2>/dev/null || printf unknown)" 2 ;;
esac



state_dir=${XDG_STATE_HOME:-$HOME/.local/state}/hi-hive
record_file=$state_dir/install.json
[ ! -L "$state_dir" ] || die "install state directory must not be a symbolic link: $state_dir"
require_safe_destination_parent "$state_dir"
if [ -e "$state_dir" ]; then require_safe_user_directory "$state_dir" 'install state directory'; fi
require_safe_regular_file "$record_file" 'install record'


















if [ "$UNINSTALL" -eq 1 ]; then
  UV=$(find_uv || true)
  [ -n "$UV" ] || die 'uv was not found; hi-hive tool removal cannot be verified'
  TOOL_BIN=$("$UV" tool dir --bin)
  case "$TOOL_BIN" in /*) ;; *) die 'uv returned a non-absolute tool-bin directory' ;; esac
  HIHIVE=$TOOL_BIN/hihive
  if [ -x "$HIHIVE" ]; then "$HIHIVE" service uninstall 2>/dev/null || true; fi
  "$UV" tool uninstall hi-hive >/dev/null 2>&1 || true
  remove_path_block
  rm -f -- "$record_file"
  say 'Removed the hi-hive tool and installer-managed service/PATH entry.'
  say "Your data in ${HIVE_HOME:-$HOME/.hihive} was kept."
  exit 0
fi

need_curl
UV=$(find_uv || true)
if [ -n "$UV" ] && ! uv_compatible "$UV"; then
  die "existing uv is too old or unreadable: $UV (update uv, then rerun)"
fi
if [ -n "$UV" ]; then
  UV_VERSION_FOUND=$("$UV" --version | awk '{print $2}')
  say "uv           found $UV_VERSION_FOUND at $UV (external tool already installed)"
  TOOL_BIN=$("$UV" tool dir --bin)
else
  TOOL_BIN=$(normal_bin_dir)
fi
validate_tool_bin
validate_install_paths
if [ -n "$UV" ]; then
  EDITABLE_SOURCE=$(editable_install_source || true)
  if [ -n "$EDITABLE_SOURCE" ] && [ "$REPLACE_EDITABLE" -eq 0 ]; then
    die "hi-hive is installed from a local checkout ($EDITABLE_SOURCE), probably for development.
The installer will not replace it. Nothing was changed.
To update the checkout: git -C '$EDITABLE_SOURCE' pull
To replace it with main: rerun with --force."
  fi
fi
HIVE_DIR=${HIVE_HOME:-$HOME/.hihive}
[ ! -L "$HIVE_DIR" ] || die "hi-hive data directory must not be a symbolic link: $HIVE_DIR"
require_safe_destination_parent "$HIVE_DIR"
if [ -e "$HIVE_DIR" ]; then require_safe_user_directory "$HIVE_DIR" 'hi-hive data directory'; fi
CONFIG_PATH=${HIVE_CONFIG_FILE:-$HIVE_DIR/config.json}
STATE_PATH=$HIVE_DIR/data/state.json
case "$CONFIG_PATH" in /*) ;; *) die 'configuration path must be absolute' ;; esac
require_safe_destination_parent "$(dirname -- "$CONFIG_PATH")"
require_safe_regular_file "$CONFIG_PATH" 'configuration file'
require_safe_destination_parent "$(dirname -- "$STATE_PATH")"
require_safe_regular_file "$STATE_PATH" 'conversation state file'
FRESH=1
[ -f "$CONFIG_PATH" ] && FRESH=0
if [ "$FRESH" -eq 1 ]; then
  say ''
  say 'Safety'
  say 'Harnesses you start in hi-hive can read, change, and delete files on this computer.'
  say 'The Claude Code harness uses --dangerously-skip-permissions by default; you can change that in Settings.'
  say ''
fi
DISPLAY_COMMAND=hihive
PATH_WAS_MISSING=0
if ! path_contains "$TOOL_BIN"; then
  PATH_WAS_MISSING=1
  if [ "$PLAIN" -eq 1 ]; then
    if [ "$MODIFY_PATH" -eq 1 ]; then say "PATH         default yes: add $TOOL_BIN (use --no-modify-path to skip)"; else say 'PATH         skipped by --no-modify-path'; fi
  elif [ "$MODIFY_PATH" -eq 1 ] && ! ask_yes "Add $TOOL_BIN to PATH in your shell startup file?"; then
    MODIFY_PATH=0
  fi
  [ "$MODIFY_PATH" -eq 1 ] || DISPLAY_COMMAND=$TOOL_BIN/hihive
fi

RUN_BACKGROUND=0
FOREGROUND_RUNNING=0
EXISTING_SERVICE=0
SERVICE_STATE_KNOWN=0
SERVICE_QUERIED=0
SERVICE_SKIPPED=0
RUNNING_PROCESS=0
SERVICE_POSSIBLE=0
if service_possible; then SERVICE_POSSIBLE=1; fi
OLD_COMMIT=$(read_record_commit || true)
if [ "$FRESH" -eq 1 ]; then
  if [ "$INSTALL_SERVICE" -eq 0 ]; then RUN_BACKGROUND=0
  elif [ "$SERVICE_POSSIBLE" -eq 1 ]; then ask_run_mode
  else
    say "Runs         only while '$DISPLAY_COMMAND serve' runs (no background service is available here)"
  fi
fi
if [ "$PLAIN" -eq 1 ] && [ "$FRESH" -eq 1 ] && [ "$SERVICE_POSSIBLE" -eq 1 ]; then
  if [ "$INSTALL_SERVICE" -eq 1 ]; then say 'Runs         default 1: background (use --no-service for terminal mode)'; else say 'Runs         terminal mode (--no-service)'; fi
fi

say 'Source        resolving latest main commit from GitHub'
fetch "$API_URL" "$TMP_DIR/main.json"
COMMIT=$(sed -n 's/.*"sha"[[:space:]]*:[[:space:]]*"\([0-9A-Fa-f][0-9A-Fa-f]*\)".*/\1/p' "$TMP_DIR/main.json" | head -n 1 | tr 'A-F' 'a-f')
case "$COMMIT" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
  *) die 'GitHub returned an invalid main commit SHA' ;;
esac
say "Target        hi-hive $PACKAGE_VERSION from main@$COMMIT"

if [ -z "$UV" ]; then install_uv; fi
say "uv           using $("$UV" --version | awk '{print $2}') at $UV"
TOOL_BIN=$("$UV" tool dir --bin)
validate_tool_bin
validate_install_paths
HIHIVE=$TOOL_BIN/hihive
DISPLAY_COMMAND=$HIHIVE
if path_contains "$TOOL_BIN" || [ "$MODIFY_PATH" -eq 1 ]; then DISPLAY_COMMAND=hihive; fi
if [ "$PATH_WAS_MISSING" -eq 1 ] && [ "$MODIFY_PATH" -eq 1 ]; then validate_path_block_target; fi

UNCHANGED=0
if [ "$OLD_COMMIT" = "$COMMIT" ] && [ "$FORCE" -eq 0 ] && [ -x "$HIHIVE" ]; then UNCHANGED=1; fi
ARCHIVE_URL=$REPOSITORY/archive/$COMMIT.tar.gz
BACKUP=
if [ "$UNCHANGED" -eq 1 ]; then
  say "hi-hive      already $PACKAGE_VERSION at main@$COMMIT; package install skipped"
else
  say "hi-hive      preparing exact source $ARCHIVE_URL"
  candidate_tools=$TMP_DIR/candidate-tools
  candidate_bin=$TMP_DIR/candidate-bin
  UV_TOOL_DIR=$candidate_tools UV_TOOL_BIN_DIR=$candidate_bin \
    "$UV" tool install --force --python 3.14 --no-config --no-progress "$ARCHIVE_URL"
  CANDIDATE=$candidate_bin/hihive
  [ -x "$CANDIDATE" ] || die 'candidate installation did not create hihive'
  "$CANDIDATE" --version >/dev/null
  if [ "$FRESH" -eq 0 ]; then
    if [ "$SERVICE_POSSIBLE" -eq 1 ]; then query_existing_service "$CANDIDATE"; fi
    "$CANDIDATE" setup-harnesses --offer --plain >/dev/null
  fi
  candidate_python=$candidate_tools/hi-hive/bin/python
  [ -x "$candidate_python" ] || die 'candidate installation has no Python interpreter'
  PY_VERSION=$("$candidate_python" -c 'import platform; print(platform.python_version())')
  case "$PY_VERSION" in 3.14.*) ;; *) die "uv selected Python $PY_VERSION; hi-hive requires Python 3.14.x" ;; esac
  PY_PATH=$("$candidate_python" -c 'import sys; print(sys.executable)')
  say "Python       selected $PY_VERSION at $PY_PATH (system Python unchanged)"

  if [ "$FRESH" -eq 0 ]; then
    backup_root=$HIVE_DIR/backups
    [ ! -L "$backup_root" ] || die "backup directory must not be a symbolic link: $backup_root"
    require_safe_destination_parent "$backup_root"
    mkdir -p "$backup_root"
    require_safe_user_directory "$backup_root" 'backup directory'
    BACKUP=$backup_root/install-$(date +%Y%m%d-%H%M%S)-$$
    [ ! -e "$BACKUP" ] && [ ! -L "$BACKUP" ] || die "backup path already exists: $BACKUP"
    mkdir "$BACKUP"
    [ ! -f "$CONFIG_PATH" ] || cp "$CONFIG_PATH" "$BACKUP/config.json"
    [ ! -f "$STATE_PATH" ] || cp "$STATE_PATH" "$BACKUP/state.json"
    say "Backup       saved config/state files in $BACKUP"
  fi
  if ! "$UV" tool install --force --python 3.14 --no-config --no-progress "$ARCHIVE_URL"; then
    warn 'The final package install failed.'
    if [ -n "$OLD_COMMIT" ]; then
      warn "Restoring the previously recorded package main@$OLD_COMMIT."
      "$UV" tool install --force --python 3.14 --no-config --no-progress "$REPOSITORY/archive/$OLD_COMMIT.tar.gz" \
        || warn 'Automatic package restore also failed; user data was not changed.'
    fi
    die 'hi-hive was not updated'
  fi
  if [ ! -x "$HIHIVE" ]; then
    if [ -n "$OLD_COMMIT" ]; then
      "$UV" tool install --force --python 3.14 --no-config --no-progress "$REPOSITORY/archive/$OLD_COMMIT.tar.gz" \
        || warn 'Automatic package restore also failed; user data was not changed.'
    fi
    die "uv did not install hihive in $TOOL_BIN"
  fi
fi
if [ "$FRESH" -eq 0 ] && [ "$SERVICE_POSSIBLE" -eq 1 ] && [ "$SERVICE_QUERIED" -eq 0 ]; then
  query_existing_service "$HIHIVE"
fi
if [ "$FRESH" -eq 0 ] && [ "$SERVICE_STATE_KNOWN" -eq 1 ] && [ "$EXISTING_SERVICE" -eq 1 ]; then
  if [ "$INSTALL_SERVICE" -eq 1 ] && [ "$SERVICE_POSSIBLE" -eq 1 ]; then
    RUN_BACKGROUND=1
  else
    SERVICE_SKIPPED=1
  fi
fi
write_record "$COMMIT"

if [ "$PATH_WAS_MISSING" -eq 1 ] && [ "$MODIFY_PATH" -eq 1 ]; then add_path_block; fi

say ''
say 'Configuration and readiness'
if [ "$FRESH" -eq 1 ]; then
  if [ "$PLAIN" -eq 1 ]; then
    "$HIHIVE" setup-harnesses --fresh --plain
  else
    "$HIHIVE" setup-harnesses --fresh
  fi
else
  if [ "$PLAIN" -eq 1 ]; then
    "$HIHIVE" setup-harnesses --offer --plain
  else
    "$HIHIVE" setup-harnesses --offer
  fi
fi
if [ "$FRESH" -eq 0 ] && [ "$RUN_BACKGROUND" -eq 0 ] && "$HIHIVE" health >/dev/null 2>&1; then
  if [ "$SERVICE_STATE_KNOWN" -eq 1 ] && [ "$EXISTING_SERVICE" -eq 0 ]; then
    FOREGROUND_RUNNING=1
  else
    RUNNING_PROCESS=1
  fi
fi

SERVICE_OK=0
if [ "$RUN_BACKGROUND" -eq 1 ]; then
  if "$HIHIVE" service install --port "$PORT"; then
    SERVICE_OK=1
  else
    warn 'hi-hive is installed but NOT running: the background service failed.'
    ROLLBACK_SERVICE_OK=0
    if [ -n "$OLD_COMMIT" ] && [ "$OLD_COMMIT" != "$COMMIT" ]; then
      warn "Restoring the previous package main@$OLD_COMMIT."
      if "$UV" tool install --force --python 3.14 --no-config --no-progress "$REPOSITORY/archive/$OLD_COMMIT.tar.gz"; then
        if [ -n "$BACKUP" ]; then
          [ ! -f "$BACKUP/config.json" ] || atomic_restore_file "$BACKUP/config.json" "$CONFIG_PATH"
          [ ! -f "$BACKUP/state.json" ] || atomic_restore_file "$BACKUP/state.json" "$STATE_PATH"
        fi
        write_record "$OLD_COMMIT"
        if "$HIHIVE" service install --port "$PORT" >/dev/null 2>&1; then
          ROLLBACK_SERVICE_OK=1
          warn 'The previous package and background service were restored.'
        else
          warn 'The previous package was restored, but its service could not be restarted.'
        fi
      else
        warn 'Automatic package rollback failed; user data and the failed service remain stopped.'
      fi
    fi
    if [ "$ROLLBACK_SERVICE_OK" -eq 0 ]; then
      warn 'Run it in a terminal instead:'
      warn "  $DISPLAY_COMMAND serve"
      warn "Then, in another terminal:  $DISPLAY_COMMAND open"
      warn "Try the background service again: $DISPLAY_COMMAND service install"
    fi
    exit 1
  fi
fi

say ''
say 'Result'
if [ "$RUN_BACKGROUND" -eq 1 ] && [ "$SERVICE_OK" -eq 1 ]; then
  say "result        hi-hive is running in the background on port $PORT."
  if [ "$FRESH" -eq 1 ] && [ "$PLAIN" -eq 0 ] && [ -z "${SSH_CONNECTION:-}${SSH_TTY:-}" ] && ask_yes 'Open hi-hive in your browser now? This also logs the browser in.'; then
    "$HIHIVE" open || true
  elif [ -n "${SSH_CONNECTION:-}${SSH_TTY:-}" ]; then
    say "On this server: $DISPLAY_COMMAND open"
    say 'It prints an ssh command for your computer and a private login link to open there.'
    if [ "$FRESH" -eq 1 ] && [ "$OS" = linux ]; then
      say 'It may stop when you log out of this server. To keep it running, see:'
      say 'https://github.com/ertygiq/hi-hive/blob/main/docs/setup.md#background-service'
    fi
  else
    say "Open it later: $DISPLAY_COMMAND open"
  fi
else
  if [ "$SERVICE_SKIPPED" -eq 1 ]; then
    if [ "$INSTALL_SERVICE" -eq 0 ]; then
      say 'result        the new version is installed; the existing background service was not restarted because of --no-service.'
    else
      say 'result        the new version is installed; the existing background service was not restarted because no service manager is available.'
    fi
    say "To load the new version in that service: $DISPLAY_COMMAND service restart"
    say "Or remove the service first:            $DISPLAY_COMMAND service uninstall"
  elif [ "$FOREGROUND_RUNNING" -eq 1 ]; then
    say 'result        the new version is installed; hi-hive is still running the previous version in a terminal.'
    say "Press Ctrl-C in that terminal, then run: $DISPLAY_COMMAND serve"
    say '(This starts the new version.)'
  elif [ "$RUNNING_PROCESS" -eq 1 ]; then
    say 'result        the new version is installed; another hi-hive process is still running the previous version.'
    say "Stop that process, then run: $DISPLAY_COMMAND serve"
    say '(The service manager is unavailable, so the installer cannot identify how that process was started.)'
  else
    say 'result        hi-hive is installed but NOT running yet.'
    say 'To use it:'
  fi
  if [ "$SERVICE_SKIPPED" -eq 1 ] || [ "$FOREGROUND_RUNNING" -eq 1 ] || [ "$RUNNING_PROCESS" -eq 1 ]; then :
  elif [ -n "${SSH_CONNECTION:-}${SSH_TTY:-}" ]; then
    say "  1. On this server:          $DISPLAY_COMMAND serve"
    say '     Keep that SSH session open. Closing it or pressing Ctrl-C stops hi-hive.'
    say "  2. In a second SSH session: $DISPLAY_COMMAND open"
    say '     It prints an ssh command for your computer and a private login link to open there.'
  else
    say "  1. In a terminal:        $DISPLAY_COMMAND serve"
    say '     Keep that terminal open. Closing it or pressing Ctrl-C stops hi-hive.'
    say "  2. In a second terminal: $DISPLAY_COMMAND open"
  fi
  if [ "$SERVICE_SKIPPED" -eq 0 ] && [ "$FOREGROUND_RUNNING" -eq 0 ] && [ "$RUNNING_PROCESS" -eq 0 ] && [ "$SERVICE_POSSIBLE" -eq 1 ]; then
    say "To run it in the background instead: $DISPLAY_COMMAND service install"
    say "First stop '$DISPLAY_COMMAND serve' with Ctrl-C."
  fi
fi
if [ "$PATH_WAS_MISSING" -eq 1 ] && [ "$MODIFY_PATH" -eq 1 ]; then
  say 'Open a new terminal before using the short hihive command.'
fi
say "Installed      hi-hive $PACKAGE_VERSION from main@$COMMIT"
say "Commands       $TOOL_BIN/hihive and $TOOL_BIN/hih"
say "Data kept at   ${HIVE_HOME:-$HOME/.hihive}"

}

main "$@"
