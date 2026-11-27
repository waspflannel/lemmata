#!/usr/bin/env bash
# Install the lemmata math toolchain inside WSL2 (Ubuntu), all under $LEMMATA_HOME
# (default ~/.lemmata). No sudo: every tool is built or unpacked per-user.
#
#   bash lemmata/mcp/setup-wsl.sh            # every step, skipping finished ones
#   bash lemmata/mcp/setup-wsl.sh sage oeis  # only these steps
#   FORCE=1 bash lemmata/mcp/setup-wsl.sh venv
#
# Needs: git, curl, gcc, make, python3 >= 3.12 and uv (https://docs.astral.sh/uv/).
# Disk: about 25 GB (two Mathlib builds, Sage) plus the v2 additions (LeanHammer and LeanCert
# in both Lean projects, solver env, Macaulay2, Kimina; see lemmata/README.md).
# RAM: local Loogle indexing peaks near 13 GB.
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

# Lake project requiring the pinned Mathlib (and REPL when $3 = repl). LeanHammer and LeanCert
# come first, as LeanHammer's README asks, so Mathlib's shared dependencies keep one revision
# (batteries, aesop and Qq are identical at v4.35.0) and the Mathlib cache stays valid.
write_lake_project() {
  local dir=$1 name=$2 with_repl=${3:-}
  mkdir -p "$dir"
  echo "$LEAN_TOOLCHAIN" > "$dir/lean-toolchain"
  cat > "$dir/lakefile.toml" <<EOF
name = "$name"
version = "0.1.0"
defaultTargets = []

[[require]]
name = "Hammer"
git = "https://github.com/JOSHCLUNE/LeanHammer"
rev = "$HAMMER_REV"

[[require]]
name = "LeanCert"
git = "https://github.com/alerad/leancert"
rev = "$LEANCERT_REV"

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

# lean-auto (Zipperposition) and lean-cvc5 run `unzip -d DIR FILE` while building. Without a
# system unzip, use a per-user one that keeps the executable bits Python's zipfile drops.
ensure_unzip() {
  command -v unzip >/dev/null && return
  cat > "$H/bin/unzip" <<'EOF'
#!/usr/bin/env python3
"""Minimal `unzip [-o] [-q] -d DIR FILE` for Lake build scripts."""
import os, sys, zipfile
args = [a for a in sys.argv[1:] if a not in ("-o", "-q")]
if len(args) != 3 or args[0] != "-d":
    sys.exit("usage: unzip -d DIR FILE")
with zipfile.ZipFile(args[2]) as archive:
    for info in archive.infolist():
        path = archive.extract(info, args[1])
        mode = info.external_attr >> 16
        if mode and not info.is_dir():
            os.chmod(path, mode & 0o7777)
EOF
  chmod +x "$H/bin/unzip"
}

# Work project: the warm REPL, lean-lsp-mcp and scratch files live here.
step_lean() {
  ensure_unzip
  write_lake_project "$H/lean" LemmataWork repl
  mkdir -p "$H/lean/Scratch"
  (cd "$H/lean" && lake update && lake exe cache get && lake build REPL repl $LEAN_EXTRA_TARGETS)
}

# Verification project: a second, read-only Mathlib that worker code never touches.
# Comparator submissions symlink its .lake/packages, so nothing is rebuilt per check.
step_verify() {
  ensure_unzip
  if [ -d "$H/verify/.lake/packages" ]; then chmod -R u+w "$H/verify/.lake/packages"; fi
  write_lake_project "$H/verify" LemmataVerify
  (cd "$H/verify" && lake update && lake exe cache get && lake build Mathlib $LEAN_EXTRA_TARGETS)
  # Precompiled packages (LeanHammer, Duper, ...) need native objects that Lake builds only
  # for a dependent module. Build one importing what every solution imports, the same way
  # comparator builds Solution, while the packages are still writable.
  local probe="$H/state/verify-probe"
  rm -rf "$probe" && mkdir -p "$probe/.lake"
  ln -s "$H/verify/.lake/packages" "$probe/.lake/packages"
  cp "$H/verify/lean-toolchain" "$H/verify/lake-manifest.json" "$probe/"
  { cat "$H/verify/lakefile.toml"; printf '\n[[lean_lib]]\nname = "Probe"\n'; } > "$probe/lakefile.toml"
  printf 'import %s\n' Mathlib $LEAN_EXTRA_MODULES > "$probe/Probe.lean"
  (cd "$probe" && lake build Probe)
  rm -rf "$probe"
  chmod -R a-w "$H/verify/.lake/packages"
}

# Modules imported next to Mathlib by the REPL header and every solution
# (lemmata_mcp.common.EXTRA_IMPORTS).
LEAN_EXTRA_MODULES="Hammer LeanCert.Tactic"
LEAN_EXTRA_TARGETS="$(printf '+%s ' $LEAN_EXTRA_MODULES)"

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

# Mace4 (finite model finder) from LADR.
step_ladr() {
  clone_at https://github.com/laitep/ladr "$H/src/ladr" "$LADR_REV"
  # Only mace4: `make all` also links prover9, which fails on current toolchains (-lm order).
  (cd "$H/src/ladr" && make -C ladr lib >/dev/null && make -C mace4.src mace4 >/dev/null)
  ln -sf "$H/src/ladr/mace4.src/mace4" "$H/bin/mace4"
}

# CSDP (SDP solver for sum-of-squares certificates), linked against the Sage env's
# LAPACK/BLAS (a stock Ubuntu has none, and there is no sudo). Needs the sage step.
step_csdp() {
  clone_at https://github.com/coin-or/Csdp "$H/src/csdp" "$CSDP_REV"
  local cflags="-O2 -fopenmp -ansi -DBIT64 -DUSEOPENMP -DSETNUMTHREADS -DUSESIGTERM -DUSEGETTIME"
  local lib="$H/miniforge3/envs/sage/lib"
  (cd "$H/src/csdp" && make -C lib clean libsdp.a CFLAGS="$cflags -I../include" >/dev/null \
    && rm -f solver/csdp && make -C solver csdp CFLAGS="$cflags -I../include" \
      LIBS="-L../lib -lsdp -L$lib -Wl,-rpath,$lib -llapack -lblas -lm" >/dev/null)
  ln -sf "$H/src/csdp/solver/csdp" "$H/bin/csdp"
}

# SoPlex (exact rational LP) in its own conda env; GAP (with SmallGrp) and PARI/GP ship in Sage's.
step_soplex() {
  local mf="$H/miniforge3"
  [ -x "$mf/envs/lp/bin/soplex" ] \
    || "$mf/bin/mamba" create -y -q -n lp -c conda-forge "soplex=$SOPLEX_VERSION"
  ln -sf "$mf/envs/lp/bin/soplex" "$H/bin/soplex"
  ln -sf "$mf/envs/sage/bin/gap" "$H/bin/gap"
  ln -sf "$mf/envs/sage/bin/gp" "$H/bin/gp"
}

# Macaulay2 from the Ubuntu archive without sudo: download the .debs that are not installed
# and unpack them under $H/m2; the launcher points LD_LIBRARY_PATH at the unpacked libraries.
step_macaulay2() {
  local d="$H/m2" pkgs=() p
  mkdir -p "$d/debs"
  for p in $(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts \
      --no-breaks --no-replaces --no-enhances macaulay2 | grep -v '^ ' | grep -v '[<>]' | sort -u); do
    [ "$p" = macaulay2 ] && continue
    dpkg -s "$p" >/dev/null 2>&1 && continue
    case $p in fonts-*|libjs-*|node-*|*debconf*|*-installer*|libtextwrap*|mime-support|gdbmtool) ;;
      *) pkgs+=("$p") ;;
    esac
  done
  (cd "$d/debs" && apt-get download "macaulay2=$MACAULAY2_VERSION" "${pkgs[@]}")
  for p in "$d"/debs/*.deb; do dpkg-deb -x "$p" "$d/root"; done
  # M2 looks for Factory's GF tables at the absolute /usr/share path it was built with.
  local factor="$d/root/usr/share/Macaulay2/Core/factor.m2" gf="$d/root/usr/share/singular/factory/"
  grep -q "$gf" "$factor" || sed -i \
    "s|^i := position(gfdirs,|gfdirs = append(gfdirs, \"$gf\");\ni := position(gfdirs,|" "$factor"
  cat > "$H/bin/M2" <<EOF
#!/bin/sh
export LD_LIBRARY_PATH="$d/root/usr/lib/x86_64-linux-gnu:$d/root/usr/lib"
exec "$d/root/usr/bin/M2" "\$@"
EOF
  chmod +x "$H/bin/M2"
  echo 'print(1+1); exit 0' | "$H/bin/M2" --script /dev/stdin
}

step_pi_base() {
  clone_at https://github.com/pi-base/data "$H/pi-base" "$PI_BASE_REV"
}

# Kimina Lean Server (opt-in batch checking) in its own venv. lean_server points it at the
# work project's REPL and Mathlib, so it never installs another Lean.
step_kimina() {
  clone_at https://github.com/project-numina/kimina-lean-server "$H/src/kimina" "$KIMINA_REV"
  [ -x "$H/kimina-venv/bin/python" ] || uv venv -q -p 3.12 "$H/kimina-venv"
  # The server imports a generated Prisma client even without a database. prisma's own
  # nodeenv download breaks on the current Node index, so give it nodejs-bin instead.
  (cd "$H/src/kimina" \
    && VIRTUAL_ENV="$H/kimina-venv" uv pip install -q -r requirements.txt nodejs-bin==18.4.0a4 \
    && VIRTUAL_ENV="$H/kimina-venv" uv pip install -q --no-deps . \
    && PATH="$H/kimina-venv/bin:$PATH" prisma generate >/dev/null)
}

# Runtime switches read by the servers (lemmata_mcp.common.OPTIONS). Never overwritten.
step_options() {
  [ -e "$H/options.env" ] && return
  cp "$PKG_DIR/options.env" "$H/options.env"
}

# SnapPy inside the Sage env: its verified (interval) routines need Sage.
step_snappy() {
  local py="$H/miniforge3/envs/sage/bin/python"
  "$py" -m pip install -q "snappy==$SNAPPY_VERSION"
  PATH="$H/miniforge3/envs/sage/bin:$PATH" "$py" -c \
    'import sage.all, snappy; print("snappy ok", snappy.Manifold("m004").volume(verified=True))'
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

ALL=(elan lean verify go landrun comparator sat nauty venv sage formal_conjectures oeis index
  ladr csdp soplex macaulay2 snappy pi_base kimina options)  # csdp, soplex, snappy need sage
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
