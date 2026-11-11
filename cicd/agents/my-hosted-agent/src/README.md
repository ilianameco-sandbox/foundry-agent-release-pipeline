# Hosted agent source

Put the agent's code and its `Dockerfile` here. The pipeline builds this folder into one container image in
G1, scans it, and deploys that exact image (by digest) to dev, test and prod.

The release fails at G1 until a `Dockerfile` exists in this folder.

## What the image must do

- **Serve the Foundry hosted-agent protocol on port 8088.** Use an official hosting library, for example
  `agent-framework-foundry-hosting` (Microsoft Agent Framework), `langchain-azure-ai[hosting]`, or
  `azure-ai-agentserver-invocations`. The library also provides the readiness endpoint Foundry probes.
- **Support the `responses` protocol.** The evaluation, red-team and smoke gates call the agent through it.
  Add other protocols (invocations, activity, ...) in `agent.json` if the agent needs them.
- **Read settings from environment variables.** `agent.json` `environment_variables` sets them per
  environment. `${MODEL_DEPLOYMENT}` is the environment's `model_deployment`, and other `${NAME}` values come
  from `config/<env>.json` `variables`. Foundry sets `FOUNDRY_*` and `AGENT_*` itself; don't reuse those names.
- **Use the agent's managed identity, not keys.** Never bake secrets into the image or pass them as
  environment variables.

## Build rules

- Pin the base image and dependency versions (a lock file with hashes for Python), so a rebuild of the same
  commit gives the same packages.
- Add a `.dockerignore` that excludes `.env`, credentials, virtual environments and caches.
- Keep the image small and run as a non-root user; the G1 scan fails on fixable HIGH or CRITICAL
  vulnerabilities.

A quick way to start: scaffold an agent with `azd ai agent init` (or a sample from `azd ai agent sample list`),
then copy its code and `Dockerfile` here. Try it locally with `azd ai agent run` before opening a pull request.
