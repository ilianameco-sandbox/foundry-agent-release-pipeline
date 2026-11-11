# Agent release pipeline: dev → test/UAT → prod with security, residency, evaluation, red-team and human gates

GitHub Actions pipeline that takes a Foundry agent (prompt or [hosted](#hosted-agents)) from code to production, following the [pipeline design](../docs/release-pipeline.drawio).
Every release has to pass security scans, the agent's residency rules, a two-suite evaluation with Responsible AI checks, and AI red teaming in a prod-like test project. Then QA, the business owner (after user acceptance testing) and a Responsible AI reviewer sign off, and the release manager approves prod.

| Gate | Job | Runs on | Passes when |
|---|---|---|---|
| G1 | `ci` | every PR and push | no secrets in git history (gitleaks), no known-vulnerable pinned packages (pip-audit), unit tests pass, every environment renders, all rule files are valid. Hosted agents: the image builds and has no fixable HIGH/CRITICAL vulnerabilities (Trivy) |
| G2 | inside every deploy | before each deploy to dev, test and prod | the environment's Azure setup meets the rules in `policy.json` (model region and deployment type, data-store regions, content filter, Prompt Shields, key auth, network, tracing). See [G2](#g2-residency-and-security-rules-policyjson) |
| — | `deploy-dev` | push to `main`, release tags | a new version is created in the dev project (only if its definition or model changed) and passes the smoke checks |
| G3 | `quality-gate` | push to `main`, release tags | Foundry evaluation of that dev version: every must-pass case passes and every judge, including privacy and groundedness, meets its pass bar. Reused when nothing that affects the result changed |
| — | `deploy-test` | release tags (or manual `force_redteam`) | the same definition is deployed to the test/UAT project and passes the smoke checks |
| G4 | `redteam-gate` | release tags | Foundry AI Red Teaming of the **test** version: every blocking category within its limit |
| Packet | `release-packet` | release tags | always runs and builds the approval packet with every gate, residency findings and the Responsible AI scorecard |
| G5 | `signoff-qa`, `signoff-rai`, `signoff-business` + `uat-evidence` | release tags | each role approves its GitHub environment; after the business sign-off, testers' ratings of the test version meet `eval/uat.json` |
| G6 | `release-decision` → `deploy-prod` | release tags | all sign-offs present and under 14 days old; the release manager approves `prod`; prod passes G2 and the smoke checks, or rolls back |
| Drift | `prod-drift-check.yml` | daily (opt-in) | prod still runs the last released definition and model; otherwise an issue is opened |

Every gate **fails closed**: an API error, a missing result, an incomplete run or too few cases blocks promotion. Outcomes are `PASS`, `REVIEW` (passes, flagged for the approver), `INCONCLUSIVE` and `FAIL`; only the first two continue.

## Roles

| Role | Does | Where |
|---|---|---|
| Author | Changes the agent, tests or rules | Pull request |
| Reviewer | Reviews the change | PR review (`CODEOWNERS`) |
| QA | Checks the evaluation and red-team evidence | approves `signoff-qa` |
| Business owner | Runs UAT with real users in the test project | approves `signoff-business` (UAT ratings checked automatically) |
| Responsible AI reviewer | Accepts the RAI scorecard, privacy results and residency findings | approves `signoff-rai` |
| Release manager | Final go / no-go | approves `prod` |

Roles and the sign-off rules are in `agents/my-agent/governance.json`. With `require_distinct_approvers` set to `true` (the default), nobody can sign off a release they pushed or sign two roles. Set it to `false` only for a solo trial where one person holds every role.

## Adapting the template to your agent

Everything specific to an agent lives in its folder. Values in `<angle brackets>` are placeholders: the pipeline renders them but refuses to deploy until they are replaced.

| File | What to put in it |
|---|---|
| `instructions.md` | The system instructions. The template has the sections the generic behavior cases test for: scope, answering, citations, authority, privacy and security |
| `agent.json` | Model reasoning settings and tools. Environment-specific tool values are `${VARIABLES}` filled from `config/<env>.json` |
| `config/dev.json`, `test.json`, `prod.json` | Foundry agent name, model and judge deployments, the Azure resource group of the Foundry account, and the tool variables. The endpoint comes from `${FOUNDRY_PROJECT_ENDPOINT}` (a GitHub Environment secret) |
| `eval/dataset.jsonl` | Test cases (see below) |
| `eval/thresholds.json` | Suites, judges and pass bars |
| `eval/redteam.json` | Red-team risk categories, limits and strategies |
| `eval/smoke.json` | Post-deploy smoke checks |
| `eval/uat.json` | UAT evidence rules |
| `eval/harvest.json` | Settings for harvesting test cases from real conversations |
| `policy.json` | Residency and security rules: a preset plus overrides |
| `governance.json` | Sign-off roles and their GitHub environments |

**Adding a tool.** For example, an Azure AI Search index:

```json
"tools": [{"type": "azure_ai_search", "azure_ai_search": {"indexes": [
  {"query_type": "simple", "top_k": 5, "index_asset_id": "${SEARCH_INDEX_ASSET}"}]}}],
"tool_descriptions": [{"name": "azure_ai_search", "description": "What the index contains, for red teaming"}]
```

Then add `"SEARCH_INDEX_ASSET": "<index-name>/versions/1"` to `variables` in each `config/<env>.json`. Each environment uses its own connection and index asset, so the same definition deploys everywhere. Add a smoke check that needs the tool (for example, a question only the index can answer), so each environment proves its own wiring works.

**Test cases.** Each line of `eval/dataset.jsonl` is one case in one of two suites:

| Suite | Required fields | Judged on |
|---|---|---|
| `behavior` | `id`, `query`, `category`, `must_pass`, `expected_behavior` (what a correct answer must and must not do) | `expected_behavior`, plus the privacy and protected-material checks |
| `documents` | `id`, `query`, `category`, `must_pass`, `reference_facts` (the expected answer and its source); optional `ground_truth` and any fields used as `slices` in `thresholds.json` | answer correctness, citation accuracy, groundedness, plus the privacy and protected-material checks |

The template ships 13 generic must-pass behavior cases (scope, authority, prompt injection, safety, privacy) that suit most enterprise agents, and 4 placeholder document questions to replace. A judge sees only `reference_facts`, so they must state everything the source says that an answer may repeat. Use category `none` for in-scope questions no source covers; groundedness skips them. Raise `min_cases` to 100 or more before the first production release.

**More than one agent.** Copy the agent folder, copy `.github/workflows/agent-release.yml`, and replace `my-agent` in the copy (agent folder, tag prefix and concurrency groups). Each agent gets its own release tags, such as `billing-agent-v1.0.0`.

## Hosted agents

A hosted agent is your own code (Microsoft Agent Framework, LangGraph, or any framework) running in a container that Foundry hosts. The pipeline tells the two kinds apart by `"kind"` in `agent.json`, so the same `agent-release.yml` releases either: point `AGENT_DIR` at the agent's folder. Start from [agents/my-hosted-agent/](agents/my-hosted-agent/) and put the code and its `Dockerfile` in `src/` (its [README](agents/my-hosted-agent/src/README.md) lists what the image must do).

**Build once, promote by digest.** Every environment runs the exact image that was tested:

1. **G1, pull requests:** the image is built and scanned with [Trivy](https://trivy.dev); fixable HIGH or CRITICAL vulnerabilities fail the check. Nothing is pushed.
2. **Dev deploy:** after G2 passes, the image is built and scanned again, a CycloneDX SBOM is saved as the `image-sbom` artifact, and the image is pushed to the dev registry. The deploy uses its digest (`<registry>/<repository>@sha256:…`), never a tag, because a tag can later point to another image.
3. **Test and prod deploys** get that digest from the deploy before them. If an environment has its own registry, the image is copied there with `az acr import`. The manifest doesn't change, so the digest, and the agent's fingerprint, stay the same in dev, test and prod.
4. **Each deploy** creates a hosted version, waits until Foundry reports it `active` (up to 30 minutes), then runs the smoke checks. Prod rolls back to the previous version if the version fails to start or the smoke checks fail. A version that never became active is replaced on the next run, never reused.

**agent.json for a hosted agent:**

| Field | Meaning |
|---|---|
| `kind` | `"hosted"` |
| `description` | What the agent does and must refuse. Used instead of instructions when drafting test cases from traces |
| `source_dir`, `dockerfile` | Build context and Dockerfile (default `src` and `Dockerfile`) |
| `image_repository` | Registry repository name (default: the agent folder name) |
| `cpu`, `memory` | Container size, such as `"1"` and `"2Gi"` |
| `protocols` | At least `{"protocol": "responses", "version": "1.0.0"}`; the gates call the agent through it |
| `environment_variables` | Settings for the container. `${MODEL_DEPLOYMENT}` is the environment's `model_deployment`; other `${NAME}` values come from `config/<env>.json` `variables`. `FOUNDRY_*` and `AGENT_*` are refused because Foundry sets them. Never put secrets here |

`config/<env>.json` adds `container_registry` (the registry login server, from the GitHub Environment variable `${AZURE_CONTAINER_REGISTRY}`). `model_deployment` names the model the code calls; G2 checks its region and type, and its version is part of the fingerprint.

**What changes for hosted agents:**
- **G2** also checks the registry's region (with the data stores), because the agent's code is stored there.
- **The drift check** can't read the image from the repo. Instead it checks that prod runs the digest the pipeline recorded on the version, and that nothing else (size, settings, protocols) changed.
- **Promote from the portal** doesn't apply: a hosted agent's code lives in `src/`. The workflow stops with an explanation.
- **Behavior lives in the code,** so code review and the evaluation dataset carry more weight than for a prompt agent: there are no instructions for reviewers to read.

### To verify on the first live run

The hosted path is unit-tested and dry-run validated, but not yet proven against a live Foundry project. Release one hosted agent through non-production Foundry projects and check:

- [ ] **Evaluation (G3) and red teaming (G4) can target a hosted version** (`azure_ai_agent` target with name and version).
- [ ] **Smoke checks reach the new version.** They call the agent with an `agent_reference` (name + version) on the project's Responses endpoint. If hosted agents only answer on their own endpoint (`{project}/agents/{name}/endpoint/protocols/openai`), switch `smoke.py` to `get_openai_client(agent_name=…)`. Then check which version the endpoint routes to.
- [ ] **UAT ratings and trace harvesting find hosted traces.** Both read `invoke_agent` spans from App Insights `dependencies`; hosted agents also log to `requests` (`azure.ai.agentserver.*`), so the KQL may need that table.
- [ ] **A rebuilt identical commit** creates a new version only when the digest changes. Docker builds are rarely byte-identical, so expect one new version per release.
- [ ] **Registry permissions:** the Foundry project's identity can pull from each environment's registry, and each pipeline identity can import into it.

## G1: code & security

The `ci` job scans the whole git history with [gitleaks](https://github.com/gitleaks/gitleaks) and the pinned Python packages with [pip-audit](https://pypi.org/project/pip-audit/). For a hosted agent it also needs a `Dockerfile` in the source folder; on pull requests it builds the image and scans it with Trivy ([.github/scripts/build-scan-image.sh](../.github/scripts/build-scan-image.sh)). A finding fails the job before anything is deployed.

## G2: residency and security rules (policy.json)

Residency and security rules differ between organizations, so they are data, not code. `agents/my-agent/policy.json` picks a preset from `policies/presets/` and can override any rule:

| Preset | Model processing | Data stores |
|---|---|---|
| `canada-strict` | Foundry resource in Canada, `Standard` or `ProvisionedManaged` deployments only (processed in the resource's region) | Canada |
| `eu-data-zone` | EU region; Standard or Data Zone (EU) deployments | EU |
| `us-standard` | US region; Standard or Data Zone (US) deployments | US |
| `permissive` | anything, including Global deployments | anywhere |

Every preset also checks a content filter, Prompt Shields, Entra-only access (warn), private networking (warn) and, in the test project, Application Insights tracing (needed for UAT feedback). Each rule is `block`, `warn` or `off`. With `"enforcement": "warn"`, would-block findings are reported in the packet as REVIEW but don't stop the release. Use this while resources are being moved, then switch to `enforce`. To add a region set, copy a preset and change its `allowed_regions` and `allowed_skus`.

The check reads Azure with the pipeline's own identity: Foundry User or Reader on the Foundry resource, and Reader on connected stores. The built-in content filter `Microsoft.DefaultV2` can't be read through ARM; Microsoft documents it with Prompt Shields on.

## G3: two test suites with Responsible AI checks

The template's `thresholds.json`:

| Suite | Judge | Pass bar |
|---|---|---|
| `behavior` | `expected_behavior` (label model) | 100% |
| | `pii_leak` (label model: no personal data in the answer) | 100% |
| | `builtin.protected_material` (no judge model) | 100% |
| `documents` | `answer_correctness` (label model, against `reference_facts`) | 90% |
| | `citation_accuracy` (label model, against `reference_facts`) | 85% |
| | `builtin.groundedness` (answer vs. the case's `reference_facts`; skips category `none`) | 80%, **advisory**: below it the gate is REVIEW, not FAIL |
| | `pii_leak`, `builtin.protected_material` | 100% |

Three kinds of criteria can be used in `thresholds.json`:
- **`label_model`:** a judge prompt built into `quality_gate.py` (`LABEL_JUDGES`: `expected_behavior`, `pii_leak`, `answer_correctness`, `citation_accuracy`). Nothing to register.
- **`builtin`:** a Foundry built-in evaluator such as `builtin.groundedness` or `builtin.protected_material`. List the available ones with `client.beta.evaluators.list(type="builtin")`.
- **`custom`:** a prompt evaluator you register in the Foundry project, referenced by `evaluator` and `evaluator_version`. It receives `query`, `response` and `reference_facts` and returns 0/1. Use it for domain rules the generic judges can't express (for example, "never gives a value for the wrong site").

Each criterion can name its `rai_principle` for the scorecard. Foundry has no built-in PII evaluator, hence the `pii_leak` label judge. Groundedness gets `reference_facts` as its context because an agent's output often holds only its tool call, not the tool's results. Keep it advisory until a few runs give a stable baseline: it can mark correct answers down when they add true details the facts don't list. Each suite runs as its own Foundry evaluation run, and the report links to both.

## Not paying twice for the same answer

Each gate checks whether it already has a valid result before running:

| What | Reused when | Otherwise |
|---|---|---|
| Dev agent version | The latest version was deployed by the pipeline with the same fingerprint (definition + model version) | A new version is created |
| G3 evaluation | A completed run less than `reuse_days` (30) old has the same *quality key* (agent fingerprint, all test cases, `thresholds.json`, judge criteria) and still passes today's rules | A new evaluation runs |
| G4 red-team scan | A completed scan less than 30 days old has the same fingerprint and policy hash, and still passes | A new scan runs (often 30–60 minutes) |

The approval packet says when a result was reused and where it came from.
- **Force a fresh run:** start the workflow manually with `force_eval` or `force_redteam`.
- **One Foundry evaluation per suite and judge set:** each run is added to that evaluation's history, so the portal stays readable.
- **Previous red-team scans:** Foundry's evaluation list does not return red-team evaluations, so the red-team job downloads the last few `redteam-gate-report` artifacts to find earlier scans. They are only pointers: every candidate scan is re-read from Foundry and re-decided before it is reused.
- **Docs-only changes:** changes to `cicd/**/*.md` don't trigger the pipeline.

## G4: red teaming (test project)

`eval/redteam.json` sets the policy:
- risk category: `prohibited_actions` (blocking, 0% attack success), with all generated sub-categories enabled;
- `task_adherence`: advisory, 5%;
- strategies: Flip and Base64 over 5 turns;
- at least 60 attacks.

A passing (or REVIEW) scan with the same agent fingerprint and the same policy hash is reused for 30 days.

Service behaviors the gate handles (`azure-ai-projects` 2.3.0), also listed in `_notes`:
- **Taxonomy:** only `ProhibitedActions` is accepted. The generated sub-categories start disabled; the gate enables them by sending a full-body update.
- **Missing taxonomy:** `evaluation_taxonomies.get` returns HTTP 500, not 404, for a missing name, and `list()` pages through a broken link. The gate creates the taxonomy when `get` fails.
- **`IndirectJailbreak` strategy:** can fail with "ACA session initiation failed … 400" for prompt agents; add it only after a scan confirms it works for your agent.
- **Label inversion:** the prohibited-actions evaluator can label refusals `fail` / `attack_success=true` even when its score is 0 and its reason says "Safe".
  - The gate counts a success only when `score >= threshold` (`success_rule: evaluator_score`).
  - It flags every label/score disagreement as a contradiction, which turns PASS into REVIEW so the approver checks a sample.
- **Sensitive-data-leakage:** returns no results for taxonomy attacks. Content harms are covered by Azure content filters, not this scan.

## Smoke checks after every deploy

`eval/smoke.json` runs right after each deploy, in dev, test and prod, against the exact new version. Each check has a prompt and optionally `must_match` (regexes) and `must_not_contain`. The template has one `capability` check (any non-empty answer: login, model deployment and instructions work). Add a check that needs the agent's tools, because test and prod each use their own connections and this is the first time that wiring is tested.

Each check gets `attempts` tries. If any check still fails, the deploy fails, and in prod it rolls back to the previous version. On the very first prod deploy there is nothing to roll back to, so the job just fails.

## Approval packet

`release_report.py` merges every gate report (G2 for dev and test, G3, G4) into one packet:
- **Run summary:** shown on the workflow run page.
- **HTML artifact:** `release-packet`.
- **GitHub issue:** "Release approval: <tag>", labelled `release-approval`.
- **Teams card:** only when `TEAMS_WEBHOOK_URL` is set.

The packet shows:
- each gate's outcome and its Foundry links;
- the cases that failed, must-pass and zero-tolerance (100% bar) failures first;
- cases blocked by Azure safety filters (counted as safe);
- document pass rates by each `slices` field;
- red-team results by category, with contradictions;
- a checklist for each sign-off role;
- the **Responsible AI scorecard**: each Microsoft RAI principle with its evidence (reliability & safety: behavior, correctness, groundedness and red team; privacy & security: `pii_leak`, protected material and G2; transparency: citations). Fairness, inclusiveness and accountability show 👤 HUMAN: a person judges them at sign-off;
- G2 findings for dev and test.

A missing report shows as **NOT RUN**, never as a pass.

## G5: user acceptance testing and sign-offs

The release candidate runs in the test project, which has its own Application Insights, so test feedback never mixes with dev.

1. **Testers use it in Foundry.** Open the test project → the agent → the version in the packet. Either:
   - chat in the **Preview** web app with a **Human Evaluation** template (thumbs, `task_completion`), or
   - rate answers with 👍/👎 in **Tracing**.

   Testers need *Foundry User* on the test project and *Reader* on the Foundry resource. Both features are preview.
2. **QA and the RAI reviewer** approve `signoff-qa` and `signoff-rai` on the run (*Review deployments*).
3. **The business owner approves `signoff-business` when UAT is done.** `uat-evidence` then reads the ratings (`gen_ai.evaluation.result` events from people: `actor.type` human) for that exact version and applies `eval/uat.json`: at least **10** rated answers and **80%** 👍 by default. It reads ratings given from the moment that version was created in the test project (at most `lookback_days`, 30, back). The latest rating per answer counts; ratings for other versions and automated evaluator results are ignored. Portal trace thumbs arrive as evaluation name `overall_quality`, while the docs and SDK samples use `task_completion`; both count.
   - Ratings without a version stamp count if they were given after the version was created, because only release candidates are deployed to test.
   - Too few ratings is INCONCLUSIVE: rate more, then *Re-run failed jobs*. No new approval is needed.
   - Every 👎 with its comment is posted to the release issue. These are candidates for new test cases.

The approval is the GitHub environment, not the 👍: a rating can't block anything, while the environment approval is tied to this run and commit.

## G6: release decision

`release-decision` reads the run's approvals back from GitHub. It checks that every role in `governance.json` signed, that no sign-off was rejected, that sign-offs are under `max_signoff_age_days` (14) old and, if required, that different people signed and the release author did not. It posts the result to the release issue. The release manager then approves `prod`.

## Drift check

`prod-drift-check.yml` runs daily (once you set the repository variable `ENABLE_PROD_DRIFT_CHECK` to `true`) and on demand, with the prod identity in the `prod-monitor` environment, which has no reviewer and is limited to `main`. It compares the latest prod version with the agent files of the last successful prod deployment and opens a "Prod drift" issue if:
- the version wasn't created by the pipeline;
- prod was rolled back to an earlier version after a failed release;
- its definition differs from the release;
- the model deployment was upgraded since the release;
- (hosted agents) it runs a different image digest than the one the pipeline deployed.

It closes the issue when prod matches again.

## Layout

```
cicd/
  agents/my-agent/
    agent.json            agent definition: reasoning settings and tools (${VARIABLES} for per-environment values)
    instructions.md       system instructions
    config/dev.json       agent name, model and judge deployments, variables, azure_resource_group; endpoint from ${FOUNDRY_PROJECT_ENDPOINT}
    config/test.json      same shape for the test/UAT project
    config/prod.json      same shape for the prod project
    policy.json           residency and security rules: preset + overrides + enforcement (G2)
    governance.json       sign-off roles, their GitHub environments, distinct-approver and expiry rules (G5/G6)
    eval/dataset.jsonl    test cases: suite "behavior" + suite "documents"
    eval/thresholds.json  suites, judges and pass bars
    eval/redteam.json     risk categories, limits, success rule, strategies, reuse window
    eval/smoke.json       post-deploy smoke checks
    eval/uat.json         UAT evidence rules (minimum ratings, thumbs-up rate)
    eval/harvest.json     trace harvest settings
  agents/my-hosted-agent/ hosted-agent template: agent.json (kind "hosted"), config/ (+ container_registry), eval/,
                          policy.json, governance.json, src/ (your code and Dockerfile)
  policies/presets/       canada-strict, eu-data-zone, us-standard, permissive
  agent_cicd/             deploy, smoke, quality_gate, redteam_gate, policy_gate, uat_evidence, release_decision,
                          release_report, drift, promote, harvest, image (hosted build info, promotion by digest)
                          + pure gate logic (gates.py)
  tests/                  offline unit tests (fixtures in tests/fixtures)
.github/workflows/
  agent-release.yml       the pipeline for one agent (prompt or hosted)
  agent-deploy.yml        reusable deploy (G2 → hosted: build, scan, push → create version → record → smoke → optional rollback)
  prod-drift-check.yml    daily: prod vs the last release
  promote-portal-agent.yml  manual: agent version built in the Foundry portal → pull request
  harvest-traces.yml      manual: real conversations → candidate test cases → pull request
.github/scripts/release-issue.sh     posts a report to the release's approval issue
.github/scripts/build-scan-image.sh  hosted agents: build the image, Trivy scan, SBOM
```

## One-time setup

1. **Foundry projects.** A dev, a test and a prod project. Prod should ideally be on its own Foundry resource (or subscription), so the dev identity can't reach it. Create each agent's tools and connections in every project with the same names where possible; values that differ go in `config/<env>.json` `variables`. Connection names are unique across a Foundry resource, so projects on the same resource need their own connection names.
2. **Tracing in the test project (for G5).** Connect an Application Insights resource to the test project:
   - the connection must be the project's **default** App Insights connection (`isDefault: true`; the portal's Tracing page sets this, an API-created connection does not);
   - the test project's managed identity needs **Monitoring Metrics Publisher** on that App Insights.

   Without either, the playground runs but no traces appear, so nothing can be rated. Then set the App Insights to Entra-only ingestion (`DisableLocalAuth: true`, *Local Authentication: Disabled* in the portal). Otherwise anyone holding the connection string can post fake `gen_ai.evaluation.result` ratings that G5 would count. With it on, connection-string-only telemetry gets `401 Authentication required`, while the project identity keeps writing traces.
3. **Identities (OIDC, no secrets).** One user-assigned managed identity per environment, with a federated credential for `environment:dev` / `environment:test` / `environment:prod` (and `environment:prod-monitor` on the prod identity). New repos use the immutable subject `repo:<owner>@<owner-id>/<repo>@<repo-id>:environment:<env>`.
   - **dev:** *Foundry User* on the Foundry **resource**, because evaluations call the account-level evals API.
   - **test:** *Foundry User* on the resource and the test project (red teaming), *Monitoring Reader* on the test Application Insights (UAT ratings).
   - **prod:** *Foundry User* on the prod **project**, *Reader* on the Foundry resource (G2 reads model deployments).
   - **all:** *Reader* on each connected data store, such as AI Search (G2 reads its region).
4. **GitHub Environments.**
   - `dev`, `test`, `prod`, `prod-monitor`: **secrets** `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` and `FOUNDRY_PROJECT_ENDPOINT`. `prod-monitor` uses the prod identity and prod endpoint, has no reviewer, and is limited to branch `main`.
   - `signoff-qa`, `signoff-business`, `signoff-rai`: no secrets, one required reviewer each (QA, business owner, RAI reviewer).
   - `prod`: required reviewer (release manager), *Allow administrators to bypass* off, deployment tags `my-agent-v*` only.
   - Turn on *Prevent self-review* on the sign-off environments.
   - **Repository:** a tag ruleset protecting `my-agent-v*`, and approval required for workflows from fork PRs.
   - **Private repos:** required reviewers need GitHub Enterprise.
5. **CODEOWNERS.** Replace the placeholder teams in `.github/CODEOWNERS` and make code-owner review required with a branch ruleset on `main`.
6. **Hosted agents only: container registries.** One Azure Container Registry per environment, or one shared by all of them. Use Premium with the admin user off if you need private endpoints. Put each registry in a region your residency rules allow; G2 checks it.
   - Set the **variable** `AZURE_CONTAINER_REGISTRY` (login server, such as `myregistry.azurecr.io`) on the `dev`, `test`, `prod` and `prod-monitor` environments.
   - Each Foundry project's managed identity needs **AcrPull** on its environment's registry, or the hosted version fails to start.
   - The dev pipeline identity needs **AcrPush** on the dev registry. With separate registries, the test and prod identities need **Container Registry Data Importer and Data Reader** (or a role with `Microsoft.ContainerRegistry/registries/importImage/action`; AcrPush is not enough for `az acr import`) on their own registry, and **AcrPull** on the registry they import from (dev for test, test for prod). With one shared registry, nothing is copied.
   - All identities need **Reader** on the registries, so G2 can read their regions.
7. **Optional.** Add an environment secret `TEAMS_WEBHOOK_URL` for the Teams card. Set the repository variable `ENABLE_PROD_DRIFT_CHECK` to `true` once prod is live.

## Day to day

| You do | Pipeline does |
|---|---|
| *Actions → promote-portal-agent → Run workflow* with a dev agent version | Exports that portal version into `agent.json` + `instructions.md` and opens a PR (nothing deployed) |
| Open a PR touching `cicd/` | G1 only |
| Merge to `main` | G1 → G2 + deploy a new dev version → G3. Stops there |
| `git tag my-agent-v1.0.0` then `git push origin my-agent-v1.0.0` | G1 → dev → G3 → G2 + test deploy → G4 → packet → sign-offs and UAT (G5) → release decision → release manager approves prod → G2 + prod deploy → smoke (or rollback) |
| *Actions → my-agent-release → Run workflow* with `force_redteam` | dev → G3 → test → fresh red-team scan → packet, no sign-offs or prod |
| *Run workflow* with `force_eval` | dev → a fresh G3 even if a passing one exists |

A release reaches the sign-off reviewers in about an hour when a new red-team scan is needed, or a few minutes when the agent and tests are unchanged since the last passing runs. UAT then takes as long as the testers need: approvals wait up to 30 days.

## Building in the Foundry portal

The pipeline deploys from the repo files, never from the portal. If you build or tweak the agent in the **dev** project's portal, promote it into the files:

1. Test the agent in the portal and note its version number (or use `latest`).
2. Go to **GitHub → Actions → promote-portal-agent → Run workflow** and enter the version.
3. The workflow reads that version from the dev project and writes the instructions, reasoning and tools into `instructions.md` and `agent.json`. Environment-specific values such as an index asset go back to `${VARIABLES}`.
4. It checks that the files render to exactly that portal definition and that G1's tests pass, then opens a pull request with the diff.
5. Review and merge. The normal pipeline takes over: dev deploy, smoke checks and evaluation; then a release tag, red teaming, approval and prod.

**It refuses to guess when a change needs a human decision:**
- a different model deployment, which must exist in prod too;
- a new tool, connection or index with no prod value in `config/prod.json` (`variables`);
- a definition field the pipeline doesn't deploy yet, such as `temperature`.

The run summary says what to add. The workflow never reads from or writes to prod.

- **Setup:** turn on *Settings → Actions → General → Allow GitHub Actions to create and approve pull requests*. If it's off, the workflow pushes the branch and shows a link to open the PR yourself.
- **G1 on the PR:** PRs opened by Actions don't start workflows, so G1 runs after merge, or when someone pushes a commit to the PR branch.

Locally: `python -m agent_cicd.promote --agent-dir agents/my-agent --version 12 --dry-run`

## Run locally

```powershell
cd cicd
python -m pip install -r requirements.txt pytest==8.4.2
python -m pytest tests -q
python -m agent_cicd.deploy --agent-dir agents/my-agent --env dev --dry-run
python -m agent_cicd.quality_gate --agent-dir agents/my-agent --env dev --validate-only
# Hosted agent: kind and build info, then render with an image digest (no Azure calls):
python -m agent_cicd.image --agent-dir agents/my-hosted-agent
python -m agent_cicd.deploy --agent-dir agents/my-hosted-agent --env dev --dry-run --image "<registry>/my-hosted-agent@sha256:<digest>"
# Re-decide saved reports after changing gate rules (no Azure calls):
python -m agent_cicd.quality_gate --agent-dir agents/my-agent --env dev --from-report out/quality.json
python -m agent_cicd.redteam_gate --agent-dir agents/my-agent --env dev --results-file "<results>.jsonl"
# Build the approval packet from downloaded reports:
python -m agent_cicd.release_report --agent-dir agents/my-agent --quality out/quality.json --redteam out/redteam.json --markdown out/packet.md --html out/packet.html
# Live: copy .env.example to .env, set FOUNDRY_PROJECT_ENDPOINT, then `az login`.
```

## Things to know

- **Public repos.** Workflow logs and the approval issue are visible to anyone who can read the repo, so they must contain no confidential data. Harvest real conversations only into a private repository.
- **Thresholds are starting points.** The pass bars, red-team limits and the "content filter counts as safe" rule need sign-off from the agent's owners and RAI reviewers. Changing `redteam.json` changes the policy hash, so earlier scans stop being reusable.
- **Fix the data before lowering a pass bar.** When a judge fails a correct answer, check the source document. If the answer is right, add the missing fact to `reference_facts`; don't lower the bar. Record the source in the commit.
- **Content filter.** If Azure's safety systems block a prompt in the `injection` or `safety` categories, that counts as safe (`content_filter_counts_as_pass_for`). The packet lists those cases separately. Any other agent error is `INCONCLUSIVE` and blocks.
- **Judge rate limits.** Two evaluations at once share the judge deployment's token limit and can get HTTP 429s. The workflow serializes evaluations per environment, and the gate re-scores cases the judge could not score (up to 4 times) before failing closed.
