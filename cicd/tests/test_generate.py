"""pipeline.yaml -> workflows and settings (agent_cicd.generate)."""
import json
import shutil
from pathlib import Path

import pytest
import yaml

from agent_cicd import generate
from agent_cicd.release_report import OFF, build_packet, render_html, render_markdown

CICD = Path(__file__).resolve().parent.parent
MY_AGENT = CICD / "agents" / "my-agent"


def _agent(tmp_path: Path, name: str = "demo", kind: str | None = None, **edits) -> Path:
    """Copy my-agent to a temporary folder and apply edits to its pipeline.yaml (dotted keys)."""
    agent_dir = tmp_path / "agents" / name
    shutil.copytree(MY_AGENT, agent_dir)
    spec = yaml.safe_load((agent_dir / "pipeline.yaml").read_text(encoding="utf-8"))
    for dotted, value in edits.items():
        node = spec
        *parents, last = dotted.split("__")
        for key in parents:
            node = node.setdefault(key, {})
        node[last] = value
    (agent_dir / "pipeline.yaml").write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    if kind:
        definition = json.loads((agent_dir / "agent.json").read_text(encoding="utf-8"))
        definition["kind"] = kind
        (agent_dir / "agent.json").write_text(json.dumps(definition), encoding="utf-8")
    return agent_dir


def _workflows(agent_dir: Path) -> dict[str, dict]:
    spec = generate.load_spec(agent_dir)
    return {path.name: yaml.safe_load(text) for path, text in generate.render_workflows(spec).items()}


def _release(agent_dir: Path) -> dict:
    return _workflows(agent_dir)[f"{agent_dir.name}-release.yml"]


def _check_needs(workflow: dict) -> None:
    jobs = workflow["jobs"]
    for name, job in jobs.items():
        needs = job.get("needs") or []
        for need in [needs] if isinstance(needs, str) else needs:
            assert need in jobs, f"{name} needs missing job {need}"


def _run(job: dict) -> str:
    return "\n".join(step.get("run", "") for step in job.get("steps", []))


def test_committed_files_match_every_pipeline_yaml():
    dirs = generate.agent_dirs()
    assert MY_AGENT in dirs
    for agent_dir in dirs:
        assert generate.changes_for(agent_dir) == [], f"run `python -m agent_cicd.generate` for {agent_dir.name}"
    assert generate.orphaned_workflows({d.name for d in dirs}) == []


def test_default_pipeline_has_every_gate(tmp_path):
    release = _release(_agent(tmp_path))
    _check_needs(release)
    jobs = release["jobs"]
    assert list(jobs) == ["ci", "deploy-dev", "quality-gate", "deploy-test", "redteam-gate", "release-packet",
                          "signoff-qa", "signoff-rai", "signoff-business", "uat-evidence", "release-decision",
                          "deploy-prod"]
    assert jobs["release-decision"]["needs"] == ["signoff-qa", "signoff-rai", "uat-evidence"]
    assert jobs["uat-evidence"]["needs"] == ["deploy-test", "signoff-business"]
    assert jobs["deploy-prod"]["needs"] == ["release-decision", "deploy-test"]
    assert jobs["deploy-prod"]["with"]["rollback_on_smoke_failure"] is True
    assert release[True]["push"]["tags"] == ["demo-v[0-9]*"]
    assert "force_redteam" in release[True]["workflow_dispatch"]["inputs"]
    assert "--redteam in/redteam.json" in _run(jobs["release-packet"])
    # release_decision dates each sign-off by the job whose name contains its environment
    for role in ("qa", "rai", "business"):
        assert f"signoff-{role}" in jobs[f"signoff-{role}"]["name"]


def test_no_test_environment_goes_dev_to_prod_and_red_teams_dev(tmp_path):
    agent_dir = _agent(tmp_path, environments__test=False,
                       gates__signoffs=[{"id": "qa", "title": "QA", "environment": "signoff-qa"}])
    release = _release(agent_dir)
    _check_needs(release)
    jobs = release["jobs"]
    assert "deploy-test" not in jobs and "uat-evidence" not in jobs
    assert jobs["redteam-gate"]["environment"] == "dev"
    assert jobs["redteam-gate"]["needs"] == ["deploy-dev", "quality-gate"]
    assert "refs/tags/demo-v" in jobs["redteam-gate"]["if"]
    assert "--env dev" in _run(jobs["redteam-gate"])
    assert jobs["deploy-prod"]["needs"] == ["release-decision", "deploy-dev"]
    assert jobs["deploy-prod"]["with"]["image"] == "${{ needs.deploy-dev.outputs.image }}"
    packet = _run(jobs["release-packet"])
    assert "--test-version" not in packet and "policy-test" not in packet
    assert "for env in dev prod;" in _run(jobs["ci"])


def test_red_teaming_off_marks_the_packet_instead_of_blocking(tmp_path):
    release = _release(_agent(tmp_path, gates__redteam=False))
    _check_needs(release)
    jobs = release["jobs"]
    assert "redteam-gate" not in jobs
    assert "force_redteam" not in release[True]["workflow_dispatch"]["inputs"]
    assert "--no-redteam" in _run(jobs["release-packet"])
    assert "force_redteam" not in jobs["deploy-test"]["if"]
    assert "redteam_gate" not in _run(jobs["ci"])


def test_no_uat_role_drops_uat_evidence(tmp_path):
    roles = [{"id": "qa", "title": "QA", "environment": "signoff-qa"},
             {"id": "security", "title": "Security", "environment": "signoff-security"}]
    agent_dir = _agent(tmp_path, gates__signoffs=roles, gates__require_distinct_approvers=False)
    release = _release(agent_dir)
    _check_needs(release)
    assert "uat-evidence" not in release["jobs"]
    assert release["jobs"]["release-decision"]["needs"] == ["signoff-qa", "signoff-security"]
    assert "uat_evidence" not in _run(release["jobs"]["ci"])
    spec = generate.load_spec(agent_dir)
    governance = generate.governance_settings(spec, {"release_manager_environment": "prod", "signoffs": {"x": {}}})
    assert governance == {"release_manager_environment": "prod",
                          "signoffs": {"qa": {"title": "QA", "environment": "signoff-qa"},
                                       "security": {"title": "Security", "environment": "signoff-security"}},
                          "require_distinct_approvers": False, "max_signoff_age_days": 14}


def test_schedules_and_rollback_switch(tmp_path):
    workflows = _workflows(_agent(tmp_path, prod__drift_check="", prod__rollback_on_smoke_failure=False,
                                  harvest__schedule="0 11 * * 1"))
    assert "schedule" not in workflows["demo-prod-drift-check.yml"][True]
    assert workflows["demo-harvest-traces.yml"][True]["schedule"] == [{"cron": "0 11 * * 1"}]
    assert "rollback_on_smoke_failure" not in workflows["demo-release.yml"]["jobs"]["deploy-prod"]["with"]


def test_hosted_agents_get_no_portal_promotion_and_stale_files_are_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(generate, "WORKFLOWS_DIR", tmp_path / "workflows")
    agent_dir = _agent(tmp_path)
    generate.apply(generate.changes_for(agent_dir))
    assert (tmp_path / "workflows" / "demo-promote-portal-agent.yml").is_file()
    assert generate.changes_for(agent_dir) == []
    definition = json.loads((agent_dir / "agent.json").read_text(encoding="utf-8"))
    definition.update({"kind": "hosted"})
    (agent_dir / "agent.json").write_text(json.dumps(definition), encoding="utf-8")
    changes = generate.changes_for(agent_dir)
    assert [(c.path.name, c.action) for c in changes] == [("demo-promote-portal-agent.yml", "delete")]


def test_check_detects_a_hand_edited_workflow(tmp_path, monkeypatch):
    monkeypatch.setattr(generate, "WORKFLOWS_DIR", tmp_path / "workflows")
    agent_dir = _agent(tmp_path)
    generate.apply(generate.changes_for(agent_dir))
    release = tmp_path / "workflows" / "demo-release.yml"
    release.write_text(release.read_text(encoding="utf-8").replace("timeout-minutes: 300", "timeout-minutes: 30"),
                       encoding="utf-8")
    assert [c.path.name for c in generate.changes_for(agent_dir)] == ["demo-release.yml"]


def test_crlf_working_copies_count_as_matching(tmp_path, monkeypatch):
    monkeypatch.setattr(generate, "WORKFLOWS_DIR", tmp_path / "workflows")
    agent_dir = _agent(tmp_path)
    generate.apply(generate.changes_for(agent_dir))
    for path in (tmp_path / "workflows").iterdir():
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert generate.changes_for(agent_dir) == []


def test_workflows_of_a_renamed_agent_are_found(tmp_path, monkeypatch):
    monkeypatch.setattr(generate, "WORKFLOWS_DIR", tmp_path / "workflows")
    generate.apply(generate.changes_for(_agent(tmp_path, name="old-name")))
    (tmp_path / "workflows" / "agent-deploy.yml").write_text("name: shared\n", encoding="utf-8")
    orphans = sorted(c.path.name for c in generate.orphaned_workflows({"new-name"}))
    assert orphans == [f"old-name-{w}.yml" for w in sorted(generate.WORKFLOWS)]
    assert generate.orphaned_workflows({"old-name"}) == []


def test_allowed_regions_narrow_a_preset_and_are_removed_again():
    spec = generate.Spec(agent="demo", kind="prompt", preset="canada-strict", enforcement="enforce",
                         allowed_regions=["canadacentral"])
    current = {"owner": "team", "preset": "permissive", "enforcement": "warn",
               "rules": {"model_region": {"severity": "warn"}}}
    policy = generate.policy_settings(spec, current)
    assert policy == {"owner": "team", "preset": "canada-strict", "enforcement": "enforce",
                      "rules": {"model_region": {"severity": "warn", "allowed_regions": ["canadacentral"]},
                                "data_store_region": {"allowed_regions": ["canadacentral"]}}}
    spec.allowed_regions = []
    assert generate.policy_settings(spec, policy)["rules"] == {"model_region": {"severity": "warn"}}


@pytest.mark.parametrize("edits, message", [
    ({"version": 2}, "version must be 1"),
    ({"stages": {}}, "unknown setting(s) stages"),
    ({"gates__redteam": "yes"}, "gates.redteam must be true or false"),
    ({"gates__signoffs": []}, "at least one sign-off role"),
    ({"gates__signoffs": [{"id": "qa", "title": "QA", "environment": "prod"}]}, "is a deployment environment"),
    ({"gates__signoffs": [{"id": "qa", "title": "QA: \"$(id)\"", "environment": "signoff-qa"}]}, "title must be"),
    ({"gates__signoffs": [{"id": "qa", "title": "QA", "environment": "s"},
                          {"id": "qa", "title": "QA2", "environment": "s"}]}, "is used twice"),
    ({"gates__signoffs": [{"id": "a", "title": "A", "environment": "s1", "uat": True},
                          {"id": "b", "title": "B", "environment": "s2", "uat": True}]}, "only one sign-off role"),
    ({"environments__test": False}, "uat: true needs environments.test"),
    ({"residency__preset": "mars"}, "residency.preset must be one of"),
    ({"residency__enforcement": "block"}, "residency.enforcement must be"),
    ({"residency__allowed_regions": ["canadacentral"]}, "needs a preset that checks regions"),
    ({"residency__preset": "canada-strict", "residency__allowed_regions": ["eastus"]}, "can only narrow"),
    ({"prod__drift_check": "daily"}, "5-field cron"),
    ({"gates__max_signoff_age_days": 0}, "max_signoff_age_days"),
])
def test_invalid_specs_are_rejected_with_a_clear_message(tmp_path, edits, message):
    agent_dir = _agent(tmp_path, **edits)
    with pytest.raises(generate.SpecError) as error:
        generate.load_spec(agent_dir)
    assert message in str(error.value)


def test_agent_folder_name_must_be_safe(tmp_path):
    with pytest.raises(generate.SpecError, match="agent folder name"):
        generate.load_spec(_agent(tmp_path, name="My Agent"))


def test_agent_names_whose_release_tags_overlap_are_rejected():
    assert generate.tag_conflicts({"billing", "billing-agent", "hr"}) == []
    conflicts = generate.tag_conflicts({"billing", "billing-voice", "hr"})
    assert len(conflicts) == 1 and "'billing-voice' starts with 'billing-v'" in conflicts[0]


def test_release_tags_need_a_version_number_and_drift_only_reads_its_own_tags(tmp_path):
    workflows = _workflows(_agent(tmp_path))
    assert workflows["demo-release.yml"][True]["push"]["tags"] == ["demo-v[0-9]*"]
    assert "refs/tags/demo-v[0-9]*)" in _run(workflows["demo-rollback-prod.yml"]["jobs"]["check"])
    assert 'startswith("demo-v")' in _run(workflows["demo-prod-drift-check.yml"]["jobs"]["drift"])


def test_packet_without_red_teaming_needs_no_redteam_json(tmp_path, monkeypatch):
    from agent_cicd import release_report
    agent_dir = _agent(tmp_path)
    (agent_dir / "eval" / "redteam.json").unlink()
    quality = tmp_path / "quality.json"
    quality.write_text(json.dumps({"result": {"outcome": "PASS", "reasons": [], "rows": [], "details": {}}}),
                       encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["release_report", "--agent-dir", str(agent_dir), "--quality", str(quality),
                                     "--no-redteam", "--markdown", str(tmp_path / "packet.md")])
    monkeypatch.delenv("TEAMS_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out.txt"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    assert release_report.main() == 0
    assert "ready=true" in (tmp_path / "out.txt").read_text(encoding="utf-8")


def test_plan_describes_the_pipeline(tmp_path):
    text = generate.describe(generate.load_spec(_agent(tmp_path, environments__test=False, gates__signoffs=[
        {"id": "qa", "title": "QA", "environment": "signoff-qa"}])))
    assert text.splitlines()[0] == "demo (prompt agent): dev -> prod"
    assert "G4 red teaming in dev" in text and "deploy test" not in text


def test_packet_without_red_teaming_is_ready_when_the_evaluation_passes():
    quality = {"result": {"outcome": "PASS", "reasons": ["ok"], "rows": [], "details": {}}}
    packet = build_packet(quality, None, {"criteria": {}}, {}, {"agent_name": "demo"}, redteam_required=False)
    assert packet["ready"] is True
    assert packet["redteam"]["outcome"] == OFF
    assert "➖ OFF" in render_markdown(packet)
    assert ">OFF<" in render_html(packet)
    assert build_packet(quality, None, {"criteria": {}}, {}, {"agent_name": "demo"})["ready"] is False
