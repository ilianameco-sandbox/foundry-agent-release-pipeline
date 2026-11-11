import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cicd import deploy as deploy_module
from agent_cicd.definition import DefinitionError, agent_kind, fingerprint, parse_image, render_agent
from agent_cicd.drift import find_drift
from agent_cicd.image import build_info, copy_to_registry, retarget
from agent_cicd.policy_gate import collect_facts
from agent_cicd.promote import promote

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "agents" / "my-hosted-agent"
PROMPT_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "agents" / "my-agent"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
DEV_ACR = "devacr.azurecr.io"
PROD_ACR = "prodacr.azurecr.io"
IMAGE = f"{DEV_ACR}/sample-hosted@{DIGEST}"
ENV = {"FOUNDRY_PROJECT_ENDPOINT": "https://acct.services.ai.azure.com/api/projects/dev"}


def make_agent(tmp_path, agent=None, config=None, src=True):
    root = tmp_path / "sample-hosted"
    (root / "config").mkdir(parents=True)
    body = {"kind": "hosted", "description": "Answers questions about sample documents.", "cpu": "1",
            "memory": "2Gi", "protocols": [{"protocol": "responses", "version": "1.0.0"}],
            "environment_variables": {"AZURE_AI_MODEL_DEPLOYMENT_NAME": "${MODEL_DEPLOYMENT}",
                                      "SEARCH_INDEX": "${SEARCH_INDEX}"}}
    body.update(agent or {})
    (root / "agent.json").write_text(json.dumps(body), encoding="utf-8")
    for env, registry in (("dev", DEV_ACR), ("prod", PROD_ACR)):
        cfg = {"project_endpoint": "${FOUNDRY_PROJECT_ENDPOINT}", "azure_resource_group": "rg-sample",
               "agent_name": "sample-hosted", "model_deployment": "gpt-4.1", "judge_deployment": "gpt-4.1-judge",
               "container_registry": registry, "variables": {"SEARCH_INDEX": f"docs-{env}"}}
        cfg.update(config or {})
        (root / "config" / f"{env}.json").write_text(json.dumps(cfg), encoding="utf-8")
    if src:
        (root / "src").mkdir()
        (root / "src" / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------- detection and rendering

def test_kind_is_detected_from_agent_json(tmp_path):
    assert agent_kind(make_agent(tmp_path)) == "hosted"
    assert agent_kind(PROMPT_TEMPLATE_DIR) == "prompt"
    assert build_info(PROMPT_TEMPLATE_DIR) == {"kind": "prompt", "build_context": "", "dockerfile": "",
                                               "repository": ""}


def test_hosted_agent_renders_without_an_image_for_the_gates(tmp_path):
    rendered = render_agent(make_agent(tmp_path), "dev", env=ENV, require_configured=True)
    assert rendered.kind == "hosted" and rendered.deployable
    assert rendered.model_deployment == "gpt-4.1" and rendered.container_registry == DEV_ACR
    assert rendered.definition == {
        "kind": "hosted", "cpu": "1", "memory": "2Gi",
        "protocol_versions": [{"protocol": "responses", "version": "1.0.0"}],
        "environment_variables": {"AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-4.1", "SEARCH_INDEX": "docs-dev"},
    }


def test_deploys_add_the_digest_pinned_image(tmp_path):
    rendered = render_agent(make_agent(tmp_path), "dev", env=ENV, image=IMAGE)
    assert rendered.definition["container_configuration"] == {"image": IMAGE}


@pytest.mark.parametrize("image", [f"{DEV_ACR}/sample-hosted:latest", f"{DEV_ACR}/sample-hosted:1.0",
                                   "sample-hosted@" + DIGEST, f"{DEV_ACR}/sample-hosted@sha256:abc"])
def test_tags_and_short_names_are_refused(tmp_path, image):
    with pytest.raises(DefinitionError, match="pinned by digest"):
        render_agent(make_agent(tmp_path), "dev", env=ENV, image=image)


def test_parse_image():
    assert parse_image(IMAGE) == {"registry": DEV_ACR, "repository": "sample-hosted", "digest": DIGEST}
    assert parse_image(f"{DEV_ACR}/team/agent@{DIGEST}")["repository"] == "team/agent"


@pytest.mark.parametrize("agent, message", [
    ({"kind": "container"}, "kind must be one of"),
    ({"cpu": ""}, "need 'cpu'"),
    ({"protocols": [{"protocol": "invocations", "version": "1.0.0"}]}, "need the 'responses' protocol"),
    ({"protocols": [{"protocol": "smtp", "version": "1"}]}, "protocol entries need"),
    ({"environment_variables": {"FOUNDRY_PROJECT_ENDPOINT": "x"}}, "set by Foundry"),
    ({"environment_variables": {"agent_name": "x"}}, "set by Foundry"),
    ({"environment_variables": {"INDEX": "${UNKNOWN}"}}, "missing variables: UNKNOWN"),
])
def test_invalid_hosted_definitions_are_rejected(tmp_path, agent, message):
    with pytest.raises(DefinitionError, match=message):
        render_agent(make_agent(tmp_path, agent=agent), "dev", env=ENV)


def test_hosted_agents_need_a_container_registry(tmp_path):
    with pytest.raises(DefinitionError, match="container_registry"):
        render_agent(make_agent(tmp_path, config={"container_registry": ""}), "dev", env=ENV)


def test_fingerprint_follows_the_digest_not_the_registry(tmp_path):
    root = make_agent(tmp_path, agent={"environment_variables": {}})
    dev = render_agent(root, "dev", env=ENV, image=IMAGE).definition
    prod = render_agent(root, "prod", env=ENV, image=retarget(IMAGE, PROD_ACR)).definition
    assert dev != prod
    assert fingerprint(dev, "gpt@1") == fingerprint(prod, "gpt@1")
    rebuilt = render_agent(root, "dev", env=ENV, image=f"{DEV_ACR}/sample-hosted@{OTHER_DIGEST}").definition
    assert fingerprint(rebuilt, "gpt@1") != fingerprint(dev, "gpt@1")


# ---------------------------------------------------------------- template

def test_hosted_template_is_valid_but_not_deployable_until_configured():
    from agent_cicd.policy_gate import load_policy
    from agent_cicd.release_decision import load_governance
    from agent_cicd.smoke import load_smoke

    load_policy(TEMPLATE_DIR)
    load_smoke(TEMPLATE_DIR)
    assert load_governance(TEMPLATE_DIR)["require_distinct_approvers"] is True
    for environment in ("dev", "test", "prod"):
        rendered = render_agent(TEMPLATE_DIR, environment, env={**ENV, "AZURE_CONTAINER_REGISTRY": DEV_ACR})
        assert rendered.kind == "hosted" and not rendered.deployable
        assert "agent_name" in rendered.placeholders
    info = build_info(TEMPLATE_DIR)
    assert info["kind"] == "hosted" and info["build_context"].endswith("my-hosted-agent/src")
    assert info["repository"] == "my-hosted-agent"
    with pytest.raises(DefinitionError, match="add the agent's code and Dockerfile"):
        build_info(TEMPLATE_DIR, require_source=True)


def test_build_info_points_at_the_dockerfile(tmp_path):
    root = make_agent(tmp_path, agent={"image_repository": "Team/Sample"})
    info = build_info(root, require_source=True)
    assert info["dockerfile"].endswith("sample-hosted/src/Dockerfile")
    assert info["repository"] == "team/sample"


# ---------------------------------------------------------------- promotion between registries

def test_same_registry_needs_no_copy():
    calls = []
    assert copy_to_registry(IMAGE, DEV_ACR, "abc", run=lambda *a, **k: calls.append(a)) == IMAGE
    assert calls == []


def test_other_registry_imports_the_same_digest():
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert copy_to_registry(IMAGE, PROD_ACR.upper(), "abc123", run=run) == f"{PROD_ACR}/sample-hosted@{DIGEST}"
    assert calls == [["az", "acr", "import", "--name", "prodacr", "--source", IMAGE,
                      "--image", "sample-hosted:abc123", "--force"]]


def test_failed_import_stops_the_deploy():
    def run(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="AuthorizationFailed")

    with pytest.raises(RuntimeError, match="AuthorizationFailed"):
        copy_to_registry(IMAGE, PROD_ACR, "abc", run=run)


# ---------------------------------------------------------------- deploy

class FakeAgents:
    def __init__(self, statuses=("creating", "active"), latest=None, versions=None):
        self.created = []
        self.statuses = list(statuses)
        self.latest = latest
        self.versions = versions or {}

    def get(self, agent_name):
        from azure.core.exceptions import ResourceNotFoundError
        if self.latest is None:
            raise ResourceNotFoundError("no agent yet")
        return SimpleNamespace(as_dict=lambda: {"versions": {"latest": self.latest}})

    def create_version(self, agent_name, definition, metadata):
        self.created.append((agent_name, definition, metadata))
        return SimpleNamespace(version=str(10 + len(self.created)))

    def get_version(self, agent_name, agent_version):
        if agent_version in self.versions:
            source = self.versions[agent_version]
            return SimpleNamespace(definition=source["definition"], as_dict=lambda: source)
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return SimpleNamespace(as_dict=lambda: {"version": agent_version, "status": status})


def fake_client(agents):
    deployment = SimpleNamespace(as_dict=lambda: {"modelName": "gpt-4.1", "modelVersion": "2025-04-14"})
    return SimpleNamespace(agents=agents, deployments=SimpleNamespace(get=lambda name: deployment))


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(deploy_module.time, "sleep", lambda seconds: None)


def test_hosted_dry_run_needs_no_image(tmp_path, capsys):
    assert deploy_module.deploy(make_agent(tmp_path), "dev", dry_run=True) == 0
    out = capsys.readouterr().out
    assert '"kind": "hosted"' in out and "rendered without its image" in out


def test_hosted_deploy_without_an_image_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", ENV["FOUNDRY_PROJECT_ENDPOINT"])
    with pytest.raises(DefinitionError, match="pass --image"):
        deploy_module.deploy(make_agent(tmp_path), "dev", dry_run=False)


def test_hosted_deploy_copies_the_image_creates_the_version_and_waits(tmp_path, monkeypatch, no_wait):
    from contextlib import contextmanager

    import agent_cicd.image as image_module

    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", ENV["FOUNDRY_PROJECT_ENDPOINT"])
    monkeypatch.setenv("GITHUB_SHA", "abc123")
    agents = FakeAgents(statuses=("creating", "creating", "active"))

    @contextmanager
    def client(endpoint):
        yield fake_client(agents)

    imports = []
    monkeypatch.setattr(deploy_module, "project_client", client)
    monkeypatch.setattr(image_module, "copy_to_registry",
                        lambda ref, registry, tag: imports.append((ref, registry, tag)) or retarget(ref, registry))
    assert deploy_module.deploy(make_agent(tmp_path), "prod", dry_run=False, image=IMAGE) == 0

    assert imports == [(IMAGE, PROD_ACR, "abc123")]
    name, definition, metadata = agents.created[0]
    assert name == "sample-hosted" and definition.__class__.__name__ == "HostedAgentDefinition"
    assert definition.as_dict()["container_configuration"] == {"image": f"{PROD_ACR}/sample-hosted@{DIGEST}"}
    assert metadata["image_digest"] == DIGEST and metadata["model_version"] == "gpt-4.1@2025-04-14"
    assert agents.statuses == ["active"]


def test_failed_hosted_version_fails_the_deploy(no_wait):
    agents = FakeAgents(statuses=("creating", "failed"))
    with pytest.raises(RuntimeError, match="is 'failed'"):
        deploy_module.wait_until_active(fake_client(agents), "sample-hosted", "3")


def test_hosted_version_that_never_starts_times_out(no_wait):
    agents = FakeAgents(statuses=("creating",))
    with pytest.raises(RuntimeError, match="not active after"):
        deploy_module.wait_until_active(fake_client(agents), "sample-hosted", "3", timeout_minutes=0)


@pytest.fixture
def hosted_deploy(tmp_path, monkeypatch, no_wait):
    """Deploy the sample hosted agent to dev against fake Foundry agents; returns (run, agents)."""
    from contextlib import contextmanager

    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", ENV["FOUNDRY_PROJECT_ENDPOINT"])
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    root = make_agent(tmp_path)
    state = {}

    @contextmanager
    def client(endpoint):
        yield fake_client(state["agents"])

    monkeypatch.setattr(deploy_module, "project_client", client)

    def run(agents):
        state["agents"] = agents
        return deploy_module.deploy(root, "dev", dry_run=False, image=IMAGE)
    return run, root


def _fingerprint_of_sample(root):
    definition = render_agent(root, "dev", env=ENV, image=IMAGE).definition
    return fingerprint(definition, "gpt-4.1@2025-04-14")[:32]


def test_a_hosted_version_that_never_became_active_is_replaced_not_reused(hosted_deploy):
    run, root = hosted_deploy
    same = {"deployed_by": "agent-cicd", "fingerprint": _fingerprint_of_sample(root)}
    healthy = FakeAgents(statuses=("active",), latest={"version": "4", "status": "active", "metadata": same})
    assert run(healthy) == 0 and healthy.created == []
    broken = FakeAgents(statuses=("active",), latest={"version": "4", "status": "failed", "metadata": same})
    assert run(broken) == 0 and len(broken.created) == 1


def test_previous_version_is_recorded_before_waiting_so_prod_can_roll_back(hosted_deploy, capsys):
    run, _ = hosted_deploy
    agents = FakeAgents(statuses=("creating", "failed"),
                        latest={"version": "4", "status": "active", "metadata": {"deployed_by": "agent-cicd"}})
    with pytest.raises(RuntimeError, match="is 'failed'"):
        run(agents)
    out = capsys.readouterr().out
    assert "agent_version=11" in out and "previous_version=4" in out


def test_rollback_keeps_the_source_versions_release_record(tmp_path, monkeypatch, no_wait):
    from contextlib import contextmanager

    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", ENV["FOUNDRY_PROJECT_ENDPOINT"])
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    source = {"version": "4", "definition": {"kind": "hosted", "container_configuration": {"image": IMAGE}},
              "metadata": {"deployed_by": "agent-cicd", "fingerprint": "f" * 32, "model_version": "gpt@1",
                           "image_digest": DIGEST, "git_sha": "old"}}
    agents = FakeAgents(statuses=("active",), versions={"4": source})

    @contextmanager
    def client(endpoint):
        yield fake_client(agents)

    monkeypatch.setattr(deploy_module, "project_client", client)
    assert deploy_module.rollback(make_agent(tmp_path), "dev", "4") == 0
    metadata = agents.created[0][2]
    assert metadata["rollback_of"] == "4" and metadata["image_digest"] == DIGEST
    assert metadata["fingerprint"] == "f" * 32 and metadata["model_version"] == "gpt@1"


@pytest.mark.parametrize("deployed_by, memory, error", [
    ("agent-cicd", "2Gi", None),
    (None, "2Gi", "was not created by the pipeline"),
    ("agent-cicd", "4Gi", "does not match the agent files"),
])
def test_manual_rollback_only_restores_the_release_it_was_started_from(tmp_path, monkeypatch, no_wait,
                                                                        deployed_by, memory, error):
    from contextlib import contextmanager

    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", ENV["FOUNDRY_PROJECT_ENDPOINT"])
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    root = make_agent(tmp_path)
    released_definition = render_agent(root, "prod", env=ENV, image=retarget(IMAGE, PROD_ACR)).definition
    source = {"version": "4", "definition": {**released_definition, "memory": memory},
              "metadata": {"deployed_by": deployed_by, "image_digest": DIGEST}}
    agents = FakeAgents(statuses=("active",), versions={"4": source})

    @contextmanager
    def client(endpoint):
        yield fake_client(agents)

    monkeypatch.setattr(deploy_module, "project_client", client)
    if error:
        with pytest.raises(DefinitionError, match=error):
            deploy_module.rollback(root, "prod", "4", reason="INC-1", require_released=True)
        assert agents.created == []
    else:
        assert deploy_module.rollback(root, "prod", "4", reason="INC-1", require_released=True) == 0
        assert agents.created[0][2]["rollback_reason"] == "INC-1"


# ---------------------------------------------------------------- drift

def released(tmp_path, image=IMAGE, deployed_by="agent-cicd", **changes):
    expected = render_agent(make_agent(tmp_path), "prod", env=ENV).definition
    shipped = render_agent(make_agent(tmp_path / "shipped"), "prod", env=ENV, image=IMAGE).definition
    live_definition = {**shipped, "container_configuration": {"image": image}, **changes}
    return expected, {"version": "9", "definition": live_definition,
                      "metadata": {"deployed_by": deployed_by, "model_version": "gpt@1", "image_digest": DIGEST,
                                   "fingerprint": fingerprint(shipped, "gpt@1")[:32]}}


def test_no_drift_when_prod_runs_the_released_image(tmp_path):
    expected, live = released(tmp_path)
    assert find_drift(expected, "gpt@1", live) == []


def test_swapped_image_is_drift(tmp_path):
    expected, live = released(tmp_path, image=f"{PROD_ACR}/sample-hosted@{OTHER_DIGEST}", deployed_by=None)
    findings = find_drift(expected, "gpt@1", live)
    assert any("not created by the pipeline" in f for f in findings)
    assert any(f"runs image {OTHER_DIGEST}" in f for f in findings)


def test_resized_container_is_drift(tmp_path):
    expected, live = released(tmp_path, memory="4Gi")
    assert find_drift(expected, "gpt@1", live) == ["Version 9 definition differs from the released files"]


def test_a_rollback_to_the_baseline_release_is_not_drift(tmp_path):
    expected, live = released(tmp_path)
    live["metadata"]["rollback_of"] = "7"
    assert find_drift(expected, "gpt@1", live) == []


def test_hosted_version_without_a_recorded_digest_is_drift(tmp_path):
    expected, live = released(tmp_path)
    del live["metadata"]["image_digest"]
    assert any("no recorded image digest" in f for f in find_drift(expected, "gpt@1", live))


# ---------------------------------------------------------------- residency and promote

def test_policy_gate_checks_the_registry_region():
    sub, rg = "sub-1", "rg-sample"
    account = f"/subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.CognitiveServices/accounts/acct"
    responses = {
        account: {"location": "canadaeast", "properties": {}},
        f"/subscriptions/{sub}/providers/Microsoft.ContainerRegistry/registries": {"value": [
            {"name": "other", "location": "eastus", "properties": {"loginServer": "other.azurecr.io"}},
            {"name": "devacr", "location": "canadacentral", "properties": {"loginServer": DEV_ACR}}]},
    }
    facts = collect_facts("https://acct.services.ai.azure.com/api/projects/proj", [], rg, sub,
                          lambda path, api: responses.get(path), container_registry=DEV_ACR)
    assert {"name": DEV_ACR, "kind": "container_registry", "location": "canadacentral"} in facts["data_stores"]
    unknown = collect_facts("https://acct.services.ai.azure.com/api/projects/proj", [], rg, sub,
                            lambda path, api: responses.get(path), container_registry="missing.azurecr.io")
    assert unknown["data_stores"][-1]["location"] is None


def test_promote_refuses_hosted_agents_without_calling_azure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", ENV["FOUNDRY_PROJECT_ENDPOINT"])
    monkeypatch.setattr("agent_cicd.promote.project_client", lambda endpoint: pytest.fail("called Azure"))
    assert promote(make_agent(tmp_path), "dev", "latest", None, dry_run=True, body_path=None) == 1
    assert "nothing to export from the portal" in capsys.readouterr().err
