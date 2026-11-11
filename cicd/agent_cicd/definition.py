"""Load an agent definition from code, render it for one environment, and fingerprint it.

agent.json "kind" decides what is rendered:
  * "prompt": model, instructions and tools (the agent runs on Foundry's own runtime).
  * "hosted": a container image built from the agent's src/ folder, plus CPU, memory, protocols and
    environment variables. The image is passed in at deploy time as a digest reference
    (<registry>/<repository>@sha256:...), so every environment runs the exact image that was tested.

Environment configs keep endpoints and resource names out of the repo: any "${NAME}" value in
config/<environment>.json is read from the process environment (GitHub Environment variables in
Actions) or, for local runs, from cicd/.env (copy .env.example).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLACEHOLDER = re.compile(r"^<[^<>]+>$")
VARIABLE = re.compile(r"\$\{([A-Z0-9_]+)\}")
REQUIRED_ENV_KEYS = ("project_endpoint", "agent_name", "model_deployment", "judge_deployment")
DOTENV_PATH = Path(__file__).resolve().parents[1] / ".env"
KINDS = ("prompt", "hosted")
# A digest-pinned image reference. Tags (":latest", ":v1") can be moved to other images, so they are refused.
IMAGE_REF = re.compile(r"^(?P<registry>[a-z0-9][a-z0-9.-]*(?::\d+)?)/(?P<repository>[a-z0-9][a-z0-9._/-]*)"
                       r"@(?P<digest>sha256:[0-9a-f]{64})$")
HOSTED_PROTOCOLS = ("responses", "invocations", "invocations_ws", "activity", "a2a", "mcp")
# Foundry injects these into every hosted container; setting them yourself breaks the agent.
RESERVED_ENV_PREFIXES = ("FOUNDRY_", "AGENT_")


class DefinitionError(ValueError):
    """Raised when an agent definition or environment config is invalid."""


@dataclass(frozen=True)
class RenderedAgent:
    environment: str
    project_endpoint: str
    agent_name: str
    judge_deployment: str
    definition: dict[str, Any]
    placeholders: tuple[str, ...]
    tool_descriptions: tuple[dict[str, str], ...] = ()
    kind: str = "prompt"
    model_deployment: str = ""
    container_registry: str = ""
    description: str = ""

    @property
    def deployable(self) -> bool:
        return not self.placeholders


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise DefinitionError(f"Missing file: {path}") from error
    except json.JSONDecodeError as error:
        raise DefinitionError(f"Invalid JSON in {path}: {error}") from error


def _substitute(value: Any, variables: dict[str, str], missing: set[str]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in variables:
                missing.add(name)
                return match.group(0)
            return variables[name]
        return VARIABLE.sub(replace, value)
    if isinstance(value, list):
        return [_substitute(item, variables, missing) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, variables, missing) for key, item in value.items()}
    return value


def _find_placeholders(value: Any, path: str = "") -> list[str]:
    if isinstance(value, str):
        return [path] if PLACEHOLDER.match(value.strip()) else []
    if isinstance(value, list):
        return [p for i, item in enumerate(value) for p in _find_placeholders(item, f"{path}[{i}]")]
    if isinstance(value, dict):
        return [p for key, item in value.items() for p in _find_placeholders(item, f"{path}.{key}" if path else key)]
    return []


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE lines; blank lines and # comments are ignored."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def default_env() -> dict[str, str]:
    """cicd/.env for local runs; real environment variables win."""
    return {**read_dotenv(DOTENV_PATH), **os.environ}


def agent_kind(agent_dir: Path) -> str:
    """The agent's kind from agent.json ("prompt" or "hosted")."""
    kind = _read_json(agent_dir / "agent.json").get("kind")
    if kind not in KINDS:
        raise DefinitionError(f"agent.json: kind must be one of {', '.join(KINDS)} (got '{kind}')")
    return kind


def parse_image(reference: str) -> dict[str, str]:
    """Split <registry>/<repository>@sha256:<digest>; anything else (tags, short names) is refused."""
    match = IMAGE_REF.match(reference.strip())
    if not match:
        raise DefinitionError(f"Image '{reference}' must be pinned by digest: <registry>/<repository>@sha256:<64 hex>")
    return match.groupdict()


def _render_prompt(agent: dict[str, Any], agent_dir: Path, config: dict[str, Any],
                   variables: dict[str, str], environment: str) -> dict[str, Any]:
    instructions_path = agent_dir / agent.get("instructions_file", "instructions.md")
    try:
        instructions = instructions_path.read_text(encoding="utf-8").rstrip("\n")
    except FileNotFoundError as error:
        raise DefinitionError(f"Missing instructions file: {instructions_path}") from error
    if not instructions.strip():
        raise DefinitionError("Instructions are empty")

    missing: set[str] = set()
    tools = _substitute(agent.get("tools", []), variables, missing)
    if missing:
        raise DefinitionError(f"config/{environment}.json is missing variables: {', '.join(sorted(missing))}")

    definition: dict[str, Any] = {
        "kind": "prompt",
        "model": config["model_deployment"],
        "instructions": instructions,
        "tools": tools,
    }
    if agent.get("reasoning"):
        definition["reasoning"] = agent["reasoning"]
    return definition


def _render_hosted(agent: dict[str, Any], config: dict[str, Any], variables: dict[str, str], environment: str,
                   image: str | None) -> dict[str, Any]:
    cpu, memory = str(agent.get("cpu") or "").strip(), str(agent.get("memory") or "").strip()
    if not cpu or not memory:
        raise DefinitionError("agent.json: hosted agents need 'cpu' (e.g. \"1\") and 'memory' (e.g. \"2Gi\")")
    protocols = agent.get("protocols") or []
    for entry in protocols:
        if entry.get("protocol") not in HOSTED_PROTOCOLS or not str(entry.get("version") or "").strip():
            raise DefinitionError(f"agent.json: protocol entries need a protocol in {', '.join(HOSTED_PROTOCOLS)} "
                                  f"and a version (got {entry})")
    if not any(entry.get("protocol") == "responses" for entry in protocols):
        raise DefinitionError("agent.json: hosted agents need the 'responses' protocol; the evaluation, red-team "
                              "and smoke gates call the agent through it")

    missing: set[str] = set()
    # ${MODEL_DEPLOYMENT} is the environment's model_deployment, so the model name is configured only once.
    environment_variables = _substitute(agent.get("environment_variables") or {},
                                        {"MODEL_DEPLOYMENT": config["model_deployment"], **variables}, missing)
    if missing:
        raise DefinitionError(f"config/{environment}.json is missing variables: {', '.join(sorted(missing))}")
    reserved = sorted(name for name in environment_variables if name.upper().startswith(RESERVED_ENV_PREFIXES))
    if reserved:
        raise DefinitionError(f"agent.json: {', '.join(reserved)} are set by Foundry in every hosted container; "
                              "use another name")

    definition: dict[str, Any] = {
        "kind": "hosted",
        "cpu": cpu,
        "memory": memory,
        "protocol_versions": [{"protocol": e["protocol"], "version": str(e["version"])} for e in protocols],
    }
    if environment_variables:
        definition["environment_variables"] = {k: str(v) for k, v in environment_variables.items()}
    if image:
        parse_image(image)
        definition["container_configuration"] = {"image": image.strip()}
    return definition


def render_agent(agent_dir: Path, environment: str, *, env: Mapping[str, str] | None = None,
                 require_configured: bool = False, image: str | None = None) -> RenderedAgent:
    """Combine agent.json, the instructions file (prompt) and config/<environment>.json into one definition.

    Unset ${NAME} values are reported as placeholders. Pass require_configured=True before calling Azure.
    Hosted agents get their container image only when `image` is given (deploys); gates that just call
    the agent by name and version don't need it.
    """
    agent = _read_json(agent_dir / "agent.json")
    raw_config = _read_json(agent_dir / "config" / f"{environment}.json")
    environment_values = {k: v for k, v in (default_env() if env is None else env).items() if v}
    unset: set[str] = set()
    config = _substitute(raw_config, environment_values, unset)

    kind = agent.get("kind")
    if kind not in KINDS:
        raise DefinitionError(f"agent.json: kind must be one of {', '.join(KINDS)} (got '{kind}')")
    required = REQUIRED_ENV_KEYS + (("container_registry",) if kind == "hosted" else ())
    for key in required:
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise DefinitionError(f"config/{environment}.json: '{key}' is required")

    variables = config.get("variables", {})
    if kind == "prompt":
        definition = _render_prompt(agent, agent_dir, config, variables, environment)
    else:
        definition = _render_hosted(agent, config, variables, environment, image)

    placeholders = tuple(_find_placeholders(config)) + tuple(f"${{{name}}} not set" for name in sorted(unset))
    if require_configured and placeholders:
        raise DefinitionError(f"{environment} is not configured: {', '.join(placeholders)}")
    return RenderedAgent(
        environment=environment,
        project_endpoint=config["project_endpoint"],
        agent_name=config["agent_name"],
        judge_deployment=config["judge_deployment"],
        definition=definition,
        placeholders=placeholders,
        tool_descriptions=tuple(agent.get("tool_descriptions", [])),
        kind=kind,
        model_deployment=config["model_deployment"],
        container_registry=str(config.get("container_registry") or "").strip().lower(),
        description=str(agent.get("description") or ""),
    )


def image_digest(definition: dict[str, Any]) -> str:
    """The sha256 digest of a hosted definition's image, or "" when there is none."""
    image = ((definition.get("container_configuration") or {}).get("image") or "").strip()
    match = IMAGE_REF.match(image)
    return match.group("digest") if match else ""


def fingerprint(definition: dict[str, Any], model_version: str) -> str:
    """Hash everything that changes the agent's behaviour, including the deployed model version.

    A hosted image counts by digest only, so the same image copied into each environment's registry keeps the
    same fingerprint in dev, test and prod.
    """
    digest = image_digest(definition)
    if digest:
        definition = {**definition, "container_configuration": {"image": digest}}
    payload = {"definition": definition, "model_version": model_version}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
