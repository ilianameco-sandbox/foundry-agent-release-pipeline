# Foundry agent release pipeline

A reusable GitHub Actions pipeline that takes a Microsoft Foundry prompt agent from dev, through a test/UAT project, to production. On the way the agent passes security, data-residency, quality, Responsible AI, red-team and human gates. Every gate fails closed: an error or a missing result blocks the release, never lets it through.

```
PR / push ──► G1 code & security ──► G2 residency + deploy dev ──► G3 evaluation (quality + RAI)
release tag ──► G2 + deploy test ──► G4 AI red team ──► approval packet + RAI scorecard
            ──► G5 UAT ratings + QA / business / RAI sign-offs ──► G6 release decision ──► G2 + deploy prod + smoke + auto-rollback
daily       ──► prod drift check
```

| Gate | What it checks | When it runs |
|---|---|---|
| G1 Code & security | secret scan (gitleaks), dependency scan (pip-audit), unit tests, config rendering, rule-file validation | every PR and push |
| G2 Config & residency | the environment's Azure setup against the rules in `policy.json` (presets: Canada strict, EU data zone, US standard, permissive; block or warn per rule) | before every deploy |
| G3 Evaluation | Foundry evaluation: must-pass behavior and privacy cases, answer correctness, citations, groundedness, PII leaks, protected material | push to `main`, release tags (reused when unchanged) |
| G4 Red team | Foundry AI Red Teaming of the release candidate in the test project | release tags (reused when unchanged) |
| G5 UAT + sign-offs | testers' ratings of the exact release candidate in Foundry (human evaluation / trace 👍👎), then QA, business-owner and RAI approvals | release tags |
| G6 Release decision | all sign-offs present and recent, then the release manager approves `prod` | before every prod deploy |

## Contents

- [cicd/](cicd/): agent-as-code, residency rules, evaluation datasets, pass bars, the gate CLIs and tests. **Start with [cicd/README.md](cicd/README.md)** for setup, roles and the runbook.
- [cicd/agents/my-agent/](cicd/agents/my-agent/): the template agent. Values in `<angle brackets>` are placeholders to replace.
- [.github/workflows/](.github/workflows/): the release pipeline (`agent-release.yml`), the reusable deploy workflow (`agent-deploy.yml`), the prod drift check (`prod-drift-check.yml`), portal-to-PR promotion (`promote-portal-agent.yml`) and the trace harvest that proposes new test cases from real conversations (`harvest-traces.yml`).
- [docs/release-pipeline.drawio](docs/release-pipeline.drawio): the pipeline design (open it at [app.diagrams.net](https://app.diagrams.net)).

## Getting started

1. **Use this repo as a template** (or fork it) for your agent.
2. **Describe your agent** in [cicd/agents/my-agent/](cicd/agents/my-agent/): instructions, tools, and the per-environment configs. Rename the folder and replace `my-agent` in [.github/workflows/agent-release.yml](.github/workflows/agent-release.yml) if you like.
3. **Write your test cases** in `eval/dataset.jsonl`: keep the generic behavior cases, replace the placeholder document questions with real ones, and raise `min_cases` in `eval/thresholds.json` to 100 or more.
4. **Pick your residency rules** in `policy.json` (a preset plus overrides) and your sign-off roles in `governance.json`.
5. **Set up Azure and GitHub** (projects, identities, environments): see [One-time setup](cicd/README.md#one-time-setup).
6. **Release:** merge to `main` for dev and evaluation, then push a tag such as `my-agent-v1.0.0` to run the full release.

Run the offline checks locally:

```bash
cd cicd
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt pytest
python -m pytest tests -q
```

Live gates need `az login` (or OIDC in Actions) with the **Foundry User** role on the target project. No endpoints are committed: copy [cicd/.env.example](cicd/.env.example) to `cicd/.env` for local runs, and set the same names as GitHub Environment secrets for Actions.

## Scope and limitations

- **Prompt agents only.** Hosted (container) agents are not deployed by this pipeline.
- **Not built yet:** identity persona tests (who can see what, through the agent's tools), continuous evaluation of prod traffic, and generating test cases from plain-language criteria for teams without a dataset.
- **Thresholds are starting points.** Agree the pass bars, red-team limits and residency rules with the agent's owners and Responsible AI reviewers before the first production release.
