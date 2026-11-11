"""Prod drift check: is prod still running exactly what the last release approved?

Compares the latest prod agent version with the files at the checked-out ref (the workflow checks out the
newest release tag) and flags:
  * a version created outside the pipeline (someone edited the agent in the portal or with the SDK),
  * a rollback (prod went back to an earlier version after a failed release),
  * a definition that differs from the released files,
  * a model deployment upgraded since the release (same agent, different model version),
  * (hosted agents) a container image other than the one the pipeline deployed. The image digest is not in
    the repo files, so it is compared with the digest the pipeline recorded on the version.

Example:
  python -m agent_cicd.drift --agent-dir agents/my-agent --env prod --report out/drift.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .definition import fingerprint, image_digest, render_agent
from .deploy import _model_version
from .foundry import add_summary, project_client, set_outputs, write_report
from .promote import _canonical, _definition_only


def find_drift(expected: dict[str, Any], expected_model_version: str, live: dict[str, Any]) -> list[str]:
    """Pure comparison; `live` is the latest version as returned by Foundry (as_dict)."""
    findings = []
    metadata = live.get("metadata") or {}
    live_definition = live.get("definition") or {}
    if metadata.get("deployed_by") != "agent-cicd":
        findings.append(f"Version {live.get('version')} was not created by the pipeline "
                        "(edited in the Foundry portal or with the SDK)")
    elif metadata.get("rollback_of"):
        findings.append(f"Version {live.get('version')} is a rollback to version {metadata['rollback_of']}: prod does "
                        "not run the latest release")
    if expected.get("kind") == "hosted" and "container_configuration" not in expected:
        live_digest = image_digest(live_definition)
        if not metadata.get("image_digest"):
            findings.append(f"Version {live.get('version')} has no recorded image digest, so its image can't be "
                            "matched to a release")
        elif live_digest != metadata["image_digest"]:
            findings.append(f"Version {live.get('version')} runs image {live_digest or 'unknown'}, not "
                            f"{metadata['image_digest']} that the pipeline deployed")
        if live_definition.get("container_configuration"):
            expected = {**expected, "container_configuration": live_definition["container_configuration"]}
    if _canonical(_definition_only(live_definition)) != _canonical(_definition_only(expected)):
        findings.append(f"Version {live.get('version')} definition differs from the released files")
    released_model = metadata.get("model_version")
    if released_model and released_model != expected_model_version:
        findings.append(f"Model deployment changed since release: released on {released_model}, "
                        f"now {expected_model_version}")
    elif metadata.get("fingerprint") and metadata["fingerprint"] != fingerprint(expected, expected_model_version)[:32] \
            and not findings:
        findings.append("Fingerprint differs from the released files")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-dir", type=Path, required=True)
    parser.add_argument("--env", default="prod")
    parser.add_argument("--ref", default="", help="Release tag being compared (for the report)")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    rendered = render_agent(args.agent_dir, args.env, require_configured=True)
    with project_client(rendered.project_endpoint) as client:
        model_version = _model_version(client, rendered.model_deployment)
        details = client.agents.get(agent_name=rendered.agent_name).as_dict()
        live = (details.get("versions") or {}).get("latest") or {}
    findings = find_drift(rendered.definition, model_version, live)
    status = "drift" if findings else "ok"
    lines = [f"### {'⚠️ Drift' if findings else '✅ No drift'}: {rendered.agent_name} in {args.env}", "",
             f"- Live version: {live.get('version')} (git {str((live.get('metadata') or {}).get('git_sha', '?'))[:7]})",
             f"- Compared with: {args.ref or 'checked-out files'}; model {model_version}"]
    lines += [f"- ❗ {f}" for f in findings]
    if findings:
        lines += ["", "Fix: release the intended change through the pipeline (or promote the portal edit to a PR "
                  "with the promote workflow), or roll prod back with `deploy.py --rollback-to`."]
    add_summary("\n".join(lines) + "\n")
    write_report(args.report, {"status": status, "findings": findings, "live_version": live.get("version"),
                               "ref": args.ref, "model_version": model_version})
    set_outputs(drift=status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
