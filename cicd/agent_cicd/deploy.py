"""Create a new agent version from code, record it, and (optionally) roll back.

Prompt agents are created from agent.json + instructions. Hosted agents are created from the container image
built in G1, passed as a digest reference with --image; the image is copied into the environment's registry
first when it lives elsewhere, and the deploy waits until Foundry reports the new version as active.

Examples:
  python -m agent_cicd.deploy --agent-dir agents/my-agent --env dev --dry-run
  python -m agent_cicd.deploy --agent-dir agents/my-agent --env dev
  python -m agent_cicd.deploy --agent-dir agents/my-hosted-agent --env dev --image myacr.azurecr.io/my-hosted-agent@sha256:...
  python -m agent_cicd.deploy --agent-dir agents/my-agent --env prod --rollback-to 7
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .definition import DefinitionError, fingerprint, image_digest, render_agent
from .foundry import project_client, set_outputs

HOSTED_READY_MINUTES = 30


def _model_version(client, deployment_name: str) -> str:
    deployment = client.deployments.get(deployment_name).as_dict()
    version = deployment.get("modelVersion") or deployment.get("model_version")
    if not version:
        raise RuntimeError(f"Model deployment '{deployment_name}' has no model version")
    return f"{deployment.get('modelName') or deployment.get('model_name')}@{version}"


def _latest_version(client, agent_name: str) -> tuple[str, dict, str]:
    """(version, metadata, status) of the agent's latest version; empty when the agent doesn't exist yet."""
    from azure.core.exceptions import ResourceNotFoundError
    try:
        details = client.agents.get(agent_name=agent_name).as_dict()
    except ResourceNotFoundError:
        return "", {}, ""
    latest = (details.get("versions") or {}).get("latest") or {}
    return str(latest.get("version", "")), latest.get("metadata") or {}, str(latest.get("status") or "active").lower()


def _sdk_definition(definition: dict[str, Any]) -> Any:
    from azure.ai.projects.models import HostedAgentDefinition, PromptAgentDefinition
    return (HostedAgentDefinition if definition.get("kind") == "hosted" else PromptAgentDefinition)(definition)


def wait_until_active(client, agent_name: str, version: str, timeout_minutes: float = HOSTED_READY_MINUTES,
                      sleep=time.sleep) -> None:
    """Hosted versions start in 'creating' while Foundry pulls the image and starts the container."""
    deadline = time.monotonic() + timeout_minutes * 60
    delay = 10.0
    while True:
        details = client.agents.get_version(agent_name=agent_name, agent_version=version).as_dict()
        status = str(details.get("status") or "active").lower()
        print(f"[{agent_name} v{version}] status={status}", flush=True)
        if status == "active":
            return
        if status in ("failed", "deleting", "deleted"):
            raise RuntimeError(f"Hosted version {version} is '{status}': check the image, the registry pull "
                               "permission (AcrPull for the Foundry project identity) and the container logs")
        if time.monotonic() > deadline:
            raise RuntimeError(f"Hosted version {version} was not active after {timeout_minutes} minutes")
        sleep(delay)
        delay = min(delay * 1.5, 60.0)


def _prepare_image(rendered, image: str | None) -> str:
    """The digest reference to deploy in this environment (copied into its registry when needed)."""
    from .image import copy_to_registry
    if not image:
        raise DefinitionError("Hosted agents deploy the image built in G1: pass --image "
                              "<registry>/<repository>@sha256:<digest>")
    tag = (os.environ.get("GITHUB_SHA") or "release")[:40]
    return copy_to_registry(image, rendered.container_registry, tag)


def deploy(agent_dir: Path, environment: str, dry_run: bool, force_new: bool = False,
           image: str | None = None) -> int:
    rendered = render_agent(agent_dir, environment, image=image if dry_run else None)
    if dry_run:
        print(json.dumps({"environment": environment, "agent_name": rendered.agent_name, "kind": rendered.kind,
                          "definition": rendered.definition}, indent=2, ensure_ascii=False))
        if rendered.kind == "hosted" and not image:
            print("NOTE: hosted agent rendered without its image; deploys add the digest built in G1")
        if rendered.placeholders:
            print(f"WARNING: {environment} is not configured yet; placeholders in: {', '.join(rendered.placeholders)}")
        return 0
    if not rendered.deployable:
        print(f"ERROR: {environment} config still has placeholders: {', '.join(rendered.placeholders)}", file=sys.stderr)
        return 1
    if rendered.kind == "hosted":
        rendered = render_agent(agent_dir, environment, image=_prepare_image(rendered, image))

    with project_client(rendered.project_endpoint) as client:
        model_version = _model_version(client, rendered.model_deployment)
        agent_fingerprint = fingerprint(rendered.definition, model_version)
        deployed_image = (rendered.definition.get("container_configuration") or {}).get("image", "")
        previous, previous_metadata, previous_status = _latest_version(client, rendered.agent_name)
        # The latest version already runs this exact definition and model: keep it instead of adding an
        # identical version (and leave nothing to roll back to, since nothing changed). A hosted version that
        # never became active (failed or timed out in an earlier run) is replaced, not reused.
        if (not force_new and previous and previous_metadata.get("deployed_by") == "agent-cicd"
                and previous_metadata.get("fingerprint") == agent_fingerprint[:32] and previous_status == "active"):
            print(f"Unchanged: version {previous} already has fingerprint {agent_fingerprint[:12]}; not creating a new one")
            set_outputs(agent_name=rendered.agent_name, agent_version=previous, previous_version="",
                        fingerprint=agent_fingerprint, model_version=model_version, unchanged="true",
                        kind=rendered.kind, image=deployed_image)
            return 0
        metadata = {
            "deployed_by": "agent-cicd",
            "git_sha": os.environ.get("GITHUB_SHA", "local"),
            "fingerprint": agent_fingerprint[:32],
            "model_version": model_version,
        }
        if rendered.kind == "hosted":
            metadata["image_digest"] = image_digest(rendered.definition)
        created = client.agents.create_version(
            agent_name=rendered.agent_name,
            definition=_sdk_definition(rendered.definition),
            metadata=metadata,
        )
        # Recorded before waiting, so a hosted version that fails to start can still be rolled back.
        set_outputs(agent_name=rendered.agent_name, agent_version=str(created.version), previous_version=previous)
        if rendered.kind == "hosted":
            wait_until_active(client, rendered.agent_name, str(created.version))
    set_outputs(fingerprint=agent_fingerprint, model_version=model_version, unchanged="false",
                kind=rendered.kind, image=deployed_image)
    return 0


def rollback(agent_dir: Path, environment: str, target_version: str) -> int:
    rendered = render_agent(agent_dir, environment, require_configured=True)
    with project_client(rendered.project_endpoint) as client:
        source = client.agents.get_version(agent_name=rendered.agent_name, agent_version=target_version)
        # Keep what the source version recorded (fingerprint, model, image digest), so the drift check can tell
        # which release prod runs after the rollback.
        recorded = {key: value for key, value in (source.as_dict().get("metadata") or {}).items()
                    if key in ("fingerprint", "model_version", "image_digest")}
        created = client.agents.create_version(
            agent_name=rendered.agent_name,
            definition=source.definition,
            metadata={**recorded, "deployed_by": "agent-cicd", "rollback_of": str(target_version),
                      "git_sha": os.environ.get("GITHUB_SHA", "local")},
        )
        if rendered.kind == "hosted":
            wait_until_active(client, rendered.agent_name, str(created.version))
    print(f"Rolled back: version {created.version} recreates version {target_version}")
    set_outputs(agent_version=str(created.version))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-dir", type=Path, required=True)
    parser.add_argument("--env", required=True)
    parser.add_argument("--dry-run", action="store_true", help="Render the definition without calling Azure")
    parser.add_argument("--image", default="", help="Hosted agents: digest reference of the image built in G1")
    parser.add_argument("--rollback-to", help="Recreate this earlier version as the newest version")
    parser.add_argument("--force-new-version", action="store_true",
                        help="Create a version even when the latest one has the same fingerprint")
    args = parser.parse_args()
    try:
        if args.rollback_to:
            return rollback(args.agent_dir, args.env, args.rollback_to)
        return deploy(args.agent_dir, args.env, args.dry_run, args.force_new_version, args.image or None)
    except (DefinitionError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
