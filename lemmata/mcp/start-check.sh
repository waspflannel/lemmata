#!/usr/bin/env bash
# WSL half of lemmata/start.ps1: check that setup-wsl.sh finished and that ~/.lemmata runs
# the MCP code of the checkout given as $1 (its lemmata/mcp directory, as a WSL path).
# The venv is an editable install, so it follows whichever checkout last ran
# `setup-wsl.sh venv`; when that is another (or a deleted) worktree, repoint it here.
set -uo pipefail

want="$(realpath "${1:?usage: start-check.sh <lemmata/mcp dir>}")"
H="${LEMMATA_HOME:-$HOME/.lemmata}"
# shellcheck source=lemmata_mcp/pins.env
source "$want/lemmata_mcp/pins.env"

if [ ! -x "$H/bin/lemmata-mcp" ] || [ ! -x "$H/venv/bin/python" ]; then
  echo "!! $H is not set up. Inside WSL run: bash $want/setup-wsl.sh"
  exit 1
fi

missing=""
# shellcheck disable=SC2013  # step names are words
for s in $(sed -n '/^ALL=(/,/)/{s/#.*//;s/ALL=(//;s/)//;p}' "$want/setup-wsl.sh"); do
  [ -e "$H/.done/$s" ] || missing="$missing $s"
done
if [ -n "$missing" ]; then
  echo "!! setup steps not finished:$missing"
  echo "   Inside WSL run: bash $want/setup-wsl.sh$missing"
  exit 1
fi
echo "ok   setup-wsl.sh finished in $H"

have="$("$H/venv/bin/python" -c \
  'import lemmata_mcp, os; print(os.path.dirname(os.path.realpath(lemmata_mcp.__file__)))' \
  2>/dev/null)"
if [ "$have" != "$want/lemmata_mcp" ]; then
  echo "fix  venv ran ${have:-a checkout that no longer exists}"
  echo "     repointing it at $want"
  if ! bash "$want/setup-wsl.sh" venv >/dev/null; then
    echo "!! repoint failed. Inside WSL run: bash $want/setup-wsl.sh venv"
    exit 1
  fi
fi
echo "ok   MCP servers run $want"

stale=0
for p in lean verify; do
  if [ "$(cat "$H/$p/lean-toolchain")" != "$LEAN_TOOLCHAIN" ] \
    || ! grep -q "JOSHCLUNE/LeanHammer" "$H/$p/lakefile.toml" \
    || ! grep -q "alerad/leancert" "$H/$p/lakefile.toml" \
    || ! grep -q "rev = \"$MATHLIB_REV\"" "$H/$p/lakefile.toml"; then
    echo "!! $H/$p does not match pins.env (a v1 project?)"
    stale=1
  fi
done
if [ "$stale" = 1 ]; then
  echo "   Inside WSL run: bash $want/setup-wsl.sh lean verify"
  exit 1
fi
echo "ok   Lean projects match pins.env ($LEAN_TOOLCHAIN, Mathlib $MATHLIB_REV)"
