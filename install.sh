#!/bin/sh
# hi-hive per-user installer for macOS and Ubuntu-tested Linux.
# Designed to run as: curl --proto '=https' --tlsv1.2 -fsSL URL | sh
set -eu

umask 077

PROGRAM=hi-hive
PACKAGE_VERSION=0.2.0
REPOSITORY=https://github.com/ertygiq/hi-hive
API_URL=https://api.github.com/repos/ertygiq/hi-hive/git/ref/heads/main
UV_VERSION=0.10.0
BEGIN_MARKER='# >>> hi-hive installer PATH >>>'
END_MARKER='# <<< hi-hive installer PATH <<<'

YES=0
PLAIN=0
MODIFY_PATH=1
INSTALL_SERVICE=1
FORCE=0
UNINSTALL=0
PORT=4567
TMP_DIR=
TTY_DEVICE=

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit "${2:-1}"; }

usage() {
  cat <<'EOF'
Install or update hi-hive from the exact current main commit.

Usage: install.sh [options]
  --yes                 Accept recommended defaults without questions
  --plain               Plain non-interactive output
  --no-modify-path      Do not add uv's tool-bin directory to shell startup
  --no-service          Do not install a background service
  --port PORT           Server port (default: 4567)
  --force, --repair     Reinstall even when the recorded commit is current
  --uninstall           Remove the service, tool, installer PATH block and record
  --help                Show this help

Uninstall preserves ~/.hihive (or HIVE_HOME), including tasks, settings and token.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --yes) YES=1 ;;
    --plain) PLAIN=1 ;;
    --no-modify-path) MODIFY_PATH=0 ;;
    --no-service) INSTALL_SERVICE=0 ;;
    --force|--repair) FORCE=1 ;;
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
[ -d "$HOME" ] || die "HOME is not a directory: $HOME"
[ ! -L "$HOME" ] || die 'HOME must not be a symbolic link'
home_uid=$(ls -dn "$HOME" 2>/dev/null | awk '{print $3}')
[ "$home_uid" = "$(id -u)" ] || die 'HOME must be owned by the current user'
[ -w "$HOME" ] || die 'HOME is not writable'
if [ -n "${HIVE_HOME:-}" ]; then case "$HIVE_HOME" in /*) ;; *) die 'HIVE_HOME must be an absolute path' ;; esac; fi
if [ -n "${XDG_STATE_HOME:-}" ]; then case "$XDG_STATE_HOME" in /*) ;; *) die 'XDG_STATE_HOME must be an absolute path' ;; esac; fi
if [ -n "${XDG_BIN_HOME:-}" ]; then case "$XDG_BIN_HOME" in /*) ;; *) die 'XDG_BIN_HOME must be an absolute path' ;; esac; fi

if [ -t 1 ] && [ -r /dev/tty ] && [ "$PLAIN" -eq 0 ]; then
  TTY_DEVICE=/dev/tty
else
  PLAIN=1
fi
if [ -n "${NO_COLOR:-}" ] || [ "${TERM:-}" = dumb ]; then PLAIN=1; fi

cleanup() {
  code=$?
  trap - 0 HUP INT TERM
  if [ -n "${TMP_DIR:-}" ] && [ -d "$TMP_DIR" ]; then
    case "$TMP_DIR" in "${TMPDIR:-/tmp}"/hi-hive-install.*) rm -rf -- "$TMP_DIR" ;; esac
  fi
  exit "$code"
}
trap cleanup 0 HUP INT TERM

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

state_dir=${XDG_STATE_HOME:-$HOME/.local/state}/hi-hive
record_file=$state_dir/install.json
[ ! -e "$state_dir" ] || [ ! -L "$state_dir" ] || die "install state directory must not be a symbolic link: $state_dir"

write_record() {
  commit=$1
  case "$UV$TOOL_BIN" in *\\*|*\"*) die 'uv or tool-bin path contains a character unsupported by the install record' ;; esac
  case "$UV$TOOL_BIN" in *"
"*) die 'uv or tool-bin path contains a newline' ;; esac
  mkdir -p "$state_dir"
  record_tmp=$state_dir/.install.json.$$
  [ ! -L "$record_file" ] || die "refusing to replace symbolic-link install record: $record_file"
  printf '{"version":"%s","commit":"%s","source":"main","uv":"%s","tool_bin":"%s"}\n' \
    "$PACKAGE_VERSION" "$commit" "$UV" "$TOOL_BIN" > "$record_tmp"
  mv -f "$record_tmp" "$record_file"
}

read_record_commit() {
  [ -f "$record_file" ] || return 0
  sed -n 's/.*"commit"[[:space:]]*:[[:space:]]*"\([0-9A-Fa-f]*\)".*/\1/p' "$record_file" | head -n 1 | tr 'A-F' 'a-f'
}

find_uv() {
  found=$(command -v uv 2>/dev/null || true)
  if [ -z "$found" ] && [ -n "${SHELL:-}" ] && [ -x "$SHELL" ]; then
    marker=HIHIVE_UV_PATH_
    output=$($SHELL -l -i -c "printf '\n${marker}%s\n' \"\$(command -v uv 2>/dev/null || true)\"" </dev/null 2>/dev/null || true)
    found=$(printf '%s\n' "$output" | sed -n "s/^${marker}//p" | tail -n 1)
  fi
  case "$found" in /*) [ -x "$found" ] && printf '%s\n' "$found" ;; esac
}

uv_compatible() {
  output=$($1 --version 2>/dev/null || true)
  version=$(printf '%s\n' "$output" | sed -n 's/^uv \([0-9][0-9.]*\).*/\1/p' | head -n 1)
  [ -n "$version" ] || return 1
  major=$(printf '%s' "$version" | cut -d. -f1)
  minor=$(printf '%s' "$version" | cut -d. -f2)
  [ "$major" -gt 0 ] || [ "$minor" -ge 7 ]
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  else die 'sha256sum or shasum is required to verify uv'
  fi
}

install_uv() {
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
  bin_dir=$(normal_bin_dir)
  case "$bin_dir" in /*) ;; *) die 'uv executable directory must be absolute' ;; esac
  mkdir -p "$bin_dir"
  [ ! -L "$bin_dir/uv" ] || die "refusing to replace symbolic link: $bin_dir/uv"
  cp "$TMP_DIR/uv-$target/uv" "$bin_dir/uv.new"
  chmod 755 "$bin_dir/uv.new"
  mv -f "$bin_dir/uv.new" "$bin_dir/uv"
  if [ -f "$TMP_DIR/uv-$target/uvx" ]; then
    cp "$TMP_DIR/uv-$target/uvx" "$bin_dir/uvx.new"
    chmod 755 "$bin_dir/uvx.new"
    mv -f "$bin_dir/uvx.new" "$bin_dir/uvx"
  else
    ln -f "$bin_dir/uv" "$bin_dir/uvx"
  fi
  UV=$bin_dir/uv
  UV_INSTALLED=1
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

validate_profile_bin() {
  case "$1" in *[!A-Za-z0-9_./\ -]*|'') die "tool-bin path cannot be written safely to a shell profile: $1" ;; esac
  case "$1" in *"
"*) die 'tool-bin path contains a newline' ;; esac
}

validate_path_block_target() {
  profile=$(profile_for_shell) || return 0
  [ ! -L "$profile" ] || die "refusing to edit symbolic-link profile: $profile"
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
  cat "$TMP_DIR/profile.new" > "$profile"
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
  cat "$TMP_DIR/profile.remove" > "$profile"
  say "PATH         removed installer block from $profile"
}

ask_yes() {
  question=$1
  if [ "$YES" -eq 1 ]; then return 0; fi
  if [ "$PLAIN" -eq 1 ]; then return 0; fi
  printf '%s [Y/n] ' "$question" > "$TTY_DEVICE"
  IFS= read -r answer < "$TTY_DEVICE" || answer=
  case "$answer" in n|N|no|NO|No) return 1 ;; *) return 0 ;; esac
}

ask_run_mode() {
  if [ "$YES" -eq 1 ] || [ "$PLAIN" -eq 1 ]; then RUN_BACKGROUND=1; return; fi
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
UV_INSTALLED=0
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
case "$TOOL_BIN" in /*) ;; *) die 'uv tool-bin directory must be absolute' ;; esac
[ ! -L "$TOOL_BIN" ] || die "uv tool-bin directory must not be a symbolic link: $TOOL_BIN"
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
SERVICE_POSSIBLE=0
if service_possible; then SERVICE_POSSIBLE=1; fi
OLD_COMMIT=$(read_record_commit || true)
HIVE_DIR=${HIVE_HOME:-$HOME/.hihive}
CONFIG_PATH=${HIVE_CONFIG_FILE:-$HIVE_DIR/config.json}
case "$CONFIG_PATH" in /*) ;; *) die 'configuration path must be absolute' ;; esac
[ ! -L "$CONFIG_PATH" ] || die "configuration file must not be a symbolic link: $CONFIG_PATH"
FRESH=1
[ -f "$CONFIG_PATH" ] && FRESH=0
if [ "$FRESH" -eq 1 ]; then
  if [ "$INSTALL_SERVICE" -eq 0 ]; then RUN_BACKGROUND=0
  elif [ "$SERVICE_POSSIBLE" -eq 1 ]; then ask_run_mode
  else
    say "Runs         only while '$DISPLAY_COMMAND serve' runs (no background service is available here)"
  fi
else
  if [ "$SERVICE_POSSIBLE" -eq 1 ] && { [ -f "$HOME/Library/LaunchAgents/com.hihive.plist" ] || [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/com.hihive.service" ]; }; then
    RUN_BACKGROUND=1
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
case "$TOOL_BIN" in /*) ;; *) die 'uv returned a non-absolute tool-bin directory' ;; esac
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
    "$CANDIDATE" setup-coding-agents --offer --plain >/dev/null
  fi
  candidate_python=$candidate_tools/hi-hive/bin/python
  [ -x "$candidate_python" ] || die 'candidate installation has no Python interpreter'
  PY_VERSION=$($candidate_python -c 'import platform; print(platform.python_version())')
  case "$PY_VERSION" in 3.14.*) ;; *) die "uv selected Python $PY_VERSION; hi-hive requires Python 3.14.x" ;; esac
  PY_PATH=$($candidate_python -c 'import sys; print(sys.executable)')
  say "Python       selected $PY_VERSION at $PY_PATH (system Python unchanged)"

  if [ "$FRESH" -eq 0 ]; then
    BACKUP=${HIVE_HOME:-$HOME/.hihive}/backups/install-$(date +%Y%m%d-%H%M%S)
    mkdir -p "$BACKUP"
    [ ! -f "$CONFIG_PATH" ] || cp "$CONFIG_PATH" "$BACKUP/config.json"
    [ ! -f "${HIVE_HOME:-$HOME/.hihive}/data/state.json" ] || cp "${HIVE_HOME:-$HOME/.hihive}/data/state.json" "$BACKUP/state.json"
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

if [ "$PATH_WAS_MISSING" -eq 1 ] && [ "$MODIFY_PATH" -eq 1 ]; then add_path_block; fi

say ''
say 'Configuration and readiness'
if [ "$FRESH" -eq 1 ]; then
  if [ "$PLAIN" -eq 1 ]; then
    "$HIHIVE" setup-coding-agents --fresh --plain
  else
    "$HIHIVE" setup-coding-agents --fresh
  fi
else
  if [ "$PLAIN" -eq 1 ]; then
    "$HIHIVE" setup-coding-agents --offer --plain
  else
    "$HIHIVE" setup-coding-agents --offer
  fi
fi
if [ "$FRESH" -eq 0 ] && [ "$RUN_BACKGROUND" -eq 0 ] && "$HIHIVE" health >/dev/null 2>&1; then
  FOREGROUND_RUNNING=1
fi

SERVICE_OK=0
if [ "$RUN_BACKGROUND" -eq 1 ]; then
  if "$HIHIVE" service install --port "$PORT"; then
    SERVICE_OK=1
  else
    warn 'hi-hive is installed but NOT running: the background service failed.'
    if [ -n "$OLD_COMMIT" ] && [ "$OLD_COMMIT" != "$COMMIT" ]; then
      warn "Restoring the previous package main@$OLD_COMMIT."
      if "$UV" tool install --force --python 3.14 --no-config --no-progress "$REPOSITORY/archive/$OLD_COMMIT.tar.gz"; then
        if [ -n "$BACKUP" ]; then
          [ ! -f "$BACKUP/config.json" ] || cp "$BACKUP/config.json" "$CONFIG_PATH"
          if [ -f "$BACKUP/state.json" ]; then
            mkdir -p "${HIVE_HOME:-$HOME/.hihive}/data"
            cp "$BACKUP/state.json" "${HIVE_HOME:-$HOME/.hihive}/data/state.json"
          fi
        fi
        write_record "$OLD_COMMIT"
        "$HIHIVE" service install --port "$PORT" >/dev/null 2>&1 || warn 'The previous package was restored, but its service could not be restarted.'
      else
        warn 'Automatic package rollback failed; user data and the failed service remain stopped.'
      fi
    fi
    warn 'Run it in a terminal instead:'
    warn "  $DISPLAY_COMMAND serve"
    warn "Then, in another terminal:  $DISPLAY_COMMAND open"
    warn "Try the background service again: $DISPLAY_COMMAND service install"
    exit 1
  fi
fi

write_record "$COMMIT"

say ''
say 'Result'
if [ "$RUN_BACKGROUND" -eq 1 ] && [ "$SERVICE_OK" -eq 1 ]; then
  say "result        hi-hive is running in the background on port $PORT."
  if [ "$PLAIN" -eq 0 ] && [ -z "${SSH_CONNECTION:-}${SSH_TTY:-}" ] && ask_yes 'Open hi-hive in your browser now? This also logs the browser in.'; then
    "$HIHIVE" open || true
  elif [ -n "${SSH_CONNECTION:-}${SSH_TTY:-}" ]; then
    say "On this server: $DISPLAY_COMMAND open"
    say 'It prints an ssh command for your computer and a private login link to open there.'
    say 'Logging out stops a systemd user service unless lingering is enabled separately; the installer never enables lingering.'
  else
    say "Open it later: $DISPLAY_COMMAND open"
  fi
else
  if [ "$FOREGROUND_RUNNING" -eq 1 ]; then
    say 'result        the new version is installed; hi-hive is still running the previous version in a terminal.'
    say "Press Ctrl-C in that terminal, then run: $DISPLAY_COMMAND serve"
    say '(This starts the new version.)'
  else
    say 'result        hi-hive is installed but NOT running yet.'
    say 'To use it:'
  fi
  if [ "$FOREGROUND_RUNNING" -eq 1 ]; then :
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
  if [ "$FOREGROUND_RUNNING" -eq 0 ] && [ "$SERVICE_POSSIBLE" -eq 1 ]; then
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
