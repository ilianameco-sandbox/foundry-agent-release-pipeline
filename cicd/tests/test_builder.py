"""Pipeline builder (agent_cicd.builder): pipeline.yaml round trip, preview/save, and the local server's guards."""
import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agent_cicd import builder, generate

CICD = Path(__file__).resolve().parent.parent


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    shutil.copytree(CICD / "agents" / "my-agent", agents / "demo")
    shutil.copytree(CICD / "agents" / "my-hosted-agent", agents / "hosted-demo")
    monkeypatch.setattr(generate, "WORKFLOWS_DIR", tmp_path / "workflows")
    return builder.Builder(agents)


def test_committed_pipeline_yaml_is_canonical():
    agent_dir = CICD / "agents" / "my-agent"
    text = (agent_dir / "pipeline.yaml").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert generate.dump_spec(generate.load_spec(agent_dir)) == text


def test_dump_round_trips_unusual_values(tmp_path):
    spec = generate.parse_spec(generate.DEFAULT_SPEC, "demo", "prompt")
    spec.signoffs[0]["checks"] = "Reviewed: scores, \"quotes\" & #hashes"
    spec.allowed_regions = []
    spec.harvest_cron = "0 11 * * 1"
    agent_dir = tmp_path / "demo"
    shutil.copytree(CICD / "agents" / "my-agent", agent_dir)
    (agent_dir / "pipeline.yaml").write_text(generate.dump_spec(spec), encoding="utf-8")
    assert generate.spec_data(generate.load_spec(agent_dir)) == generate.spec_data(spec)


def test_lists_agents_and_offers_defaults_without_pipeline_yaml(workspace):
    agents = {a["name"]: a for a in workspace.agents()}
    assert agents["demo"] == {"name": "demo", "kind": "prompt", "has_pipeline": True}
    assert agents["hosted-demo"] == {"name": "hosted-demo", "kind": "hosted", "has_pipeline": False}
    loaded = workspace.load("hosted-demo")
    assert loaded["exists"] is False and loaded["spec"] == generate.DEFAULT_SPEC
    assert loaded["presets"]["canada-strict"]["regions"] == ["canadacentral", "canadaeast"]
    with pytest.raises(KeyError):
        workspace.load("../../etc")


def test_preview_shows_the_plan_and_every_file_that_would_change(workspace):
    data = workspace.load("demo")["spec"]
    data["gates"]["redteam"] = False
    result = workspace.preview("demo", data)
    assert result["ok"]
    release = next(lane for lane in result["plan"] if lane["lane"].startswith("Release tag"))
    redteam = next(s for s in release["steps"] if s["key"] == "redteam")
    assert redteam == {**redteam, "on": False, "toggle": "gates.redteam"}
    paths = {f["path"].replace("\\", "/").split("/")[-1]: f["status"] for f in result["files"]}
    assert paths["pipeline.yaml"] == "changed" and paths["demo-release.yml"] == "new"
    pipeline_diff = next(f["diff"] for f in result["files"] if f["path"].endswith("pipeline.yaml"))
    assert "-  redteam: true" in pipeline_diff and "+  redteam: false" in pipeline_diff


def test_preview_reports_problems_instead_of_raising(workspace):
    data = workspace.load("demo")["spec"]
    data["environments"]["test"] = False  # the business role still has uat: true
    result = workspace.preview("demo", data)
    assert result["ok"] is False
    assert any("uat: true needs environments.test" in p for p in result["problems"])


def test_save_writes_canonical_pipeline_yaml_and_generates(workspace):
    data = workspace.load("hosted-demo")["spec"]
    data["gates"]["signoffs"] = [{"id": "lead", "title": "Tech lead", "environment": "signoff-lead"}]
    data["residency"].update(preset="canada-strict", allowed_regions=["canadacentral"])
    result = workspace.save("hosted-demo", data)
    assert result["ok"] and result["new_environments"] == ["signoff-lead"]
    agent_dir = workspace.agents_dir / "hosted-demo"
    spec = generate.load_spec(agent_dir)
    assert spec.preset == "canada-strict" and spec.allowed_regions == ["canadacentral"]
    assert (generate.WORKFLOWS_DIR / "hosted-demo-release.yml").is_file()
    assert not (generate.WORKFLOWS_DIR / "hosted-demo-promote-portal-agent.yml").exists()
    assert json.loads((agent_dir / "governance.json").read_text())["signoffs"] == {
        "lead": {"title": "Tech lead", "environment": "signoff-lead"}}
    assert workspace.preview("hosted-demo", data)["files"] == []


def test_save_refuses_invalid_settings_and_overlapping_names(workspace):
    data = workspace.load("demo")["spec"]
    data["gates"]["signoffs"] = []
    assert workspace.save("demo", data)["ok"] is False
    shutil.copytree(workspace.agents_dir / "demo", workspace.agents_dir / "demo-voice")
    result = workspace.preview("demo-voice", workspace.load("demo-voice")["spec"])
    assert result["ok"] is False and "starts with 'demo-v'" in result["problems"][0]


@pytest.mark.parametrize("text", ["line\x85break", "del\x7f", "c1\x9f", "lone \ud800 surrogate", "x" * 301])
def test_unwritable_checks_text_is_rejected_and_the_file_is_untouched(workspace, text):
    path = workspace.agents_dir / "demo" / "pipeline.yaml"
    before = path.read_bytes()
    data = workspace.load("demo")["spec"]
    data["gates"]["signoffs"][0]["checks"] = text
    result = workspace.save("demo", data)
    assert result["ok"] is False and "printable text" in result["problems"][0]
    assert path.read_bytes() == before


def test_missing_keys_load_as_the_generator_reads_them(workspace):
    path = workspace.agents_dir / "demo" / "pipeline.yaml"
    path.write_text("version: 1\ngates:\n  signoffs:\n    - {id: qa, title: QA, environment: signoff-qa}\n",
                    encoding="utf-8")
    loaded = workspace.load("demo")
    assert loaded["error"] is None
    assert loaded["spec"]["prod"]["drift_check"] == "" and loaded["spec"]["harvest"]["schedule"] == ""
    assert loaded["spec"]["environments"]["test"] is True
    # an invalid file still loads into the form, with the generator's defaults for missing keys
    path.write_text("version: 1\nprod: {rollback_on_smoke_failure: true}\n", encoding="utf-8")
    loaded = workspace.load("demo")
    assert "signoffs" in loaded["error"] and loaded["spec"]["prod"]["drift_check"] == ""


# ---------------------------------------------------------------- HTTP

@pytest.fixture
def server(workspace):
    srv, url = builder.make_server(workspace, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base, token = url.split("?t=")
    assert "<title>Pipeline builder</title>" in urllib.request.urlopen(url).read().decode()
    yield base, token
    srv.shutdown()
    srv.server_close()


def _call(url, path, token=None, body=None, headers=None):
    request = urllib.request.Request(url.rstrip("/") + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Builder-Token", token)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def test_page_and_static_files_are_served_with_a_strict_policy(server):
    url, token = server
    assert len(token) >= 40
    with urllib.request.urlopen(url + "app.js") as response:
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]
        assert response.headers["Content-Type"].startswith("text/javascript")
    page = urllib.request.urlopen(f"{url}?t={token}").read().decode()
    assert token not in page


def test_page_is_not_served_without_the_token(server):
    url, token = server
    for path in ("", "?t=wrong"):
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(url + path)
        assert error.value.code == 403


def test_bad_requests_get_json_errors(server, workspace):
    url, token = server
    request = urllib.request.Request(url + "api/preview", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", "X-Builder-Token": token,
                                              "Content-Length": "abc"})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 400 and "JSON body" in json.loads(error.value.read())["error"]
    (workspace.agents_dir / "demo" / "governance.json").write_text("{not json", encoding="utf-8")
    status, body = _call(url, "/api/preview", token, {"agent": "demo", "spec": workspace.load("demo")["spec"]})
    assert status == 500 and body["error"].startswith("JSONDecodeError")


def test_api_needs_the_page_token(server):
    url, token = server
    assert _call(url, "/api/agents")[0] == 403
    assert _call(url, "/api/agents", token="wrong")[0] == 403
    status, body = _call(url, "/api/agents", token)
    assert status == 200 and {a["name"] for a in body["agents"]} == {"demo", "hosted-demo"}


def test_api_rejects_other_hosts_and_origins(server):
    url, token = server
    assert _call(url, "/api/agents", token, headers={"Host": "evil.example:80"})[0] == 403
    assert _call(url, "/api/agents", token, headers={"Origin": "https://evil.example"})[0] == 403
    port = url.rsplit(":", 1)[1].strip("/")
    assert _call(url, "/api/agents", token, headers={"Origin": f"http://localhost:{port}"})[0] == 200


def test_api_preview_and_save_round_trip(server, workspace):
    url, token = server
    status, loaded = _call(url, "/api/spec?agent=demo", token)
    assert status == 200
    spec = loaded["spec"]
    spec["prod"]["drift_check"] = ""
    status, preview = _call(url, "/api/preview", token, {"agent": "demo", "spec": spec})
    assert status == 200 and preview["ok"]
    status, saved = _call(url, "/api/save", token, {"agent": "demo", "spec": spec})
    assert status == 200 and saved["ok"]
    assert generate.load_spec(workspace.agents_dir / "demo").drift_cron == ""
    assert _call(url, "/api/save", token, {"agent": "../demo", "spec": spec})[0] == 404
    assert _call(url, "/api/save", token, {"agent": "demo"})[0] == 400
