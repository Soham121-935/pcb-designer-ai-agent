#!/usr/bin/env bash
# Reproducible dev setup for the standalone PCB agent — no uv, no Anna, no KiCad required.
#
#   ./scripts/bootstrap.sh            # create .venv, install test/runtime deps
#   PCB_AI_WITH_KICAD=1 ./scripts/bootstrap.sh   # also print what KiCad would add
#
# Everything runs against the source tree (pcbai lives in anna-app/executas/pcb-designer and is
# exposed to Python via [tool.pytest.ini_options].pythonpath in the root pyproject.toml), so edits
# take effect immediately with no reinstall step.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="${PCB_AI_VENV:-$ROOT/.venv}"
PY="${VENV}/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "==> creating venv at $VENV"
  python3 -m venv "$VENV"
fi

echo "==> python: $("$PY" --version 2>&1)"
echo "==> installing runtime + test deps"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q click requests python-dotenv pytest pypdf

echo "==> editable install (so 'pcbai' / 'pcbai-agent' work without PYTHONPATH)"
"$PY" -m pip install -q -e . 2>/dev/null \
  && echo "    installed: $(ls "$VENV/bin" | grep -E '^pcbai' | tr '\n' ' ')" \
  || echo "    skipped editable install (tests still work via pytest pythonpath)"

echo "==> sanity: package imports"
PYTHONPATH="$ROOT/anna-app/executas/pcb-designer" "$PY" - <<'PY'
from pcbai.eda import backend
from pcbai.agent.registry import registry
caps = backend.capabilities()
print(f"    pcbai imported; {len(registry())} tools registered")
print(f"    KiCad pcbnew: {'yes' if caps.pcbnew else 'no (pure-Python path only)'}")
print(f"    kicad-cli    : {caps.kicad_cli or 'no (DRC/Gerber parity reported as skipped)'}")
print(f"    footprint libs: {caps.footprint_libs or 'no'}")
PY

if [[ "${PCB_AI_WITH_KICAD:-0}" == "1" ]]; then
  echo "==> KiCad present: pcbnew + kicad-cli parity checks will run"
  PYTHONPATH="$ROOT/anna-app/executas/pcb-designer" "$PY" -c "
import pcbnew, os
print('    pcbnew', getattr(pcbnew, 'GetBuildVersion', lambda: '?')())
print('    footprints', os.environ.get('PCB_AI_KICAD_FOOTPRINT_DIR', '/usr/share/kicad/footprints'))
" || echo "    (pcbnew import failed — set PCB_AI_KICAD_LIB_PATH to the dist-packages dir)"
fi

cat <<EOF

Done. Next:
  make test          # run the test suite
  make tools         # list every tool, its safety class and maturity
  make demo          # end-to-end: parse → BOM → footprint → pipeline
EOF
