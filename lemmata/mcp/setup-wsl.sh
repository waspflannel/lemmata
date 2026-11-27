#!/usr/bin/env bash
# Install the lemmata math toolchain inside WSL2 (Ubuntu), all under $LEMMATA_HOME
# (default ~/.lemmata). No sudo: every tool is built or unpacked per-user.
#
#   bash lemmata/mcp/setup-wsl.sh            # every step, skipping finished ones
#   bash lemmata/mcp/setup-wsl.sh sage oeis  # only these steps
#   FORCE=1 bash lemmata/mcp/setup-wsl.sh venv
#
# Needs: git, curl, gcc, make, python3 >= 3.12 and uv (https://docs.astral.sh/uv/).
# Disk: about 25 GB (two Mathlib builds, Sage). RAM: local Loogle indexing peaks near 13 GB.
set -euo pipefail

MCP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$MCP_DIR/lemmata_mcp"
# shellcheck source=lemmata_mcp/pins.env
source "$PKG_DIR/pins.env"

H="${LEMMATA_HOME:-$HOME/.lemmata}"
export PATH="$H/bin:$H/go/bin:$HOME/.elan/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
mkdir -p "$H/bin" "$H/src" "$H/state" "$H/index" "$H/.done"

log() { printf '\n== %s\n' "$*"; }

# Clone $1 into $2 at revision $3 (tag or commit), shallow.
clone_at() {
  local url=$1 dir=$2 rev=$3
  if [ ! -d "$dir/.git" ]; then git init -q "$dir"; git -C "$dir" remote add origin "$url"; fi
  git -C "$dir" fetch -q --depth 1 origin "$rev"
  git -C "$dir" checkout -q FETCH_HEAD
}

# Lake project requiring the pinned Mathlib (and REPL when $3 = repl).
write_lake_project() {
  local dir=$1 name=$2 with_repl=${3:-}
  mkdir -p "$dir"
  echo "$LEAN_TOOLCHAIN" > "$dir/lean-toolchain"
  cat > "$dir/lakefile.toml" <<EOF
name = "$name"
version = "0.1.0"
defaultTargets = []

[[require]]
name = "mathlib"
git = "https://github.com/leanprover-community/mathlib4"
rev = "$MATHLIB_REV"
EOF
  if [ "$with_repl" = repl ]; then
    cat >> "$dir/lakefile.toml" <<EOF

[[require]]
name = "REPL"
git = "https://github.com/leanprover-community/repl"
rev = "$REPL_REV"
EOF
  fi
}

step_elan() {
  if ! command -v elan >/dev/null; then
    curl -sSfL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
      | sh -s -- -y --default-toolchain none --no-modify-path
  fi
  elan toolchain install "$LEAN_TOOLCHAIN"
}

# Work project: the warm REPL, lean-lsp-mcp and scratch files live here.
step_lean() {
  write_lake_project "$H/lean" LemmataWork repl
  mkdir -p "$H/lean/Scratch"
  (cd "$H/lean" && lake update && lake exe cache get && lake build REPL repl)
}

# Verification project: a second, read-only Mathlib that worker code never touches.
# Comparator submissions symlink its .lake/packages, so nothing is rebuilt per check.
step_verify() {
  if [ -d "$H/verify/.lake/packages" ]; then chmod -R u+w "$H/verify/.lake/packages"; fi
  write_lake_project "$H/verify" LemmataVerify
  (cd "$H/verify" && lake update && lake exe cache get && lake build Mathlib)
  chmod -R a-w "$H/verify/.lake/packages"
}

step_go() {
  [ -x "$H/go/bin/go" ] && return
  curl -sSfL "https://go.dev/dl/$GO_VERSION.linux-amd64.tar.gz" | tar -xz -C "$H"
}

step_landrun() {
  clone_at https://github.com/Zouuup/landrun "$H/src/landrun" "$LANDRUN_REV"
  (cd "$H/src/landrun" && go build -o "$H/bin/landrun" ./cmd/landrun)
  "$H/bin/landrun" --rox / -- /bin/true
}

step_comparator() {
  clone_at https://github.com/leanprover/comparator "$H/src/comparator" "$COMPARATOR_REV"
  (cd "$H/src/comparator" && lake build lean4export comparator)
  ln -sf "$H/src/comparator/.lake/build/bin/comparator" "$H/bin/comparator"
  ln -sf "$H/src/comparator/.lake/packages/lean4export/.lake/build/bin/lean4export" "$H/bin/lean4export"
}

step_sat() {
  clone_at https://github.com/arminbiere/cadical "$H/src/cadical" "$CADICAL_REV"
  (cd "$H/src/cadical" && ./configure >/dev/null && make -j"$(nproc)" >/dev/null)
  ln -sf "$H/src/cadical/build/cadical" "$H/bin/cadical"
  clone_at https://github.com/marijnheule/drat-trim "$H/src/drat-trim" "$DRAT_TRIM_REV"
  (cd "$H/src/drat-trim" && make >/dev/null)
  ln -sf "$H/src/drat-trim/drat-trim" "$H/bin/drat-trim"
}

step_nauty() {
  local d="$H/src/nauty$NAUTY_VERSION"
  if [ ! -d "$d" ]; then
    curl -sSfL "https://pallini.di.uniroma1.it/nauty$NAUTY_VERSION.tar.gz" | tar -xz -C "$H/src"
  fi
  (cd "$d" && ./configure >/dev/null && make -j"$(nproc)" geng >/dev/null)
  ln -sf "$d/geng" "$H/bin/geng"
}

step_venv() {
  [ -x "$H/venv/bin/python" ] || uv venv -q -p 3.12 "$H/venv"
  VIRTUAL_ENV="$H/venv" uv pip install -q -e "$MCP_DIR"
  # Launcher for MCP clients: fixed PATH, no login shell (its output would corrupt stdio).
  cat > "$H/bin/lemmata-mcp" <<EOF
#!/bin/sh
export PATH="$H/bin:$HOME/.elan/bin:/usr/local/bin:/usr/bin:/bin"
export LEMMATA_HOME="$H"
exec "$H/venv/bin/python" -m lemmata_mcp "\$@"
EOF
  chmod +x "$H/bin/lemmata-mcp"
}

step_sage() {
  local mf="$H/miniforge3"
  if [ ! -x "$mf/bin/conda" ]; then
    curl -sSfL -o /tmp/miniforge.sh \
      https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
    bash /tmp/miniforge.sh -b -p "$mf"
    rm /tmp/miniforge.sh
  fi
  [ -x "$mf/envs/sage/bin/sage" ] || "$mf/bin/mamba" create -y -q -n sage -c conda-forge "sage=$SAGE_VERSION"
  # Sage shells out to gap, maxima, ... from its env's bin, so that must be on PATH.
  PATH="$mf/envs/sage/bin:$PATH" "$mf/envs/sage/bin/python" -c 'from sage.all import QQ; print("sage ok", QQ)'
}

step_formal_conjectures() {
  clone_at https://github.com/google-deepmind/formal-conjectures "$H/formal-conjectures" "$FORMAL_CONJECTURES_REV"
}

step_oeis() {
  mkdir -p "$H/oeis"
  curl -sSfL -o "$H/oeis/stripped.gz" https://oeis.org/stripped.gz
  curl -sSfL -o "$H/oeis/names.gz" https://oeis.org/names.gz
}

# Declaration index over the pinned Mathlib (full types, modules, revision).
step_index() {
  (cd "$H/lean" && lake env lean --run "$PKG_DIR/DumpDecls.lean" Mathlib > "$H/index/decls.jsonl")
  "$H/bin/lemmata-mcp" build-index "$H/index/decls.jsonl"
}

ALL=(elan lean verify go landrun comparator sat nauty venv sage formal_conjectures oeis index)
STEPS=("$@")
[ ${#STEPS[@]} -eq 0 ] && STEPS=("${ALL[@]}")
for s in "${STEPS[@]}"; do
  if [ -e "$H/.done/$s" ] && [ -z "${FORCE:-}" ] && [ $# -eq 0 ]; then
    echo "skip $s (done)"; continue
  fi
  log "$s"
  "step_$s"
  touch "$H/.done/$s"
done
log "lemmata toolchain ready in $H"
