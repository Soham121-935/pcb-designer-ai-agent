"""`pcbai-agent` — drive the PCB tool layer from a shell (no Anna, no server).

    pcbai-agent tools                       # every tool, safety class and maturity
    pcbai-agent describe generate_footprint # one tool's parameters
    pcbai-agent call capabilities
    pcbai-agent call generate_footprint --arg footprint_type=qfp --json '{"params":{...}}'
    pcbai-agent call move_component --arg ref=C12 --json '{"delta":[0,-5]}'

    pcbai-agent scaffold pcbai/boards/sv16.yaml --out ../build/sv16     # generate a real project
    pcbai-agent validate ../build/sv16/sv16.kicad_pcb                   # our DRC-style checks
    pcbai-agent inspect   ../build/sv16/sv16.kicad_pcb
    pcbai-agent nets      ../build/sv16/sv16.kicad_pcb --undrouted
    pcbai-agent edit      ../build/sv16/sv16.kicad_pcb --op '{"op":"set_rule","field":"min_clearance","value":0.15}'

`call` (and every command above) exits non-zero when the tool reports success:false, so they are all
scriptable in tests and CI.
"""
from __future__ import annotations

import json
import sys

import click

from pcbai.agent.registry import describe_all, dispatch, registry


def _parse_kv(pairs: tuple) -> dict:
    out: dict = {}
    for pair in pairs or ():
        if "=" not in pair:
            raise click.UsageError(f"--arg expects key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        try:
            out[key] = json.loads(value)
        except json.JSONDecodeError:
            out[key] = value
    return out


@click.group()
def main() -> None:
    """Standalone PCB-design agent tool surface (KiCad projects, repo-local)."""


@main.command()
@click.option("--mutating/--read-only", "only", default=None, help="Filter by safety class.")
def tools(only: bool | None) -> None:
    """List all tools with their safety class and maturity."""
    rows = describe_all()
    if only is not None:
        rows = [r for r in rows if (r["safety"] == "mutating") == only]
    width = max(len(r["name"]) for r in rows) + 2
    for r in rows:
        flag = {"implemented": " ", "placeholder": "P", "not-implemented (Phase 4)": "4",
                "implemented (no KiCad needed)": "K",
                "not-implemented (Phase 5)": "5", "not-implemented (Phase 6)": "6",
                "implemented (requires REF-PIN netlist)": "!",
                "implemented (weak: regex extractor, no OCR here)": "D"}.get(r["maturity"], "?")
        click.echo(f"[{flag}] {r['name']:<{width}} {r['safety']:<10} {r['summary']}")
    click.secho("\n  legend: ' '=usable  P=placeholder  4/5/6=arrives in that phase  "
                "!=needs real netlist  D=degraded extraction", dim=True)


@main.command()
@click.argument("name")
def describe(name: str) -> None:
    """Show one tool's parameters."""
    spec = registry().get(name)
    if spec is None:
        raise click.ClickException(f"unknown tool: {name}")
    click.echo(json.dumps(spec.describe(), indent=2))


@main.command()
@click.argument("name")
@click.option("--arg", "args", multiple=True, help="key=value (value parsed as JSON when possible).")
@click.option("--json", "json_args", default="{}", help="Extra arguments as a JSON object.")
@click.option("--allow-mutating/--no-mutating", default=True, help="Policy gate for writes.")
@click.option("--confirm", is_flag=True, help="Acknowledge that this call mutates project files.")
def call(name: str, args: tuple, json_args: str, allow_mutating: bool, confirm: bool) -> None:
    """Invoke a tool and print its JSON result."""
    try:
        extra = json.loads(json_args or "{}")
    except json.JSONDecodeError as exc:
        raise click.UsageError(f"--json is not valid JSON: {exc}") from exc
    arguments = {**_parse_kv(args), **extra}
    result = dispatch(name, arguments, allow_mutating=allow_mutating, confirm=confirm)
    click.echo(json.dumps(result, indent=2, default=str))
    if not result.get("success"):
        sys.exit(1)


@main.command()
def backend() -> None:
    """Which EDA backends are usable in this environment."""
    result = dispatch("capabilities")
    click.echo(json.dumps(result, indent=2))


# ─────────────────────────────────────────────────────────────────────────────
# native KiCad commands — these need no KiCad install (see pcbai/kicad/)
# ─────────────────────────────────────────────────────────────────────────────
@main.command()
@click.argument("spec", type=click.Path(exists=True))
@click.option("--out", "out_dir", type=click.Path(), default=None,
              help="Where to write (default: <project>/build/scaffold).")
@click.option("--stem", default=None, help="File stem; defaults to the spec name.")
@click.option("--dialect", type=click.Choice(["kicad-8", "kicad-9", "kicad-10"]), default="kicad-10")
@click.option("--no-route", is_flag=True, help="Skip the checked signal routes (planes/fanout only).")
@click.option("--dry-run", is_flag=True, help="Build and validate without writing.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def scaffold(spec: str, out_dir: str | None, stem: str | None, dialect: str, no_route: bool,
             dry_run: bool, as_json: bool) -> None:
    """Generate a KiCad project from a design SPEC (.yaml/.json).

    The generator places parts, pours the planes, fans out power and lays only the tracks it can
    prove are legal; what it leaves for you is reported, not hidden.
    """
    result = dispatch("generate_scaffold",
                      {"spec": spec, "out_dir": out_dir, "stem": stem, "dialect": dialect,
                       "route": not no_route, "dry_run": dry_run},
                      allow_mutating=not dry_run, confirm=not dry_run)
    _emit(result, as_json)
    if not result.get("success"):
        sys.exit(1)


@main.command()
@click.argument("path", type=click.Path(), required=False)
@click.option("--json", "as_json", is_flag=True)
def validate(path: str | None, as_json: bool) -> None:
    """Run the built-in board checks (clearance, shorts, drills, outline, nets, zones)."""
    result = dispatch("validate_board", {"path": path or ""})
    _emit(result, as_json)
    data = result.get("data") or {}
    if not data.get("ok") or not result.get("success"):
        click.secho("DRC-style failures found — KiCad's own `kicad-cli pcb drc` is still the "
                    "authority before you order.", fg="yellow", err=True)
        sys.exit(1)


@main.command()
@click.argument("path", type=click.Path(), required=False)
@click.option("--json", "as_json", is_flag=True)
def inspect(path: str | None, as_json: bool) -> None:
    """Summarise a .kicad_pcb (format dialect, stackup, counts, issues)."""
    _emit(dispatch("inspect_pcb", {"path": path or ""}), as_json)


@main.command()
@click.argument("path", type=click.Path(), required=False)
@click.option("--undrouted", is_flag=True, help="Only nets whose pads are not yet connected.")
@click.option("--json", "as_json", is_flag=True)
def nets(path: str | None, undrouted: bool, as_json: bool) -> None:
    """List nets with pad counts, classes and what still looks unconnected."""
    _emit(dispatch("get_nets", {"path": path or "", "only_undrouted": undrouted}), as_json)


@main.command()
@click.argument("path", type=click.Path(), required=False)
@click.option("--json", "as_json", is_flag=True)
def rules(path: str | None, as_json: bool) -> None:
    """Show the design rules (board file plus the project's net classes)."""
    _emit(dispatch("get_design_rules", {"path": path or ""}), as_json)


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--op", "ops", multiple=True, help='Edit as JSON, e.g. --op \'{"op":"set_rule",'
            '"field":"min_clearance","value":0.15}\'  (see `describe apply_edit`).')
@click.option("--write", is_flag=True, help="Actually write the file (default is a dry run).")
@click.option("--allow-lossy", is_flag=True, help="Accept that unwritten-by-model tokens are dropped.")
@click.option("--json", "as_json", is_flag=True)
def edit(path: str, ops: tuple, write: bool, allow_lossy: bool, as_json: bool) -> None:
    """Apply model-level edits to a .kicad_pcb, then re-read and re-validate the result."""
    edits = []
    for raw in ops or ():
        try:
            edits.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise click.UsageError(f"--op is not valid JSON: {exc}") from exc
    if not edits:
        raise click.UsageError("give at least one --op; `pcbai-agent describe apply_edit` lists them")
    result = dispatch("apply_edit", {"edits": edits, "path": path, "dry_run": not write,
                                     "allow_lossy": allow_lossy},
                      allow_mutating=write, confirm=write)
    _emit(result, as_json)
    if not result.get("success"):
        sys.exit(1)


def _emit(result: dict, as_json: bool) -> None:
    """Print a tool envelope as JSON (--json) or as a short human report."""
    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return
    if not result.get("success"):
        click.secho(f"FAILED ({result.get('reason')}): {result.get('error')}", fg="red", err=True)
        data = result.get("data")
        if data:
            click.echo(json.dumps(data, indent=2, default=str))
        return
    data = result.get("data") or {}
    summary = data.get("summary") if isinstance(data, dict) else None
    if isinstance(summary, dict):
        for key, value in summary.items():
            text = json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value)
            click.echo(f"  {key}: {text if len(text) <= 160 else text[:157] + '...'}")
    for key, value in data.items():
        if key in ("summary",):
            continue
        text = json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value)
        if len(text) > 220:
            text = text[:217] + "..."
        click.echo(f"  {key}: {text}")
    for warning in result.get("warnings") or []:
        click.secho(f"  ! {warning}", fg="yellow", err=True)


if __name__ == "__main__":  # pragma: no cover
    main()
