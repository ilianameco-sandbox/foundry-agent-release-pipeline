"""Container image helpers for hosted agents: where to build from, and promotion by digest.

The pipeline builds a hosted agent's image once (G1), pushes it to the dev registry and passes the digest
reference (<registry>/<repository>@sha256:...) to every deploy. When an environment uses its own registry,
the image is copied there with `az acr import`; the manifest is unchanged, so the digest (and the agent's
fingerprint) stays the same in dev, test and prod.

Examples:
  python -m agent_cicd.image --agent-dir agents/my-agent                    # prints kind=prompt
  python -m agent_cicd.image --agent-dir agents/my-hosted-agent --require-source
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .definition import DefinitionError, _read_json, agent_kind, parse_image
from .foundry import set_outputs


def build_info(agent_dir: Path, require_source: bool = False) -> dict[str, str]:
    """Kind, build context, Dockerfile and image repository; build fields are empty for prompt agents."""
    kind = agent_kind(agent_dir)
    if kind != "hosted":
        return {"kind": kind, "build_context": "", "dockerfile": "", "repository": ""}
    agent = _read_json(agent_dir / "agent.json")
    context = agent_dir / str(agent.get("source_dir") or "src")
    dockerfile = context / str(agent.get("dockerfile") or "Dockerfile")
    repository = str(agent.get("image_repository") or agent_dir.name).lower()
    if require_source and not dockerfile.is_file():
        raise DefinitionError(f"Missing {dockerfile.as_posix()}: add the agent's code and Dockerfile to "
                              f"{context.as_posix()}/ (see its README.md)")
    return {"kind": kind, "build_context": context.as_posix(), "dockerfile": dockerfile.as_posix(),
            "repository": repository}


def retarget(reference: str, registry: str) -> str:
    """The same image (same digest) addressed through another registry."""
    parts = parse_image(reference)
    return f"{registry.strip().lower()}/{parts['repository']}@{parts['digest']}"


def copy_to_registry(reference: str, registry: str, tag: str,
                     run: Callable[..., Any] = subprocess.run) -> str:
    """Make the image available in `registry` and return its digest reference there.

    Nothing is copied when the image is already in that registry. The pipeline identity needs AcrPull on the
    source registry and the import permission on the target one (Container Registry Data Importer and Data
    Reader, or any role with registries/importImage/action; AcrPush does not include it).
    """
    parts = parse_image(reference)
    registry = registry.strip().lower()
    if parts["registry"] == registry:
        return reference.strip()
    command = ["az", "acr", "import", "--name", registry.split(".")[0], "--source", reference.strip(),
               "--image", f"{parts['repository']}:{tag}", "--force"]
    print(f"Copying {parts['repository']}@{parts['digest'][:19]} from {parts['registry']} to {registry}")
    result = run(command, capture_output=True, text=True, shell=os.name == "nt", check=False)
    if result.returncode:
        raise RuntimeError(f"az acr import into {registry} failed: {(result.stderr or result.stdout).strip()}")
    return retarget(reference, registry)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-dir", type=Path, required=True)
    parser.add_argument("--require-source", action="store_true",
                        help="Fail when a hosted agent has no Dockerfile in its source folder")
    args = parser.parse_args()
    try:
        info = build_info(args.agent_dir, args.require_source)
    except DefinitionError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    set_outputs(**info)
    print(json.dumps(info))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
