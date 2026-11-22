import asyncio
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from sysml_copilot import cli
from sysml_copilot.operations import ModelService

from .conftest import PKG


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_cli_read_write_and_check(capsys, sample_path):
    code, out = run_cli(capsys, "-m", sample_path, "find_elements", "query=fuel pump", "limit=1")
    assert code == 0 and out["results"][0]["id"] == f"{PKG}.FuelPump"

    code, out = run_cli(capsys, "-m", sample_path, "--dry-run", "create_element",
                        "kind=part def", "name=Filter", f"owner={PKG}")
    assert code == 0 and "+    part def Filter;" in out["diff"]
    assert "Filter" not in open(sample_path, encoding="utf-8").read()

    code, out = run_cli(capsys, "-m", sample_path, "create_element",
                        json.dumps({"kind": "part def", "name": "Filter", "owner": PKG}))
    assert code == 0 and out["saved"] == [sample_path]
    assert "part def Filter;" in open(sample_path, encoding="utf-8").read()

    code, out = run_cli(capsys, "-m", sample_path, "delete_element", f"element={PKG}.Nope")
    assert code == 1 and out["error"]["code"] == "not_found"

    code, out = run_cli(capsys, "-m", sample_path, "check")
    assert code == 0 and out["errors"] == 0


def test_cli_reports_syntax_errors(capsys, tmp_path):
    bad = tmp_path / "bad.sysml"
    bad.write_text("package P {\n  part def A\n}\n")
    code, out = run_cli(capsys, "-m", str(bad), "summary")
    assert code == 2 and out["error"]["line"] == 3


def test_mcp_server_tools(sample_path):
    pytest.importorskip("mcp")
    from sysml_copilot.mcp_server import build_server

    server = build_server(ModelService([sample_path]))

    async def scenario():
        tools = {t.name: t for t in await server.list_tools()}
        assert {"create_element", "apply_batch", "impact_analysis", "validate"} <= set(tools)
        assert "kind" in tools["create_element"].input_schema["properties"]
        res = await server.call_tool("create_element", {"kind": "part def", "name": "Pump2", "owner": PKG})
        return res

    res = asyncio.run(scenario())
    assert res.structured_content["created"] == f"{PKG}.Pump2"


def test_web_api(sample_path):
    from sysml_copilot.web import make_handler

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ModelService([sample_path])))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        html = urllib.request.urlopen(base + "/").read().decode()
        assert "MBSE Workbench" in html
        req = urllib.request.Request(base + "/api/call", data=json.dumps({"op": "summary"}).encode(),
                                     headers={"Content-Type": "application/json"})
        data = json.loads(urllib.request.urlopen(req).read())
        assert data["ok"] and data["element_count"] > 10
    finally:
        httpd.shutdown()
