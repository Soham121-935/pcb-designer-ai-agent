# Phase 1 — Repository Audit

Audited at: `247aa7f` (single-commit history: "docs: added KiCad system requirements and local testing instructions")
Auditor: agent, 2026-09-27.
Method: full source read + live execution of the test suite, the RPC harness and the tools. Every claim below is
either backed by a file:line reference or by a command that was actually run in this sandbox.

---

## 1. What this project actually is

A **KiCad-oriented PCB generation pipeline** packaged as a plugin ("Executa") for the **Anna OS** app platform, plus a
thin browser dashboard. It is **not** an agent that operates on an existing PCB project.

The pipeline is *generative-only* and *template-based*: natural language → keyword list → fixed parts catalog →
"toy" netlist → (KiCad file writers, largely bypassed) → **a single pre-generated ESP32-C3 board copied from a
template directory** → zip download.

### Repository layout (104 tracked files, ~3.1k lines of Python)

```
pcb-designer-ai-agent/
├── README.md                     # user-facing docs (partly aspirational — see §6 D1)
├── SECURITY.md                   # unfilled GitHub template
├── LICENSE                       # custom non-commercial licence
├── gemini_test.py                # ad-hoc demo (hardcoded author path — broken)
├── gemini_e2e.py                 # ad-hoc demo (hardcoded author path — broken)
├── test_rpc.py                   # spawns plugin over stdio with `uv` (uv not installed)
├── plugin.erc / plugin.net / plugin.log / plugin_sklib.py   # SKiDL debug leftovers, committed by accident
├── .coverage                     # SQLite coverage DB, committed by accident
├── tests/                        # 5 pytest files, footprint + datasheet-extractor only
├── reef_harness/                 # "Reef" LLM-prompt-evolution harness config (external tool, unused here)
└── anna-app/
    ├── app.json                  # Anna app manifest
    ├── bundle/                   # static frontend (index.html, app.js, styles.css, jszip)
    └── executas/pcb-designer/    # ★ the actual product
        ├── plugin.py             # 625 L — JSON-RPC 2.0 stdio server + 7 tool wrappers + manifest
        ├── test_harness.py       # 419 L — Anna runtime simulator, REPL, smoke tests
        ├── e2e_demo.py           # manual 5-step demo
        ├── pyproject.toml        # deps + `pcbai` console script + pytest config
        ├── uv.lock               # 85 packages (incl. skidl, kinet2pcb, hierplace, simp-sexp — all UNUSED in code)
        ├── pcb-designer-plugin   # 0 bytes   ┐ PyInstaller artifacts committed empty
        ├── pcb-designer-plugin.exe # 0 bytes ┘ (dist/*.tar.gz referenced by executa.json does not exist)
        ├── pcbai/
        │   ├── core/config.py    # env-driven settings (tiny)
        │   ├── core/logger.py    # stdout logger (pollutes JSON-RPC)
        │   ├── llm/provider.py   # 222 L — 6 providers, text-in/text-out only
        │   ├── pipeline/cli.py   # click CLI: design|bom|footprint|extract_package|synthesize
        │   └── steps/            # 20 modules (see §3)
        └── pcbai/steps/template_project/   # the fixed ESP32-C3 board that "full_pipeline" returns
```

**There is no separate frontend/backend/AI-agent service.** The only "backend" is `plugin.py`; the only
"frontend" is a static dashboard; the only "agent loop" is provided by the Anna OS host (see §2).

---

## 2. Current agent architecture — how AI talks to tools

```
Natural language
   │
   ▼
Anna OS host agent  ←── the ONLY reasoning loop (LLM, memory, tool selection)
   │  invoke {tool, arguments}                    (JSON-RPC 2.0 over stdio, "protocol v2")
   ▼
plugin.py  ── MANIFEST/`describe` advertises 7 tools ── TOOLS{} dispatch table
   │
   ├─► pcbai/steps/*        (pure-Python generators, no KiCad needed)
   ├─► pcbai/steps/pcb_router.py  ─► spawns `python3` + `import pcbnew`  (subprocess)
   ├─► pcbai/steps/kicad_pcb_writer.py ─► `import pcbnew` at module import (hard dependency)
   └─► reverse-RPC  sampling/createMessage  ─► plugin borrows the host LLM
```

Two alternative reasoning paths exist, and they are **not** interchangeable:

| Path | Selection logic | Notes |
|---|---|---|
| A. Anna host LLM | default when `PCB_AI_LLM_PROVIDER` is unset or `=anna` | plugin calls back into the host via `sampling/createMessage` (`plugin.py:46-110`) |
| B. External provider | when `PCB_AI_LLM_PROVIDER` ∈ {openai, gemini, claude/anthropic, ollama, lmstudio} | `pcbai/llm/provider.py:get_provider()`; raw HTTP via `requests` |

Key structural facts about the AI layer:

* **No function/tool-calling protocol anywhere.** `LLMProvider.chat()` accepts `List[Dict]` and returns a `str`.
  There is no `tools=[...]` schema, no tool-choice loop, no JSON-schema enforcement, no streaming. Only two
  call-sites use the LLM (`parse_requirements`, `full_pipeline` report), and both are prompt-for-JSON + `json.loads`.
* **The plugin never plans, loops, or self-corrects.** It is a passive tool server. Anna's host agent is the brain.
* `sample()` blocks with `resp = q.get(timeout=10)` (`plugin.py:104`) — a 10 s ceiling on *any* host-LLM call, which
  is shorter than real reasoning calls on long datasheets (the author's own 4096-token extraction prompt).
* JSON parsing is triple-duplicated (```` fence` stripping in `requirements_parser.py:41-45`,
  `plugin.py:250-268`, `plugin.py:373-378`) and will throw on any prose before/after the JSON.

---

## 3. Module-by-module status of `pcbai/steps/`

| Module | LOC | Status | Notes |
|---|---:|---|---|
| `design_compiler.py` | 42 | **BROKEN / FAKE** | `compile_design(prompt, dir)` **ignores `prompt` entirely**: it `shutil.copytree`s `template_project/` and returns those paths. `generate_schematic`/`generate_pcb` are imported at `:5-6` and never called. |
| `kicad_pcb_writer.py` | 580 | Implemented but **hardcoded + dead** | Real KiCad authoring via `pcbnew` API, but board is fixed: 40×30 mm ESP32-C3, one net list, one `_NET_MAP`, one placement table. `import pcbnew` at module top (`:26`) with hardcoded `/usr/lib/kicad/lib/python3/dist-packages` (`:23`). Never invoked in production path (only via dead import). |
| `kicad_schematic_writer.py` | 479 | Implemented but **hardcoded + dead** | Emits `.kicad_sch` by raw string S-expr templating (KiCad 6-era `version 20231120`), 23 ESP32/BME280-specific references. Not wired to any tool. |
| `pcb_router.py` | 152 | **Non-functional by construction** | Generates a temp Python script that loads footprints from `<outdir>/footprints/*.kicad_mod` — **nothing ever writes that directory** (verified: `route_pcb` output dir contains only `fp-lib-table` and `netlist.xml`). Also writes JSON into a file named `netlist.xml`, swallows all errors into a `status` string, and returns `success: true` regardless. |
| `smart_placer.py` | 107 | Experimental heuristic | Centers U-parts at (100,100)+25 mm steps, snaps "decoupling" caps 3 mm off a matching IC pad, random orbit for the rest. `extract_layout_constraints_with_llm()` is a documented stub that just `pass`es. Bug: `"V" in n.upper()` matches almost every net name. |
| `native_router.py` | 69 | Self-declared **not production** | Naive 2-segment Manhattan daisy-chain, no obstacle avoidance, no vias, ignores clearance; requires `pcbnew`; opt-in via `PCB_AI_EXPERIMENTAL_ROUTER=1`. |
| `schematic_synthesizer.py` | 18 | **PLACEHOLDER** | Produces `{GND:[refs], VCC:[refs]}` with *empty* pad lists — no real connectivity ever. This is what `synthesize_netlist` returns. |
| `skidl_schematic.py` | 109 | Implemented, **unreachable** | Real SKiDL netlist generation + decoupling/buck support circuits. Only referenced by `cli.py synthesize`, never by `plugin.py`. SKiDL is not installed. Root `plugin.net`/`plugin.erc` are its leftovers. |
| `bom_generator.py` | 48 | Minimal static catalog | 10 keyword buckets → 12 fixed MPNs; unknown keyword → `{mpn}-UNKNOWN`. **No Octopart code** despite the tool description claiming it (`plugin.py:143`). |
| `requirements_parser.py` | 56 | Works | LLM→JSON with keyword-substring fallback (15 hardcoded keywords). No component counts, no pin/net intent, no board geometry. |
| `footprint_generator.py` (SMD RC/SOIC) | 137 | Works, **legacy format** | Emits KiCad 5/6 `module`/`fp_text` sexpr; not KiCad 8/9/10 compliant (`footprint`/`property`); `(tedit 5B3079AF)` literal. |
| `footprint_qfn_qfp.py` | 204 | Works | QFN/QFP, also legacy sexpr dialect. |
| `footprint_bga.py` / `_dip` / `_header` / `_usbc` / `_custom` | 86/81/26/37/41 | Mostly work | USB-C is a fixed template; header/DIP are simple grids; `KiCadModuleWriter` duplicated in 3 files with divergent behaviour. |
| `datasheet_package_extractor.py` | 241 | Works, weak | Regex/keyword extraction from PDF text; on the bundled real LM5164 datasheet returns only `{pkg_type: soic, pins: 8}` and `None` for pitch/body/pads (verified). Falls back to a localhost:1234 LLM and prints to stdout. |
| `datasheet_fetcher.py` | 32 | **STUB** | Guesses 3 URL patterns, no search, no caching; unused by `plugin.py`. |
| `gerber_exporter.py` | 21 | **STUB** | Calls `kicad-cli` if present, else writes `GERBERS_READY.txt` saying "Gerbers would be exported". Not called by any tool (only imported by `cli.py`). |

---

## 4. How the AI currently interacts with repository files

It essentially does not.

* **Zero file parsers.** No code path reads a `.kicad_pcb`, `.kicad_sch`, or `.kicad_pro` into data structures.
  The only S-expression work in the repo is the *ad-hoc reader inside `pcb_router`'s generated script*, which reads
  a JSON netlist, not a KiCad file.
* **Zero in-place editing.** Every "output" is a fresh file in a temp dir (or a copied template). The generated
  `.kicad_pcb` content is returned to the host as a **string in the RPC payload**; the UI writes it into a zip.
  Nothing ever loads, diffs, mutates or saves an existing project.
* **No backups, no revert, no undo, no file-safety layer.** `grep -rni "backup\|snapshot\|revert\|undo"` → 0 hits.
* **No validation of output.** No parser round-trip, no connectivity check, no DRC. `full_pipeline` does not even
  re-read the files it returns.

### Target CAD format / assumptions

* **Format:** KiCad S-expression v7+ (`kicad_pcb`, `kicad_sch`, `kicad_mod`) and **KiCad 10** specifically.
  Evidence: template `board.kicad_pcb` header is `(version 20260206) (generator "pcbnew") (generator_version "10.0")`.
* **Backend API:** `pcbnew` Python bindings (`/usr/lib/kicad/lib/python3/dist-packages`),
  footprint libs `/usr/share/kicad/footprints`, `kicad-cli` for Gerber export.
* **Authoring style:** footprint `lib_id` + `.kicad_pro` JSON + gerber set + `pcb_project.zip` download.
* **Manufacturing target:** LPKF ProtoLaser S4 / MultiPress S4 / Contac S4 (min trace ~0.15 mm, small vias) — stated
  in `executa.json`, `plugin.py` prompts and the README's mock report.
* **Format drift problem:** three dialects coexist — PCB writer targets KiCad 10, `kicad_schematic_writer` targets
  KiCad 6 (`20231120`), `footprint_*` target KiCad 5/6 (`module`/`fp_text`), frontend fallback uses `20211014`.
  A single project generated end-to-end mixes versions.

---

## 5. Capability matrix (measured, not documented)

Legend: **Complete** = works today as claimed · **Partial** = exists but unusable for real work ·
**Missing** = not implemented · **Broken** = implemented but fails/claims false success.

| Capability | State | Evidence |
|---|---|---|
| Repository/project inspection (`open project`, list files) | **Missing** | no tool, no code |
| PCB file **reading** (`.kicad_pcb` → model) | **Missing** | no parser anywhere |
| Schematic file reading (`.kicad_sch` → netlist) | **Missing** | writer only |
| PCB file **modification** of an existing board | **Missing** | writers create new files only |
| Board creation from scratch (template) | **Partial** | `full_pipeline` returns the fixed ESP32 template; verified identical output for 3 unrelated prompts |
| Board creation from analysis (dynamic) | **Broken/Dead** | `kicad_pcb_writer.generate_pcb` real but hardcoded ESP32 + requires `pcbnew`; not called |
| Schematic generation | **Dead code** | `kicad_schematic_writer` never called; template `.kicad_sch` shipped as static file |
| Component selection / BOM | **Partial** | 12-part hardcoded catalog; no MPN search, no vendor/stock, `X-UNKNOWN` on miss |
| Footprint generation | **Complete** (legacy dialect) | 13/13 unit tests pass; QFP-144 generated successfully for this audit |
| Footprint **library** lookup/management | **Missing** | depends on OS KiCad libs; no local .pretty handling |
| Pin/pad-level net assignment | **Partial** | `kicad_pcb_writer._NET_MAP` hardcodes 6 devices; SKiDL path unreachable |
| Placement | **Partial** | `smart_placer` heuristics, only reachable via `route_pcb` → requires pcbnew + a footprints dir that never exists |
| Routing | **Broken** | no router runs; FreeRouting only *exports* `.dsn`, never invokes it; `native_router` self-declares "expect shorts" |
| Design rules (net classes, constraints) | **Missing** | template `board.kicad_pro` has 1 class "Default", `design_settings` empty; no code reads/writes rules |
| DRC | **Missing** | no `kicad-cli drc`, no pcbnew `BOARD_DESIGN_SETTINGS`/`DRC` usage; zero rules defined to check |
| ERC | **Missing (in agent)** | SKiDL `ERC()` exists in `skidl_schematic.py:105` only, unreachable from any tool |
| Unconnected-net / dangling-pin detection | **Missing** | — |
| Schematic↔PCB consistency check | **Missing** | — |
| BOM generation | **Partial** | as above; no cost/stock, no group-by of identical parts |
| Gerber/manufacturing export | **Stub** | `gerber_exporter` writes a marker file; template ships pre-baked gerbers |
| AI reasoning (planning, tool choice) | **External** | lives in Anna host; local providers are plain text completion, no tool loop |
| Tool execution / RPC | **Complete** | initialize/describe/health/invoke/shutdown verified working over stdio |
| Self-inspection of its own changes | **Missing** | no read-back after write |
| Error recovery / rollback | **Broken** | blanket `except` → `success: true` (see §6 D2/D3) |
| Human-in-the-loop confirmation for mutations | **Missing** | no confirm/approve concept |
| Project save / backup | **Missing** | temp dirs only; `full_pipeline` deletes its temp dir (`plugin.py:~490`) |
| Tests for PCB intelligence | **Missing** | 13 tests cover footprints + datasheet regex only |
| CI | **Missing** | no `.github/`, no pre-commit, no lint config |

---

## 6. Defect register (highest impact first)

| # | Severity | Defect | Location |
|---|---|---|---|
| D1 | **Critical — honesty** | README and tool descriptions advertise capabilities the code does not have ("perfectly routed board", "Fixes DRC", "Octopart lookups", "ERC checks" in the agent path). The `full_pipeline` tool returns the same board for every prompt. | `design_compiler.py:8-31`, `README.md`, `executa.json` |
| D2 | **Critical — safety** | Tools report `success: true` even when the operation failed. `route_pcb` returns `{"success": true, "data": {"status": "failed: Traceback ... ModuleNotFoundError: pcbnew"}}` (verified live). `full_pipeline` returns `success: true` with `analysis_report = "Pipeline failed: <traceback>"`. | `pcb_router.py:136-146`, `plugin.py:~530` |
| D3 | **Critical — stdout pollution** | 23 `print()` calls inside `pcbai/*` write to **stdout**, which is the JSON-RPC channel; only `log()` uses stderr. Observed in smoke run: `[harness] Bad JSON from plugin: [requirements_parser] LLM unavailable…`. A host that parses stdout strictly will desync on the first warning. | `kicad_pcb_writer.py` (8), `pcb_router.py` (7), `llm/provider.py:175,177`, `requirements_parser.py:50`, `skidl_schematic.py:107`, `datasheet_package_extractor.py:135`, `kicad_schematic_writer.py:479` |
| D4 | **Critical — no project state** | Nothing can read a PCB/schematic. Any "modify my board" request is impossible. | whole repo |
| D5 | **High — pipeline break** | `route_pcb` looks for footprints in `<outdir>/footprints`, which is never populated; `generate_footprint` writes to a `TemporaryDirectory` that is discarded. Netlist `pins` are component refs, not `REF-PIN`, so pad-level connectivity cannot exist. | `pcb_router.py:35-41`, `plugin.py:400-460`, `schematic_synthesizer.py:11-18` |
| D6 | **High — hard KiCad dependency** | `import pcbnew` at module import in `kicad_pcb_writer.py` and `smart_placer.py`/`native_router.py` makes them unimportable off-KiCad; `pcb_router` shells out to whatever `python3` is on PATH (not KiCad's), so it cannot work even with KiCad installed unless the env is perfect. | `kicad_pcb_writer.py:23-29`, `pcb_router.py:133-141` |
| D7 | **High — format drift** | Four different KiCad S-expression dialects in one product; generated footprints are KiCad 5/6 `module`/`fp_text`, which KiCad 8/9/10 will not load cleanly. | `footprint_generator.py:52-56`, `kicad_pcb_writer.py` (10), `kicad_schematic_writer.py`, `bundle/app.js` |
| D8 | **Medium — packaging** | `pcb-designer-plugin` and `pcb-designer-plugin.exe` are 0-byte files; `executa.json` selects `distribution.active = "binary"` with `binary_artifacts.linux-x86_64.path = dist/pcb-designer-linux-x86_64.tar.gz`, which does not exist. Anna's published app cannot start from this repo state. | `executa.json:24-40` |
| D9 | **Medium — broken entry points** | `gemini_test.py:4` and `gemini_e2e.py:4` `sys.path.insert(0, "/home/assalas/Downloads/...")` → `ModuleNotFoundError: test_harness` for any other user (their own path was also wrong, `/Downloads/` vs `/`). | root scripts |
| D10 | **Medium — latent typing bug** | `plugin.py` uses `Dict`/`Optional`/`Any` in runtime-evaluated annotations (`:33` module-level, `:482` function body) without importing `typing`; survives only because of `from __future__ import annotations`. Breaks `get_type_hints`, pydantic-style validation, and any strict static check. | `plugin.py:8-33` |
| D11 | **Medium — timeouts** | `sample(...)` waits a hard 10 s; `requests` timeouts are 60 s for APIs, 5 s for LM Studio model list. No retry/backoff anywhere. | `plugin.py:104`, `llm/provider.py` |
| D12 | **Medium — test asset broken** | `test_datasheet_MP1584.pdf` is an **HTML error page**, not a PDF (`PDFSyntaxError: No /Root object`). Any real test using it fails for the wrong reason. | `test_datasheet_MP1584.pdf` |
| D13 | **Low — hygiene** | Committed artifacts: `.coverage` (53 KB), `plugin.log`, `plugin.erc`, `plugin.net`, `plugin_sklib.py`, 2.6 MB of `assets/*.jpg`, 2 MB datasheet, 143 KB template, 268 KB `uv.lock`. Frontend `app.js` fabricates PCB/footprint files in the browser when fields are missing (a second, hidden "generator" that diverges from the Python one). | repo root, `bundle/app.js:124-152` |
| D14 | **Low — CLI mismatch** | `cli.py design` advertises "Gerbers" and always prints ✅ even though `generate_pcb` returned False; `export_gerbers` imported but never called; `synthsize` writes `netlist.txt`. | `pipeline/cli.py:31-46,186-197` |

### Verified working today (do not rewrite these)

Run in this sandbox with `.venv` (click, requests, pytest, pypdf, pdfminer.six):

* `pytest tests` → **13 passed** (footprint generators + datasheet regex extractor).
* `test_harness.py --smoke` → **10/10 "passed"** — protocol handshake, 7 advertised tools, `parse_requirements`,
  `generate_bom`, `generate_footprint`, unknown-tool error, `full_pipeline` (but see D1/D2: those "passes" are
  fallbacks and template copies, not real PCB work).
* `python -m pcbai.pipeline.cli footprint --type qfp --pins 144 --pitch 0.5 …` → writes a 144-pad TQFP `.kicad_mod`
  in ~2 s. **This is the most genuinely reusable asset for the SV-16** (LFE5U-12F-6TG144C is a 144-pin TQFP).
* `plugin.py` stdio JSON-RPC server: `initialize`, `describe`, `health`, `invoke`, `shutdown` all behave.

---

## 7. Environment assessment (this sandbox)

| Item | Result |
|---|---|
| Python | 3.11.2 at `/usr/bin/python3` (pyproject requires ≥3.10 ✔) |
| pip / network to PyPI | ✔ works (installed click, requests, pytest, pypdf, pdfminer.six) |
| `uv` | ✘ not installed — `test_rpc.py` and `executa.json` `command` depend on it |
| KiCad / `kicad-cli` | ✘ absent; `apt update` fails (port 80 to deb.debian.org blocked, HTTPS-only proxy) → **no apt-installable KiCad here** |
| `pcbnew` bindings | ✘ absent, and no pip wheel exists — hard blocker for the current design |
| `skidl` | ✘ absent (installable from PyPI: 2.3.0) |
| Pure-Python KiCad libs available on PyPI | ✔ `playa-pdf` 1.1.0 (KiCad 8/9/10 object model — **reader and writer**), `simp-sexp`, `kinet2pcb`, `hierplace`, and `skidl` (already in `uv.lock`, unused) |
| Repo state | clean working tree; no venv/lockfile drift; single commit |

**Consequence for the roadmap:** any architecture that *requires* `pcbnew` cannot be built, tested or validated in
this environment. The KiCad `pcbnew` path must become an **optional accelerator**, with a pure-Python
S-expression model as the primary substrate.

---

## 8. What is missing to become a practical PCB-design agent

1. **Project model + readers** — parse `.kicad_pcb`/`.kicad_sch`/`.kicad_pro` into inspectable structures
   (outline, layers, stackup, nets, net classes, components, pads, tracks, vias, zones, rule areas/keepouts,
   design settings, coordinates). None exists.
2. **Mutation layer with validation + rollback** — atomic ops (move/rotate/add/remove component, add/remove track
   & via, set net class, change rules, edit outline) each with: backup → apply → reload → diff → validate → keep
   or revert. None exists.
3. **Real connectivity** — net → `REF-PIN` mapping end-to-end (BOM → schematic → PCB), replacing
   `schematic_synthesizer`'s empty pin lists, so that routing/ERC/DRC have something to work on.
4. **DRC/ERC/validation** — clearance, track-width, creepage, unconnected/dangling nets, courtyard overlap,
   keepout violations, missing footprints, orphan refs, silksolder/mask, Fab outline closure. Optional
   `kicad-cli drc/erc/fab` pass-through when KiCad is present.
5. **A tool surface the agent can actually drive** — ~15-25 fine-grained, typed, idempotent tools with an
   explicit read-only vs mutating split, plus dry-run/confirm semantics, replacing 7 coarse one-shot pipeline
   tools.
6. **A real agent loop for non-Anna use** — a local orchestrator that does
   inspect → plan → act → verify → report, with tool-call schemas, so the system is usable from this repo
   standalone (README's "universal agent" claim) rather than only as an Anna plugin.
7. **Truthful reporting** — success only when the post-condition is verified; failures as structured errors.
8. **Tests + CI** — parser round-trip, mutation, validation and golden-board regression suites; a runner that works
   without KiCad.
9. **Project/workspace management** — a notion of "the SV-16 project directory" (open/save/list), instead of
   anonymous temp dirs; file safety (backup, atomic write, `.bak` retention, no-clobber).

---

## 9. Prioritised implementation roadmap

Each phase is independently shippable, keeps the existing architecture, and ends with a commit + green tests.

### Phase 2 — Make the existing project run reproducibly (foundation)
*Add no features. Make what exists honest, importable and testable.*
1. Repo hygiene: remove committed artifacts (`.coverage`, `plugin.*`, zero-byte binaries) or move to `docs/`;
   document the 2.6 MB assets; replace `test_datasheet_MP1584.pdf` (D12); add `.venv`, `build/`, `work/`, `*.zip`
   to `.gitignore`.
2. Fix D3: route **all** logging to stderr/logger inside library code; make stdout RPC-exclusive; add a test that
   asserts stdout is pure JSON-RPC.
3. Fix D2/D14: propagate real failure — every tool returns `{success: false, error: …}` when the work failed;
   CLI stops printing ✅ on `generate_pcb → False`.
4. Fix D9 (hardcoded paths), D10 (missing `typing` imports), `netlist.xml`→`netlist.json`.
5. Make `pcbnew` optional everywhere: lazy import + `has_pcbnew()` capability probe; remove hardcoded
   `/usr/lib/kicad/...` and `/usr/share/kicad/footprints` in favour of env vars with defaults.
6. Add a single documented dev setup: `scripts/bootstrap.sh` (venv + `pip install -e .[test]`), a Makefile, and a
   pytest config that runs from the repo root (currently needs `PYTHONPATH`).
7. Decide `uv` vs `pip`: either vendor a bootstrap that works without `uv` or make `uv` a documented requirement.
   *Deliverable:* fresh clone → `./scripts/bootstrap.sh && make test` green, and the 7 existing tools each returning
   honest results.

### Phase 3 — Prove the agent loop on a throwaway project
1. Introduce `pcbai/agent/` with a provider-agnostic **tool-calling loop** (schema, dispatch, max-steps,
   trace-of-record) usable both from Anna (`plugin.py`) and standalone (`pcbai chat`).
2. Wrap the existing safe read/generate tools so the loop can drive them: `describe_project`, `generate_footprint`,
   `extract_package_from_pdf`, `generate_bom`.
3. Add the *honest* end-to-end test that is missing today: prompt → tool call → file written → **file re-read** →
   post-condition asserted.
4. Add a `DummyLLM` scripted provider so this test runs with no API key.
   *Deliverable:* a CI test where the agent, via tools only, creates a 3-part test board and proves it did so.

### Phase 4 — PCB file intelligence (the real gap)
1. Adopt a pure-Python S-expression core: `playa-pdf` (or a vendored sexpr reader + `simp-sexp`) to parse and write
   `kicad_pcb` / `kicad_sch` / `kicad_pro`; keep `pcbnew` as an optional backend when available.
2. Build `pcbai/model/`: `Board`, `Layer`, `Component`, `Pad`, `Track`, `Via`, `Zone`, `Net`, `NetClass`,
   `Keepout`, `DesignRules`, `Geometry` (mm, board-relative origin, bbox/overlap queries).
3. Implement `inspect_project / inspect_pcb / inspect_schematic / list_components / get_component / get_nets /
   get_net / get_board_outline / get_design_rules / find_overlaps / components_without_footprints` — read-only,
   JSON-serialisable, and used by the report generation.
4. Golden tests on the bundled template board + a hand-written "tiny valid board" fixture (with proper KiCad 10
   net tables, which the shipped template lacks) + fuzz/robustness tests on malformed files.
   *Deliverable:* `inspect_pcb("sv16.kicad_pcb")` answers every question in the user's §5 example list.

### Phase 5 — Controlled, validated modification
1. Transactional write path: `create_backup` → mutate in memory → serialize → atomic replace → `load()` →
   validate → keep or revert. Never a bare overwrite.
2. Operations: `add_component`, `remove_component`, `move_component` (absolute + delta mm), `rotate_component`,
   `flip_side`, `set_footprint`, `add_net_class`, `set_design_rules`, `add_track`, `add_via`, `remove_track`,
   `set_board_outline`, `add_keepout`, `add_mounting_hole`.
3. Rules-aware placement: snap to grid, courtyard/assembly clearance, keepout respect, edge clearance, and a
   feasibility check *before* moving (return candidate positions + reasons when infeasible).
4. Real routing: netlist-driven A*/Lee router on a rasterised copper field with clearance & width from net classes,
   layer changes via vias; replace `native_router`'s daisy-chain; keep `.dsn` export **and actually run**
   Freerouting when it is installed.
   *Deliverable:* "Move C12 5 mm left", "Place U1 at board centre", "Route VDD_3V3" execute, validate, and revert
   cleanly on failure.

### Phase 6 — Validation & manufacturing checks
1. Parser validation, connectivity (unconnected nets, dangling pins, single-pad nets, net vs schematic mismatch),
   geometry DRC (clearance/width/courtyard/edge/silkscreen-mask), fab checks (annular ring, min drill,
   via aspect, outline closure, fiducials+panel notes for LPKF), BOM integrity (missing MPN/footprint/value, ref
   collisions), and `kicad-cli drc/erc` + `export gerbers/drill/pos` pass-through when KiCad is present.
2. `validate_project()` and `run_drc()` return machine-readable findings with severity, location, rule id and
   suggested fix, so the agent can loop: fix → re-run → report.
3. Consistency checker: schematic ↔ PCB (KiCad-style netlist diff).

### Phase 7 — AI orchestration over the new tool surface
1. System prompt encoding the workflow contract (inspect → plan → mutate → reload → validate → fix → report),
   read-only vs mutating policy, and "ask when ambiguous".
2. Constrained tool schemas (typed params, enums, ranges), step budget, loop/repeat detection, refusal of
   destructive ops without confirm, per-turn change summary (files touched, components moved, DRC delta).
3. Fix `sample()` timeout and JSON extraction; add provider-level retry/backoff.

### Phase 8 — Test suite & CI
1. Split `tests/` to run from repo root without `PYTHONPATH`; add `pcbai` as an installable package (or path
   fixture) so imports are stable.
2. Suites: parser round-trip, mutation, validation, routing, tool-dispatch, RPC-protocol purity, golden board
   regression (hash/geometry invariants of a reference board), and a no-network/no-API CI lane.
3. Add `.github/workflows/ci.yml`: lint (ruff) + pytest + `kicad` optional job (docker image) for real DRC/Gerber.

### Phase 9 — SV-16 (only after 2-8 are green)
1. `projects/sv16/` created from the KiCad 10 template the toolchain produces (not the ESP32 copy).
2. BOM for LFE5U-12F-6TG144C (+ generated 144-TQFP footprint, already close), power tree, configuration/DDR/IO
   banks, clock/oscillator, connectors, mounting, decoupling.
3. Board-level constraints as net classes (Vcore/VCCIO/1V8/3V3, differential clocks), keepouts, then placement →
   routing → DRC loop → BOM/MRP; iterate until the validation suite reports clean.
4. Never treat "a file was generated" as "the board is done" — the acceptance gate is the Phase 6 checks.

### Suggested commit sequence (per the phase instruction, no mega-commits)
```
chore: remove committed build artifacts and debug leftovers
fix: route all library logging to stderr; keep stdout RPC-clean
fix: propagate tool failures instead of returning success:true
fix: make pcbnew optional and remove hardcoded KiCad paths
fix: correct test entrypoints and add reproducible bootstrap
feat: pure-Python kicad_pcb/kicad_sch parser + project model
feat: read-only inspection tools (inspect_pcb, list_components, get_nets, ...)
feat: transactional mutation layer with backup/rollback
feat: rules-aware placement and A* router with vias
feat: DRC/ERC/connectivity/fab validators + kicad-cli pass-through
feat: agent tool-calling loop with inspect→plan→act→validate→report
test: golden-board and mutation regression suites + CI
feat(sv16): project scaffold and board design rules
```

---

## 10. Decisions needed from the user before Phase 2

| # | Question | Options / my default |
|---|---|---|
| Q1 | **Primary substrate:** keep `pcbnew` as required, or go pure-Python S-expr (`playa-pdf`) with `pcbnew` optional? | Default: **pure-Python primary, pcbnew optional** — the only option testable in this sandbox, and it also removes D6. |
| Q2 | **Anna app vs standalone agent.** Do we keep `plugin.py`+`executa.json` as the product surface and add a standalone CLI/agent, or drop Anna packaging? | Default: keep both (preserve architecture), add `pcbai chat` standalone loop. |
| Q3 | **KiCad target version** for emitted files: KiCad 10 (`20260206`, current template) vs KiCad 8 (`20231210`)? The repo currently emits four dialects. | Default: **KiCad 8 sexpr (widely installed, stable) with KiCad 10 accepted on read**; footprint writers upgraded from `module`/`fp_text` to `footprint`/`property`. |
| Q4 | Is the **SV-16 board size/layer count/fab** fixed (LPKF prototype vs. JLC/OSHPark 4-layer)? This determines design rules and how aggressive routing/placement validation must be. | Needed for Phase 9; also affects default net classes in Phase 5. |
| Q5 | Should the fabricated frontend (`app.js` browser-side generator) be **removed** and the UI made a thin viewer of real server artifacts? | Default: yes — it currently can ship files that never went through any validation. |
| Q6 | LLM provider/keys for development testing (Gemini/OpenAI/Claude/local)? | Default: scripted `DummyLLM` for CI; live provider optional via env. |

**No architectural changes have been made.** Only this document plus a scratch probe under `.audit/` (untracked,
safe to delete).
