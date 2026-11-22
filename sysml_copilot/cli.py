"""`mbse` command line: the same deterministic operations as the MCP server.

    mbse -m data/ summary
    mbse -m data/ get_element element=VehicleFuelSystem.Vehicle
    mbse -m data/ create_element kind="part def" name=Filter owner=VehicleFuelSystem
    mbse -m data/ apply_batch '{"operations": [...]}'
    mbse -m data/ --dry-run update_element element=VehicleFuelSystem.FuelTank name=Tank
    mbse -m data/ fmt            # rewrite files in canonical form
    mbse -m data/ check          # validate, non-zero exit on errors (for CI)
    mbse -m data/ serve          # web UI
    mbse -m data/ mcp            # MCP server on stdio

Arguments are either one JSON object or `key=value` pairs (values are parsed
as JSON when possible: `max_hops=2`, `cascade=true`, `type='["A","B"]'`).
Output is always JSON on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys

from .operations import ALL_OPERATIONS, MUTATING_OPERATIONS, ModelService, call
from .syntax import SysmlSyntaxError


def _parse_args(items):
    if len(items) == 1 and items[0].lstrip().startswith("{"):
        return json.loads(items[0])
    out = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"argument '{item}' must be key=value or a JSON object")
        key, raw = item.split("=", 1)
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw
    return out


def _print(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mbse", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-m", "--model", action="append", default=None,
                        help=".sysml file or directory (repeatable; default: current directory)")
    parser.add_argument("--dry-run", action="store_true", help="print the diff instead of saving")
    parser.add_argument("command", help="operation name, or one of: ops, fmt, check, serve, mcp")
    parser.add_argument("args", nargs="*", help="JSON object or key=value pairs")
    ns = parser.parse_args(argv)
    paths = ns.model or ["."]

    if ns.command == "ops":
        _print({"operations": list(ALL_OPERATIONS), "mutating": list(MUTATING_OPERATIONS)})
        return 0
    if ns.command == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main(paths + (["--no-autosave"] if ns.dry_run else []))
        return 0
    if ns.command == "serve":
        from .web import serve
        opts = _parse_args(ns.args) if ns.args else {}
        serve(paths, host=opts.get("host", "127.0.0.1"), port=int(opts.get("port", 8765)))
        return 0

    try:
        service = ModelService(paths)
    except SysmlSyntaxError as exc:
        _print({"ok": False, "error": exc.to_json()})
        return 2
    except OSError as exc:
        _print({"ok": False, "error": {"code": "io_error", "message": str(exc)}})
        return 2

    if ns.command == "fmt":
        result = service.diff() if ns.dry_run else service.save()
        _print({"ok": True, **result})
        return 0
    if ns.command == "check":
        result = call(service, "validate")
        _print(result)
        return 1 if result["errors"] else 0

    args = _parse_args(ns.args)
    result = call(service, ns.command, args)
    if result.get("ok") and (ns.command in MUTATING_OPERATIONS or ns.command in ("apply_batch", "undo", "redo")):
        if ns.dry_run:
            result["diff"] = service.diff()["diff"]
        else:
            result["saved"] = service.save()["written"]
    _print(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
