"""Generate an agent's release workflows and pipeline settings from its pipeline.yaml.

pipeline.yaml (in the agent folder) describes the pipeline: whether there is a test/UAT environment, which optional
gates run, who signs off, the residency preset and the schedules. This writes
  .github/workflows/<agent>-release.yml, -rollback-prod.yml, -prod-drift-check.yml, -harvest-traces.yml and,
  for prompt agents, -promote-portal-agent.yml
and the pipeline-owned keys of governance.json (sign-offs) and policy.json (preset, enforcement, allowed regions).
Everything else in those files, the eval/*.json pass bars and the test cases stay hand-edited.
G1 runs `--check`, so a generated file that no longer matches pipeline.yaml fails the build.

    python -m agent_cicd.generate                          # every agent folder that has a pipeline.yaml
    python -m agent_cicd.generate --agent-dir agents/x     # one agent
    python -m agent_cicd.generate --plan --agent-dir agents/x   # print the stages and gates; write nothing
    python -m agent_cicd.generate --check                  # CI: list what is out of date and fail
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jinja2
import yaml

from .definition import DefinitionError, agent_kind
from .policy_gate import PRESETS_DIR

CICD_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = CICD_DIR.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
SPEC_FILE = "pipeline.yaml"
WORKFLOWS = ("release", "rollback-prod", "prod-drift-check", "harvest-traces", "promote-portal-agent")
RESERVED_ENVIRONMENTS = {"dev", "test", "prod", "prod-monitor"}
REGION_RULES = ("model_region", "data_store_region")

_AGENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_ROLE_ID = re.compile(r"^[a-z][a-z0-9-]{0,29}$")
_ENVIRONMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
# Titles end up in job names and echo commands, so no quotes, $, backticks, colons or #.
_TITLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ()&/.,'+-]{0,59}$")
_CRON = re.compile(r"^[0-9*/,-]+( [0-9*/,-]+){4}$")
_REGION = re.compile(r"^[a-z0-9]+$")

SCHEMA = {
    "": {"version", "environments", "gates", "residency", "prod", "harvest"},
    "environments": {"test"},
    "gates": {"redteam", "signoffs", "require_distinct_approvers", "max_signoff_age_days"},
    "signoff": {"id", "title", "environment", "checks", "uat"},
    "residency": {"preset", "enforcement", "allowed_regions"},
    "prod": {"rollback_on_smoke_failure", "drift_check"},
    "harvest": {"schedule"},
}


class SpecError(ValueError):
    """pipeline.yaml is invalid."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or [message]


@dataclass
class Spec:
    agent: str
    kind: str
    test: bool = True
    redteam: bool = True
    signoffs: list[dict[str, Any]] = field(default_factory=list)
    require_distinct_approvers: bool = True
    max_signoff_age_days: int = 14
    preset: str = "permissive"
    enforcement: str = "warn"
    allowed_regions: list[str] = field(default_factory=list)
    rollback_on_smoke_failure: bool = True
    drift_cron: str = ""
    harvest_cron: str = ""

    @property
    def uat_role(self) -> dict[str, Any] | None:
        return next((r for r in self.signoffs if r.get("uat")), None)


# ---------------------------------------------------------------- loading and validation

def _section(data: dict[str, Any], key: str, problems: list[str]) -> dict[str, Any]:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        problems.append(f"'{key}' must be a mapping")
        return {}
    unknown = sorted(set(value) - SCHEMA[key])
    if unknown:
        problems.append(f"{key}: unknown setting(s) {', '.join(unknown)} (known: {', '.join(sorted(SCHEMA[key]))})")
    return value


def _bool(section: dict[str, Any], key: str, default: bool, where: str, problems: list[str]) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        problems.append(f"{where}.{key} must be true or false")
        return default
    return value


def _cron(section: dict[str, Any], key: str, where: str, problems: list[str]) -> str:
    value = section.get(key) or ""
    if not isinstance(value, str) or (value and not _CRON.match(value.strip())):
        problems.append(f"{where}.{key} must be a 5-field cron expression in UTC (for example \"17 6 * * *\") or empty")
        return ""
    return value.strip()


def _preset_regions(preset: str) -> dict[str, dict[str, Any]]:
    rules = json.loads((PRESETS_DIR / f"{preset}.json").read_text(encoding="utf-8")).get("rules") or {}
    return {name: rules.get(name) or {} for name in REGION_RULES}


def load_spec(agent_dir: Path) -> Spec:
    path = agent_dir / SPEC_FILE
    if not path.is_file():
        raise SpecError(f"Missing {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise SpecError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError(f"{path}: must be a mapping")
    problems: list[str] = []
    try:
        kind = agent_kind(agent_dir)
    except (DefinitionError, OSError, ValueError) as exc:
        problems.append(str(exc))
        kind = "prompt"
    return parse_spec(data, agent_dir.resolve().name, kind, str(path), problems)


def parse_spec(data: dict[str, Any], agent: str, kind: str, source: str = SPEC_FILE,
               problems: list[str] | None = None) -> Spec:
    """Validate pipeline.yaml settings (already parsed) for one agent. Used by the CLI and the builder UI."""
    problems = list(problems or [])
    if not isinstance(data, dict):
        raise SpecError(f"{source}: must be a mapping")
    if not _AGENT.match(agent):
        problems.append(f"agent folder name '{agent}' must be lowercase letters, digits and dashes "
                        "(it names the workflows, release tags and concurrency groups)")
    unknown = sorted(set(data) - SCHEMA[""])
    if unknown:
        problems.append(f"unknown setting(s) {', '.join(unknown)} (known: {', '.join(sorted(SCHEMA['']))})")
    if data.get("version") != 1:
        problems.append("version must be 1")

    envs = _section(data, "environments", problems)
    gates = _section(data, "gates", problems)
    residency = _section(data, "residency", problems)
    prod = _section(data, "prod", problems)
    harvest = _section(data, "harvest", problems)

    spec = Spec(agent=agent, kind=kind)
    spec.test = _bool(envs, "test", True, "environments", problems)
    spec.redteam = _bool(gates, "redteam", True, "gates", problems)
    spec.require_distinct_approvers = _bool(gates, "require_distinct_approvers", True, "gates", problems)
    age = gates.get("max_signoff_age_days", 14)
    if isinstance(age, bool) or not isinstance(age, int) or not 1 <= age <= 90:
        problems.append("gates.max_signoff_age_days must be a whole number of days from 1 to 90")
    else:
        spec.max_signoff_age_days = age

    roles = gates.get("signoffs")
    if not isinstance(roles, list) or not roles:
        problems.append("gates.signoffs must list at least one sign-off role (the release manager on prod is separate)")
        roles = []
    seen_ids: set[str] = set()
    seen_envs: set[str] = set()
    for i, role in enumerate(roles, 1):
        where = f"gates.signoffs[{i}]"
        if not isinstance(role, dict):
            problems.append(f"{where} must be a mapping with id, title and environment")
            continue
        unknown = sorted(set(role) - SCHEMA["signoff"])
        if unknown:
            problems.append(f"{where}: unknown setting(s) {', '.join(unknown)}")
        rid, title, env = role.get("id"), role.get("title"), role.get("environment")
        if not isinstance(rid, str) or not _ROLE_ID.match(rid):
            problems.append(f"{where}.id must be lowercase letters, digits and dashes, starting with a letter")
        elif rid in seen_ids:
            problems.append(f"{where}.id '{rid}' is used twice")
        if not isinstance(title, str) or not _TITLE.match(title):
            problems.append(f"{where}.title must be up to 60 letters, digits, spaces and ()&/.,'+- "
                            "(no quotes, colons, $ or #)")
        if not isinstance(env, str) or not _ENVIRONMENT.match(env):
            problems.append(f"{where}.environment must be a GitHub environment name (letters, digits, . _ -)")
        elif env in RESERVED_ENVIRONMENTS:
            problems.append(f"{where}.environment '{env}' is a deployment environment; give each sign-off its own")
        elif env.lower() in seen_envs:
            problems.append(f"{where}.environment '{env}' is used by another role; each role needs its own")
        checks = role.get("checks", "")
        if not isinstance(checks, str) or not checks.isprintable() or len(checks) > 300:
            problems.append(f"{where}.checks must be one line of printable text, up to 300 characters")
        uat = role.get("uat", False)
        if not isinstance(uat, bool):
            problems.append(f"{where}.uat must be true or false")
        seen_ids.add(str(rid))
        seen_envs.add(str(env).lower())
        entry = {"id": rid, "title": title, "environment": env}
        if checks:
            entry["checks"] = checks
        if uat is True:
            entry["uat"] = True
        spec.signoffs.append(entry)
    uat_roles = [r for r in spec.signoffs if r.get("uat")]
    if len(uat_roles) > 1:
        problems.append("only one sign-off role can have uat: true (the UAT evidence check runs after it)")
    if uat_roles and not spec.test:
        problems.append("uat: true needs environments.test: true (testers rate answers in the test project)")

    presets = sorted(p.stem for p in PRESETS_DIR.glob("*.json"))
    spec.preset = residency.get("preset", "permissive")
    if spec.preset not in presets:
        problems.append(f"residency.preset must be one of {', '.join(presets)}")
    spec.enforcement = residency.get("enforcement", "warn")
    if spec.enforcement not in ("warn", "enforce"):
        problems.append("residency.enforcement must be 'warn' (report only) or 'enforce' (block the deploy)")
    regions = residency.get("allowed_regions") or []
    if not isinstance(regions, list) or not all(isinstance(r, str) and _REGION.match(r) for r in regions):
        problems.append("residency.allowed_regions must be a list of Azure region names such as canadacentral")
    elif regions and spec.preset in presets:
        for rule, settings in _preset_regions(spec.preset).items():
            if settings.get("severity", "block") == "off":
                problems.append(f"residency.allowed_regions needs a preset that checks regions "
                                f"('{spec.preset}' turns {rule} off)")
                break
            outside = sorted(set(regions) - set(settings.get("allowed_regions") or []))
            if outside:
                problems.append(f"residency.allowed_regions can only narrow the '{spec.preset}' preset; "
                                f"not in its {rule}: {', '.join(outside)}")
                break
        spec.allowed_regions = list(regions)

    spec.rollback_on_smoke_failure = _bool(prod, "rollback_on_smoke_failure", True, "prod", problems)
    spec.drift_cron = _cron(prod, "drift_check", "prod", problems)
    spec.harvest_cron = _cron(harvest, "schedule", "harvest", problems)
    if problems:
        raise SpecError(f"{source}:\n  - " + "\n  - ".join(problems), problems)
    return spec


def presets() -> dict[str, dict[str, Any]]:
    """Residency presets: description and the regions a pipeline.yaml may narrow to (empty: no region rules)."""
    out = {}
    for path in sorted(PRESETS_DIR.glob("*.json")):
        preset = json.loads(path.read_text(encoding="utf-8"))
        rules = [preset.get("rules", {}).get(r) or {} for r in REGION_RULES]
        regions: list[str] = []
        if all(r.get("severity", "block") != "off" for r in rules):
            allowed = [set(r.get("allowed_regions") or []) for r in rules]
            regions = sorted(set.intersection(*allowed))
        out[path.stem] = {"description": preset.get("description", ""), "regions": regions}
    return out


DEFAULT_SPEC: dict[str, Any] = {
    "version": 1,
    "environments": {"test": True},
    "gates": {
        "redteam": True,
        "signoffs": [
            {"id": "qa", "title": "QA", "environment": "signoff-qa",
             "checks": "Evaluation and red-team results reviewed; failures understood"},
            {"id": "business", "title": "Business owner (UAT)", "environment": "signoff-business",
             "checks": "Users tried the release candidate in the test project and rated answers", "uat": True},
            {"id": "rai", "title": "Responsible AI reviewer", "environment": "signoff-rai",
             "checks": "RAI scorecard, residency findings and privacy results accepted"},
        ],
        "require_distinct_approvers": True,
        "max_signoff_age_days": 14,
    },
    "residency": {"preset": "permissive", "enforcement": "warn", "allowed_regions": []},
    "prod": {"rollback_on_smoke_failure": True, "drift_check": "17 6 * * *"},
    "harvest": {"schedule": ""},
}


def spec_data(spec: Spec) -> dict[str, Any]:
    """The pipeline.yaml settings of a validated spec, in canonical form."""
    return {
        "version": 1,
        "environments": {"test": spec.test},
        "gates": {"redteam": spec.redteam, "signoffs": [dict(r) for r in spec.signoffs],
                  "require_distinct_approvers": spec.require_distinct_approvers,
                  "max_signoff_age_days": spec.max_signoff_age_days},
        "residency": {"preset": spec.preset, "enforcement": spec.enforcement,
                      "allowed_regions": list(spec.allowed_regions)},
        "prod": {"rollback_on_smoke_failure": spec.rollback_on_smoke_failure, "drift_check": spec.drift_cron},
        "harvest": {"schedule": spec.harvest_cron},
    }


def _y(value: Any) -> str:
    # JSON scalars and flow lists are valid YAML, and quoting every string keeps titles and crons unambiguous.
    return json.dumps(value, ensure_ascii=False)


def dump_spec(spec: Spec) -> str:
    """Canonical, commented pipeline.yaml. The builder writes this; hand edits are still accepted."""
    roles = []
    for role in spec.signoffs:
        roles.append(f"    - id: {_y(role['id'])}")
        roles.append(f"      title: {_y(role['title'])}")
        roles.append(f"      environment: {_y(role['environment'])}")
        if role.get("checks"):
            roles.append(f"      checks: {_y(role['checks'])}")
        if role.get("uat"):
            roles.append("      uat: true")
    return "\n".join([
        "# Release pipeline for this agent: what a change goes through between a commit and prod.",
        "# Edit it with the pipeline builder (python -m agent_cicd.builder) or by hand, then run from cicd/:",
        "#   python -m agent_cicd.generate",
        "# That rewrites this agent's workflows (.github/workflows/<agent>-*.yml) and the sign-off and residency",
        "# settings in governance.json and policy.json. CI fails if those files no longer match this one.",
        "# Preview without writing anything:  python -m agent_cicd.generate --plan --agent-dir agents/<agent>",
        "#",
        "# Always on: G1 code & security, G2 config & residency before every deploy, G3 evaluation in dev,",
        "# and the release manager's approval of the `prod` environment (G6). Pass bars and test cases stay in eval/.",
        "version: 1",
        "",
        "environments:",
        "  # dev and prod always exist. test is a separate Foundry project for the release candidate, red teaming and UAT.",
        "  # false: releases go dev -> prod, red teaming (if on) runs in dev, and there is no UAT.",
        f"  test: {_y(spec.test)}",
        "",
        "gates:",
        "  # G4: AI red teaming of the release candidate (eval/redteam.json). false: the approval packet shows it as off.",
        f"  redteam: {_y(spec.redteam)}",
        "  # G5: who signs off before the release manager. Each role approves its own GitHub environment",
        "  # (create it with the role's people as required reviewers). Roles appear in this order in governance.json.",
        "  # uat: true marks the role that confirms UAT is done; the UAT evidence check (eval/uat.json: testers rated",
        "  # enough answers of the release candidate) runs after it. Needs environments.test. Remove it to skip UAT.",
        "  signoffs:",
        *roles,
        "  # false only for a solo trial where one person holds every role",
        f"  require_distinct_approvers: {_y(spec.require_distinct_approvers)}",
        "  # G6 rejects sign-offs older than this",
        f"  max_signoff_age_days: {_y(spec.max_signoff_age_days)}",
        "",
        "residency:",
        f"  # G2 rules: {', '.join(presets())} (cicd/policies/presets)",
        f"  preset: {_y(spec.preset)}",
        "  # warn: report would-block findings only; enforce: block the deploy. Keep warn until the resources comply.",
        f"  enforcement: {_y(spec.enforcement)}",
        "  # Optional: narrow the preset's allowed regions for models and data stores, e.g. [\"canadacentral\"]",
        f"  allowed_regions: {_y(list(spec.allowed_regions))}",
        "",
        "prod:",
        "  # Restore the previous prod version automatically when the new one fails its smoke checks",
        f"  rollback_on_smoke_failure: {_y(spec.rollback_on_smoke_failure)}",
        "  # Drift check schedule (cron, UTC); scheduled runs also need the repository variable",
        "  # ENABLE_PROD_DRIFT_CHECK=true. Empty: on demand only.",
        f"  drift_check: {_y(spec.drift_cron)}",
        "",
        "harvest:",
        "  # Real conversations -> candidate test cases (cron, UTC), e.g. \"0 11 * * 1\" for Mondays 11:00.",
        "  # Empty: run it by hand until the agent has real users.",
        f"  schedule: {_y(spec.harvest_cron)}",
        "",
    ])


# ---------------------------------------------------------------- rendering

def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _jinja() -> jinja2.Environment:
    # Workflows contain ${{ }} everywhere, so templates use {= =} for values.
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)), undefined=jinja2.StrictUndefined,
        variable_start_string="{=", variable_end_string="=}", trim_blocks=True, lstrip_blocks=True,
        keep_trailing_newline=True, autoescape=False)


def template_context(spec: Spec) -> dict[str, Any]:
    tag_prefix = f"{spec.agent}-v"
    tag_if = f"startsWith(github.ref, 'refs/tags/{tag_prefix}')"
    uat = spec.uat_role
    candidate_job = "deploy-test" if spec.test else "deploy-dev"
    envs = ["dev", "test", "prod"] if spec.test else ["dev", "prod"]
    plain_roles = [r for r in spec.signoffs if not r.get("uat")]
    passed = (["test"] if spec.test else ["evaluation in dev"]) + (["red teaming"] if spec.redteam else []) \
        + (["UAT"] if uat else [])
    artifacts = ["quality-gate-report"] + (["redteam-gate-report"] if spec.redteam else []) \
        + [f"policy-report-{e}" for e in envs if e != "prod"]
    return {
        "agent": spec.agent, "tag_prefix": tag_prefix, "test": spec.test, "redteam": spec.redteam, "uat": uat,
        "flow": " -> ".join(["dev"] + (["test/UAT" if uat else "test"] if spec.test else []) + ["prod"]),
        "promoted_to": "test and prod" if spec.test else "prod",
        "env_list": ", ".join(envs), "env_words": " ".join(envs),
        "redteam_env": "test" if spec.test else "dev", "candidate_job": candidate_job,
        "candidate_if": tag_if + (" || github.event.inputs.force_redteam == 'true'" if spec.redteam else ""),
        "force_redteam_help": ("Deploy to test, red-team it and build an approval packet"
                               if spec.test else "Red-team the dev version and build an approval packet")
                              + " (no sign-off or prod without a release tag)",
        "signoff_titles": _join([r["title"] for r in spec.signoffs]),
        "signoff_jobs": plain_roles + ([uat] if uat else []),
        "packet_needs": ", ".join(["deploy-dev", "quality-gate"] + (["deploy-test"] if spec.test else [])
                                  + (["redteam-gate"] if spec.redteam else [])),
        "packet_artifacts": "{" + ",".join(artifacts) + "}",
        "redteam_report_arg": "--redteam in/redteam.json" if spec.redteam else "--no-redteam",
        "policy_report_args": " ".join(f"--policy-report {e}=in/policy-{e}.json" for e in envs if e != "prod"),
        "decision_needs": ", ".join([f"signoff-{r['id']}" for r in plain_roles] + (["uat-evidence"] if uat else [])),
        "prod_image_note": _join(passed),
        "rollback_on_smoke_failure": spec.rollback_on_smoke_failure,
        "drift_cron": spec.drift_cron, "harvest_cron": spec.harvest_cron,
    }


def workflow_path(agent: str, workflow: str) -> Path:
    return WORKFLOWS_DIR / f"{agent}-{workflow}.yml"


def render_workflows(spec: Spec) -> dict[Path, str]:
    env, context = _jinja(), template_context(spec)
    names = [w for w in WORKFLOWS if w != "promote-portal-agent" or spec.kind == "prompt"]
    return {workflow_path(spec.agent, w): env.get_template(f"{w}.yml.j2").render(context) for w in names}


def governance_settings(spec: Spec, current: dict[str, Any]) -> dict[str, Any]:
    desired = copy.deepcopy(current)
    desired["signoffs"] = {r["id"]: {k: r[k] for k in ("title", "environment", "checks") if k in r}
                           for r in spec.signoffs}
    desired["require_distinct_approvers"] = spec.require_distinct_approvers
    desired["max_signoff_age_days"] = spec.max_signoff_age_days
    return desired


def policy_settings(spec: Spec, current: dict[str, Any]) -> dict[str, Any]:
    desired = copy.deepcopy(current)
    desired["preset"] = spec.preset
    desired["enforcement"] = spec.enforcement
    rules = desired.get("rules") or {}
    for rule in REGION_RULES:
        override = dict(rules.get(rule) or {})
        if spec.allowed_regions:
            override["allowed_regions"] = list(spec.allowed_regions)
        else:
            override.pop("allowed_regions", None)
        if override:
            rules[rule] = override
        else:
            rules.pop(rule, None)
    if rules or "rules" in desired:
        desired["rules"] = rules
    return desired


# ---------------------------------------------------------------- planning and writing

@dataclass
class Change:
    path: Path
    action: str  # "write" | "delete"
    content: str = ""


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _same_text(path: Path, content: str) -> bool:
    return path.is_file() and path.read_text(encoding="utf-8").replace("\r\n", "\n") == content


def changes_for(agent_dir: Path, spec: Spec | None = None) -> list[Change]:
    """Files that must change for the agent's generated files to match its pipeline.yaml."""
    spec = spec or load_spec(agent_dir)
    changes: list[Change] = []
    rendered = render_workflows(spec)
    for path, content in rendered.items():
        if not _same_text(path, content):
            changes.append(Change(path, "write", content))
    for workflow in WORKFLOWS:
        path = workflow_path(spec.agent, workflow)
        if path not in rendered and path.is_file():
            changes.append(Change(path, "delete"))
    for name, build in (("governance.json", governance_settings), ("policy.json", policy_settings)):
        path = agent_dir / name
        current = _read_json(path)
        desired = build(spec, current)
        # Compare meaning, not formatting, so hand-formatted files are only rewritten when a setting changes.
        if desired != current:
            changes.append(Change(path, "write", json.dumps(desired, indent=2, ensure_ascii=False) + "\n"))
    return changes


def apply(changes: list[Change]) -> None:
    for change in changes:
        if change.action == "delete":
            change.path.unlink()
        else:
            change.path.parent.mkdir(parents=True, exist_ok=True)
            change.path.write_text(change.content, encoding="utf-8", newline="\n")


def plan_model(spec: Spec) -> list[dict[str, Any]]:
    """The pipeline as lanes of steps, for the builder's diagram. Optional steps stay in the list with on=False,
    and `toggle` names the pipeline.yaml setting that turns them on or off."""
    uat = spec.uat_role
    hosted = spec.kind == "hosted"

    def step(key, title, detail, kind, on=True, toggle=None, **extra):
        return {"key": key, "title": title, "detail": detail, "kind": kind, "on": on, "toggle": toggle, **extra}

    release = [
        step("deploy-test", "Deploy test", "G2 residency + smoke checks in the test/UAT project", "deploy",
             spec.test, "environments.test"),
        step("redteam", "G4 AI red teaming", f"Release candidate attacked in {'test' if spec.test else 'dev'}",
             "gate", spec.redteam, "gates.redteam"),
        step("packet", "Approval packet", "Every gate, residency findings, Responsible AI scorecard", "info"),
        step("signoffs", "G5 sign-offs", "Each role approves its own GitHub environment", "human",
             roles=[{"title": r["title"], "environment": r["environment"], "uat": bool(r.get("uat"))}
                    for r in spec.signoffs]),
        step("uat", "UAT evidence", (f"After {uat['title']}: testers rated enough answers in test" if uat
                                     else "No role is marked uat: true"), "gate", bool(uat)),
        step("decision", "G6 release decision",
             f"All sign-offs present, under {spec.max_signoff_age_days} days old"
             + (", by different people" if spec.require_distinct_approvers else ""), "gate"),
        step("approve", "Release manager", "Approves the prod environment", "human"),
        step("deploy-prod", "Deploy prod", "G2 + smoke checks" + ("; restores the previous version if they fail"
                                                                  if spec.rollback_on_smoke_failure else ""),
             "deploy", rollback=spec.rollback_on_smoke_failure),
    ]
    regions = f" in {', '.join(spec.allowed_regions)}" if spec.allowed_regions else ""
    return [
        {"lane": "Pull request", "steps": [
            step("g1", "G1 code & security", "Secret and dependency scans, unit tests, rule files"
                 + ("; image build + vulnerability scan" if hosted else ""), "gate")]},
        {"lane": "Merge to main", "steps": [
            step("deploy-dev", "Deploy dev", f"G2 residency ({spec.preset}, {spec.enforcement}{regions}) + smoke",
                 "deploy"),
            step("g3", "G3 evaluation", "Quality and Responsible AI checks on the dev version", "gate")]},
        {"lane": f"Release tag {spec.agent}-v1.2.3", "steps": release},
        {"lane": "After release", "steps": [
            step("drift", "Drift check", spec.drift_cron or "On demand only", "info", bool(spec.drift_cron)),
            step("rollback", "Rollback", "On demand; release manager approves", "info"),
            step("harvest", "Trace harvest", spec.harvest_cron or "On demand only", "info", bool(spec.harvest_cron)),
            step("promote", "Promote portal version", "Prompt agents only" if hosted else "On demand",
                 "info", not hosted)]},
    ]


def describe(spec: Spec) -> str:
    """Plain-language outline of the pipeline (what a release goes through)."""
    uat = spec.uat_role
    signoffs = ", ".join(r["title"] + (" (then UAT evidence)" if r.get("uat") else "") for r in spec.signoffs)
    release = []
    if spec.test:
        release.append("deploy test (G2 + smoke)")
    if spec.redteam:
        release.append(f"G4 red teaming in {'test' if spec.test else 'dev'}")
    release += ["approval packet", f"G5 sign-offs: {signoffs}", "G6 release decision",
                "release manager approves prod",
                "deploy prod (G2 + smoke" + (", automatic rollback)" if spec.rollback_on_smoke_failure else ")")]
    regions = f", regions {', '.join(spec.allowed_regions)}" if spec.allowed_regions else ""
    lines = [
        f"{spec.agent} ({spec.kind} agent): dev -> " + ("test/UAT -> " if uat else "test -> " if spec.test else "")
        + "prod",
        "  pull request / push   G1 code & security"
        + (" (+ image build and scan)" if spec.kind == "hosted" else ""),
        "  push to main          deploy dev (G2 + smoke) -> G3 evaluation",
        f"  release tag {spec.agent}-v*",
        *(f"    -> {step}" for step in release),
        "  on demand             rollback prod; harvest traces"
        + ("; promote a portal version" if spec.kind == "prompt" else ""),
        f"  schedules             drift check {spec.drift_cron or 'off (on demand only)'}; "
        f"trace harvest {spec.harvest_cron or 'off (on demand only)'}",
        f"  residency (G2)        {spec.preset} preset, {spec.enforcement}{regions}",
        f"  approvers             distinct people: {'yes' if spec.require_distinct_approvers else 'no'}; "
        f"sign-offs expire after {spec.max_signoff_age_days} days",
    ]
    return "\n".join(lines)


def agent_dirs() -> list[Path]:
    return sorted(p.parent for p in (CICD_DIR / "agents").glob(f"*/{SPEC_FILE}"))


def tag_conflicts(agents: set[str]) -> list[str]:
    """Release tags are <agent>-v<version>, so 'billing' would also release on 'billing-voice-v1.0.0'."""
    return [f"agent '{b}' starts with '{a}-v': {a}'s release, rollback and drift workflows would also act on "
            f"{b}'s tags. Rename one of the folders."
            for a in sorted(agents) for b in sorted(agents) if a != b and b.startswith(f"{a}-v")]


_GENERATED_FROM = re.compile(r"^# Generated from cicd/agents/([^/]+)/pipeline\.yaml ", re.MULTILINE)


def orphaned_workflows(agents: set[str]) -> list[Change]:
    """Generated workflows whose agent no longer has a pipeline.yaml (renamed or removed agent folder)."""
    changes = []
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        head = "".join(path.read_text(encoding="utf-8").splitlines(keepends=True)[:15])
        match = _GENERATED_FROM.search(head)
        if match and match.group(1) not in agents:
            changes.append(Change(path, "delete"))
    return changes


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-dir", type=Path, action="append",
                        help="agent folder (repeatable); default: every agent folder with a pipeline.yaml")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail if any generated file is out of date")
    mode.add_argument("--plan", action="store_true", help="print the pipeline outline; write nothing")
    args = parser.parse_args()

    dirs = args.agent_dir or agent_dirs()
    if not dirs:
        print(f"No agent folder under {_rel(CICD_DIR / 'agents')} has a {SPEC_FILE}.")
    try:
        specs = [(d, load_spec(d)) for d in dirs]
    except SpecError as exc:
        print(f"::error::{exc}" if args.check else str(exc), file=sys.stderr)
        return 1
    agents = [s.agent for _, s in specs]
    if len(set(agents)) != len(agents):
        print("The same agent folder is listed twice.", file=sys.stderr)
        return 1
    conflicts = tag_conflicts(set(agents) | {d.resolve().name for d in agent_dirs()})
    if conflicts:
        print("\n".join(conflicts), file=sys.stderr)
        return 1

    if args.plan:
        print("\n\n".join(describe(s) for _, s in specs))
        return 0
    stale = 0
    groups = [(f"{spec.agent}: generated files", agent_dir / SPEC_FILE, changes_for(agent_dir, spec))
              for agent_dir, spec in specs]
    if not args.agent_dir:
        groups.append(("no workflows left from renamed or removed agents", None, orphaned_workflows(set(agents))))
    for label, source, changes in groups:
        stale += len(changes)
        if args.check:
            for c in changes:
                reason = (f"does not match {_rel(source)}" if source else "was generated for an agent folder "
                          "that has no pipeline.yaml any more") + (" (should not exist)" if c.action == "delete" else "")
                print(f"::error file={_rel(c.path)}::{_rel(c.path)} {reason}. "
                      "Run `python -m agent_cicd.generate` in cicd/ and commit the result.")
            if not changes:
                print(f"{label}: OK")
        else:
            apply(changes)
            for c in changes:
                print(f"{'deleted' if c.action == 'delete' else 'wrote'} {_rel(c.path)}")
            if not changes:
                print(f"{label}: already up to date")
    return 1 if args.check and stale else 0


if __name__ == "__main__":
    sys.exit(main())
