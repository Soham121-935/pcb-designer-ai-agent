# PCB Design AI Agent

An AI agent for **real KiCad PCB engineering work**: inspect a board, plan a change, execute it
through a typed tool layer, re-parse the result, validate it, and report what actually happened.

This repository is being repaired in phases. The current state is **Phase 2** — the existing system is
reproducible, its tools report failures honestly, and a repo-level agent tool surface exists.
The board *model* (parser), *controlled modification* and *DRC* are Phase 4-6 and are **not** implemented
yet; where a capability is missing the tool layer says so instead of improvising.

Read [`docs/AUDIT.md`](docs/AUDIT.md) for the full audit, the defect register and the locked decisions.

---

## Quick start

```bash
./scripts/bootstrap.sh        # .venv + deps. No uv, no KiCad, no API keys required.
make test                     # 63 tests, all runnable offline and without KiCad
make tools                    # every tool + its safety class + maturity
make backend                  # which EDA backends are usable here
make demo                     # requirements → BOM → footprint → pipeline (reports mode)
make rpc                      # drive the Anna JSON-RPC plugin over stdio
```

Invoke a single tool (this is exactly what the agent does):

```bash
export PYTHONPATH=anna-app/executas/pcb-designer

.venv/bin/python -m pcbai.agent.cli call capabilities

.venv/bin/python -m pcbai.agent.cli call generate_footprint --confirm \
  --arg footprint_type=qfp \
  --json '{"params":{"name":"LQFP-144_TQFP_20x20_P0.5","pins":144,"pitch":0.5,
          "body_l":20,"body_w":20,"pad_l":1.2,"pad_w":0.25}}'

.venv/bin/python -m pcbai.agent.cli call inspect_pcb_file \
  --json '{"path":"anna-app/executas/pcb-designer/pcbai/steps/template_project/board.kicad_pcb"}'
```

`call` exits non-zero whenever a tool returns `success: false`, so it is safe to script.

### System requirements

| Requirement | Needed for | Notes |
|---|---|---|
| Python ≥ 3.10, `click`, `requests` | everything in the repo | installed by `bootstrap.sh` |
| `pypdf` **or** `pdfminer.six` | `extract_package_from_pdf` | datasheet text extraction |
| **KiCad ≥ 8 with python bindings + `kicad-cli`** | `route_pcb`, native board authoring, DRC/Gerber parity | **optional**; without it those tools return `success:false / reason:"backend-unavailable"` instead of silently "succeeding" |
| `skidl` | `pcbai synthesize` (CLI) | optional, not installed by default |
| `java` + `freerouting.jar` | Freerouting pass after `.dsn` export | optional |

KiCad is detected, never assumed. Override discovery with `PCB_AI_KICAD_LIB_PATH` (directory containing
`pcbnew`), `PCB_AI_KICAD_CLI`, `PCB_AI_KICAD_FOOTPRINT_DIR`, `PCB_AI_KICAD_PYTHON`.

---

## Architecture

```
 natural-language PCB task
          │
          ▼
   ┌──────────────────────────────────────────────────────────┐
   │  THE AGENT (me, Claude Code, in this repo)               │
   │  inspect → plan → mutate → re-parse → validate → report │
   └──────────────────────────────────────────────────────────┘
          │  typed tool calls (read-only | mutating+confirm)
          ▼
   pcbai/agent/registry.py ── dispatch() ── policy gates (§ safety below)
          │
          ▼
   pcbai/agent/tools.py  ──►  pcbai/steps/*      (pure-Python: footprints, BOM, extraction)
          │                   pcbai/core/filesafe (backup → atomic write → verify → rollback)
          │                   pcbai/eda/backend   (KiCad capability probe)
          ▼
   pcbai/steps/pcb_router.py ──► KiCad pcbnew (child process) ── optional Freerouting
   pcbai/steps/gerber_exporter.py ──► kicad-cli

   Anna OS path (secondary, unchanged contract): anna-app/bundle/app.js
      → Anna host agent → JSON-RPC over stdio → anna-app/executas/pcb-designer/plugin.py
        (7 tools; reverse-RPC `sampling/createMessage` borrows Anna's LLM)
```

* **`pcbai` is one package, not two.** It physically lives in
  `anna-app/executas/pcb-designer/pcbai/` so the Anna plugin keeps its layout; the root
  `pyproject.toml` maps `package-dir = {"" = "anna-app/executas/pcb-designer"}` and
  `[tool.pytest.ini_options] pythonpath` makes it importable from the repo root with no install step.
* **There is no LLM tool-calling loop in the repo yet** (Phase 3). The `LLMProvider` layer is
  text-in/text-out; only two steps use it (`parse_requirements`, the pipeline report). Until Phase 3,
  *the agent* is the planner and calls the tools directly — which is why the tool layer, not the
  prompt, carries the safety guarantees.

### Target CAD format

KiCad S-expressions, **emitting KiCad 10** (`(version 20260206)`, `(generator_version "10.0")`) and
reading KiCad 7→10 (decided in AUDIT §11/Q3). Known deviation: the footprint writers still emit the
KiCad 5/6 `module`/`fp_text` dialect and say so in `warnings` — upgrading them is part of the
Phase 4/5 writer work, not silently pretending they are KiCad 10.

Manufacturing-target defaults are **not** fixed yet (AUDIT §11/Q4). When they are, they land as an
editable per-project rules file, not constants.

---

## Tool surface (as implemented, not as advertised)

Legend: **usable** now · **placeholder** returns data that is not engineering-grade ·
**Phase N** registered but refuses with `reason:"not-implemented"`.

| Tool | Safety | State | What it really does |
|---|---|---|---|
| `capabilities` | read-only | usable | pcbnew / kicad-cli / footprint libs / Freerouting availability + notes |
| `list_project_files` | read-only | usable | finds `.kicad_pcb/.kicad_sch/.kicad_pro/.kicad_mod/bom` under a project dir |
| `read_project_file` | read-only | usable | size-capped read, refuses paths escaping the project root |
| `inspect_pcb_file` | read-only | usable | depth-aware top-level counts (footprints, pads, segments, vias, zones, Edge.Cuts, net table, net classes) + structural warnings. **Counts only — no geometry yet** |
| `extract_package_from_pdf` | read-only | usable (weak) | regex/keyword extraction from datasheet text; unresolved fields are listed in `warnings` |
| `generate_bom` | read-only | usable (limited) | maps keywords to a 12-part built-in catalog; unmatched keywords → `*-UNKNOWN` + warning. **No vendor/stock lookup** |
| `parse_requirements` | read-only | usable | LLM → JSON, deterministic keyword fallback (reported as `source:"keyword-fallback"`) |
| `synthesize_netlist` | read-only | **placeholder** | GND/VCC ref groups, *no REF-PIN connectivity* → a router cannot use it (Phase 4) |
| `generate_footprint` | mutating | usable | 9 package types; writes `<project>/footprints/*.kicad_mod`, **re-reads and compares**, validates paren balance + pad count |
| `create_backup` | mutating | usable | explicit `.bak` + append-only `.history/` snapshot |
| `route_pcb` | mutating | usable only with KiCad | loads `footprints/`, assigns pads→nets, heuristic placement, `.dsn` export, optional Freerouting; honest `ok/reason` |
| `full_pipeline` | mutating | usable | requirements → BOM → board. `data.mode` = `generated` \| `template`; template results are flagged `template_only:true` with warnings |
| `inspect_project`, `inspect_pcb`, `inspect_schematic`, `list_components`, `get_component`, `get_nets`, `get_board_outline`, `get_design_rules` | read-only | **Phase 4** | refused: `not-implemented` |
| `add_component`, `remove_component`, `move_component`, `rotate_component`, `set_footprint`, `add_track`, `add_via`, `route_net`, `set_board_outline`, `save_project` | mutating | **Phase 5** | refused: `not-implemented` |
| `run_drc`, `run_erc`, `validate_project`, BOM↔schematic consistency, fab checks | read-only | **Phase 6** | refused: `not-implemented` |

`pcbai-agent tools` prints this live; `pcbai-agent describe <tool>` shows parameters.

### Safety model (enforced in code, `pcbai/agent/registry.py`)

1. Every tool is classified **read-only** or **mutating**.
2. Mutating tools are refused unless the caller passes `confirm=True` (`reason:"needs-confirmation"`)
   and are blocked entirely by `--no-mutating` (`reason:"needs-mutation-approval"`).
3. Writes go through `pcbai/core/filesafe.py`: backup → temp file + `fsync` + `os.replace` →
   re-read/validate → **rollback on failure**; `.history/` keeps one entry per overwrite;
   non-PCB extensions and `*.bak`/`.history` paths are rejected; a transaction cannot escape its root.
4. `success:true` means the **post-condition was verified**, not that a file was written.
5. Missing capabilities are reported as `skipped`/`not-implemented` with a reason — never as a pass.

---

## Workflow the agent must follow

```
inspect project → inspect board → plan → (backup) → mutate → re-parse → validate → DRC/ERC (if KiCad)
                                                          → inspect result → fix → report what changed
```
Ambiguous engineering requests ("make the power section better") must produce an inspection report
plus a clarifying question, not a guessed edit. Deterministic requests ("move R12 5 mm left") execute.

---

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `PCB_AI_LLM_PROVIDER` | `openai` (plugin: host sampling when unset/`anna`) | `openai`\|`gemini`\|`claude`/`anthropic`\|`ollama`\|`lmstudio`\|`dummy` |
| `PCB_AI_MODEL` | provider-specific | model id for any provider |
| `OPENAI_API_KEY` / `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` | — | provider keys |
| `LMSTUDIO_URL` / `OLLAMA_URL` / `OLLAMA_HOST` | `http://localhost:1234` / `:11434` | local endpoints |
| `PCB_AI_MAX_TOKENS`, `PCB_AI_TEMPERATURE` | call-site value | override all LLM calls |
| `PCB_AI_SAMPLE_TIMEOUT` | `60` | seconds to wait for Anna reverse-RPC sampling |
| `PCB_AI_WORKDIR` | `<repo>/build` | generated output root |
| `PCB_AI_PROJECT` | `$PCB_AI_WORKDIR/demo-project` | project dir the tools read/write |
| `PCB_AI_REPO_ROOT` | auto-detected | override repo-root discovery |
| `PCB_AI_ALLOW_TEMPLATE_COPY` | `1` | permit the ESP32-C3 reference-template fallback (always reported as `mode:"template"`) |
| `PCB_AI_EXPERIMENTAL_ROUTER` | `0` | enable the naive Manhattan router (**produces shorts**; not for real work) |
| `PCB_AI_KICAD_LIB_PATH` | distro paths | dirs to search for `pcbnew` |
| `PCB_AI_KICAD_PYTHON` | `python3` | interpreter that can import `pcbnew` |
| `PCB_AI_KICAD_CLI` (legacy `KICAD_CLI`) | `kicad-cli` | for Gerber/parity checks |
| `PCB_AI_KICAD_FOOTPRINT_DIR`, `PCB_AI_KICAD_SYMBOL_DIR` | `/usr/share/kicad/...` | KiCad libraries |
| `PCB_AI_FREEROUTING_JAR`, `PCB_AI_JAVA` | — | DSN→SES routing pass |
| `PCB_AI_LOG` | `INFO` | log level (stderr only) |
| `OCTOPART_API_KEY` | — | **declared in `executa.json` but not implemented**; nothing reads it |

---

## Testing

```bash
make test                                   # 63 tests, no KiCad / no network
cd anna-app/executas/pcb-designer && PCB_AI_LLM_PROVIDER=dummy python3 test_harness.py --smoke
python3 -m pytest tests/test_rpc_protocol.py -q      # JSON-RPC contract + honesty invariants
```

CI (`.github/workflows/ci.yml`) runs two lanes: **core** (must pass with no KiCad) and
**kicad-parity** (`kicad/kicad:latest` container) which exercises the same assertions where a real
`pcbnew`/`kicad-cli` exists. Test suite layout: parser/tool layer (`test_tool_layer.py`),
file safety (`test_filesafe.py`), pipeline honesty (`test_design_compiler.py`), router contract
(`test_pcb_router.py`), RPC protocol (`test_rpc_protocol.py`), plus the inherited footprint/datasheet
tests in `tests/test_*.py`.

---

## Known limitations (current, real)

1. **No board model.** Nothing can read a `.kicad_pcb`/`.kicad_sch` into geometry — `inspect_pcb_file`
   is a structural sanity check only. "Move C12", "route this net", "run DRC" are impossible today.
2. **Netlists have no pad-level connectivity** (`schematic_synthesizer` is a placeholder), so routing
   has no input even when KiCad is installed.
3. **No DRC/ERC/fab checks** in the agent path; `kicad-cli` parity checks are not wired yet.
4. **`full_pipeline` does not design.** It returns a hardcoded ESP32-C3 reference board unless KiCad's
   writers run (which are themselves hardcoded to that board). Now labelled `mode:"template"`.
5. **Footprint dialect** is KiCad 5/6 `module`/`fp_text` — loads in modern KiCad after an upgrade
   pass, but is not KiCad 10 native. Flagged in every tool warning.
6. **`bom_generator` catalog is 12 hardcoded parts**; no vendor, price, stock, or lifecycle data.
7. **Datasheet extraction is weak**: on the bundled 2 MB LM5164 PDF it recovers only `soic`/`8 pins`
   and leaves pitch/body/pad dimensions null.
8. **The Anna web UI can still fabricate PCB files in the browser** when tool output lacks fields
   (`anna-app/bundle/app.js`) — scheduled for removal so the UI only shows validated artifacts.
9. **No LLM tool loop in-repo** (Phase 3); `plugin.py`'s reverse-RPC sampling needs a host that answers
   `sampling/createMessage` (the local `test_harness.py` does).
10. The bundled **`pcb_project.zip` is stale** relative to the template files inside
    `template_project/` (regenerate before shipping it).

## Contributing / roadmap

Phase 3 agent loop → Phase 4 KiCad parser + board model → Phase 5 validated mutation + router →
Phase 6 DRC/ERC/fab checks → Phase 7 orchestration → Phase 8 test hardening → Phase 9 SV-16 board.
Full details and commit plan: [`docs/AUDIT.md`](docs/AUDIT.md) §9.

## License

Dual-licensed: non-commercial open source under `LICENSE`; commercial/enterprise use requires prior
written authorisation from the author (contact in `LICENSE`). This is inherited from the upstream
project — confirm the licence fits your intended use before relying on it.
