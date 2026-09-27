"""`pcbai-agent` — drive the PCB tool layer from a shell (no Anna, no server).

    pcbai-agent tools                       # every tool, safety class and maturity
    pcbai-agent describe generate_footprint # one tool's parameters
    pcbai-agent call capabilities
    pcbai-agent call generate_footprint --arg footprint_type=qfp --json '{"params":{...}}'
    pcbai-agent call move_component --arg ref=C12 --json '{"delta":[0,-5]}'

`call` exits non-zero when a tool reports success:false, so it is scriptable in tests.
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


if __name__ == "__main__":  # pragma: no cover
    main()
