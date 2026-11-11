"use strict";
// Pipeline builder page. It only edits pipeline.yaml settings; the server validates them with the generator's
// own rules, previews the generated files, and on Save writes pipeline.yaml and runs the generator.

// The token is in the URL printed by `python -m agent_cicd.builder`; the server never hands it out.
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const SCHEDULES = {
  drift: [["", "Off (on demand only)"], ["17 6 * * *", "Daily, 06:17 UTC"], ["17 6 * * 1", "Weekly, Monday 06:17 UTC"]],
  harvest: [["", "Off (run it by hand)"], ["0 11 * * 1", "Weekly, Monday 11:00 UTC"], ["0 11 * * *", "Daily, 11:00 UTC"]],
};
const state = {
  agents: [], agent: null, kind: "", exists: false, spec: null, savedJson: "", presets: {},
  custom: { drift: false, harvest: false }, lastPlan: null, preview: null,
};
let timer = null;
let sequence = 0;

// ---------------------------------------------------------------- helpers

async function api(path, body) {
  const options = { headers: { "X-Builder-Token": TOKEN } };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

// Builds DOM nodes; text always goes in as text nodes, never as HTML.
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  let value;
  for (const [key, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), v);
    else if (key === "class") el.className = v;
    else if (key === "value") value = v;
    else if (key === "checked" || key === "disabled") el[key] = Boolean(v);
    else el.setAttribute(key, v === true ? "" : String(v));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  if (value !== undefined) el.value = value;
  return el;
}

function slug(text) {
  const s = text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 30);
  return /^[a-z]/.test(s) ? s : ("role-" + s).slice(0, 30).replace(/-+$/, "");
}

function cleanSpec() {
  const spec = structuredClone(state.spec);
  spec.gates.signoffs = spec.gates.signoffs.map((r) => {
    const role = { id: r.id, title: r.title, environment: r.environment };
    if (r.checks) role.checks = r.checks;
    if (r.uat) role.uat = true;
    return role;
  });
  return spec;
}

function isDirty() {
  return !state.exists || JSON.stringify(cleanSpec()) !== state.savedJson;
}

function changed(structural = false) {
  if (structural) renderForm();
  updateStatus();
  clearTimeout(timer);
  timer = setTimeout(runPreview, 200);
}

function clearUat() {
  state.spec.gates.signoffs.forEach((r) => { r.uat = false; });
}

// ---------------------------------------------------------------- form

function switchRow(title, help, checked, onChange) {
  return h("label", { class: "switch" },
    h("input", { type: "checkbox", checked, onchange: (e) => onChange(e.target.checked) }),
    h("div", {}, h("b", {}, title), h("span", {}, help)));
}

function scheduleField(kind, section, key) {
  const options = SCHEDULES[kind];
  const value = section[key] || "";
  const known = options.some(([v]) => v === value);
  const custom = state.custom[kind] || !known;
  const select = h("select", {
    "aria-label": kind === "drift" ? "Drift check schedule" : "Trace harvest schedule",
    onchange: (e) => {
      if (e.target.value === "__custom") {
        state.custom[kind] = true;
      } else {
        state.custom[kind] = false;
        section[key] = e.target.value;
      }
      changed(true);
    },
    value: custom ? "__custom" : value,
  }, ...options.map(([v, label]) => h("option", { value: v }, label)), h("option", { value: "__custom" }, "Custom cron…"));
  const parts = [select];
  if (custom) {
    parts.push(h("input", {
      type: "text", value, placeholder: "minute hour day month weekday (UTC)", "aria-label": "Custom cron",
      oninput: (e) => { section[key] = e.target.value; changed(); },
    }));
  }
  return h("div", { class: "row" }, ...parts);
}

function roleCard(role, index, roles) {
  const testOn = state.spec.environments.test;
  const idInput = h("input", { type: "text", value: role.id, "aria-label": "Role ID",
    oninput: (e) => { role.id = e.target.value; role._auto = false; changed(); } });
  const envInput = h("input", { type: "text", value: role.environment, "aria-label": "GitHub environment",
    oninput: (e) => { role.environment = e.target.value; role._auto = false; changed(); } });
  const move = (to) => { roles.splice(to, 0, roles.splice(index, 1)[0]); changed(true); };

  const card = h("div", { class: "role", "data-index": index },
    h("div", { class: "role-head" },
      h("span", { class: "grip", title: "Drag to reorder", "aria-hidden": "true", draggable: "true" }, "⠿"),
      h("input", { type: "text", value: role.title, "aria-label": "Role title",
        oninput: (e) => {
          role.title = e.target.value;
          if (role._auto) {
            role.id = slug(role.title || "role");
            role.environment = `signoff-${role.id}`;
            idInput.value = role.id;
            envInput.value = role.environment;
          }
          changed();
        } }),
      h("button", { type: "button", class: "icon", title: "Move up", "aria-label": "Move up", disabled: index === 0,
        onclick: () => move(index - 1) }, "▲"),
      h("button", { type: "button", class: "icon", title: "Move down", "aria-label": "Move down",
        disabled: index === roles.length - 1, onclick: () => move(index + 1) }, "▼"),
      h("button", { type: "button", class: "icon", title: "Remove role", "aria-label": "Remove role",
        disabled: roles.length === 1, onclick: () => { roles.splice(index, 1); changed(true); } }, "✕")),
    h("div", { class: "role-grid" },
      h("label", {}, "ID", idInput),
      h("label", {}, "GitHub environment", envInput),
      h("label", { class: "wide" }, "What they check",
        h("input", { type: "text", value: role.checks || "", "aria-label": "What they check",
          oninput: (e) => { role.checks = e.target.value; changed(); } }))),
    h("label", { class: "uat-pick", title: testOn ? "" : "Needs the test/UAT project" },
      h("input", { type: "radio", name: "uat", checked: role.uat, disabled: !testOn,
        onchange: () => { clearUat(); role.uat = true; changed(true); } }),
      "Confirms UAT is done (the UAT evidence check runs after this role)"));

  card.addEventListener("dragstart", (e) => {
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(index));
    card.classList.add("dragging");
  });
  card.addEventListener("dragend", () => card.classList.remove("dragging"));
  card.addEventListener("dragover", (e) => { e.preventDefault(); card.classList.add("drag-over"); });
  card.addEventListener("dragleave", () => card.classList.remove("drag-over"));
  card.addEventListener("drop", (e) => {
    e.preventDefault();
    card.classList.remove("drag-over");
    const from = Number(e.dataTransfer.getData("text/plain"));
    if (Number.isInteger(from) && from !== index) {
      roles.splice(index, 0, roles.splice(from, 1)[0]);
      changed(true);
    }
  });
  return card;
}

function renderForm() {
  const s = state.spec;
  const testOn = s.environments.test;
  const roles = s.gates.signoffs;
  const preset = state.presets[s.residency.preset] || { description: "", regions: [] };
  const anyUat = roles.some((r) => r.uat);

  const environments = h("fieldset", {}, h("legend", {}, "Environments"),
    h("p", { class: "note" }, "dev and prod are always there."),
    switchRow("Test / UAT project",
      "A separate Foundry project for the release candidate, red teaming and user acceptance testing. Off: releases go dev → prod.",
      testOn, (v) => { s.environments.test = v; if (!v) clearUat(); changed(true); }));

  const gates = h("fieldset", {}, h("legend", {}, "Gates"),
    h("p", { class: "note" }, "Always on: G1 code & security, G2 residency before every deploy, G3 evaluation, and the release manager's approval of prod."),
    switchRow("AI red teaming (G4)", `Attacks the release candidate in ${testOn ? "test" : "dev"} before sign-off (eval/redteam.json).`,
      s.gates.redteam, (v) => { s.gates.redteam = v; changed(true); }));

  const signoffs = h("fieldset", {}, h("legend", {}, "Sign-offs (G5)"),
    h("p", { class: "note" }, "Drag ⠿ to reorder. Each role approves its own GitHub environment; the release manager approves prod after them."),
    ...roles.map((role, i) => roleCard(role, i, roles)),
    h("label", { class: "uat-pick" },
      h("input", { type: "radio", name: "uat", checked: !anyUat, onchange: () => { clearUat(); changed(true); } }),
      testOn ? "No UAT step" : "No UAT step (needs the test/UAT project)"),
    h("div", { class: "row" }, h("button", { type: "button", class: "add", onclick: () => {
      roles.push({ id: "", title: "", environment: "", checks: "", _auto: true });
      changed(true);
      const titles = document.querySelectorAll(".role-head input");
      titles[titles.length - 1].focus();
    } }, "+ Add a sign-off role")),
    switchRow("Different people for each role", "Nobody signs two roles or signs off a release they pushed. Turn off only for a solo trial.",
      s.gates.require_distinct_approvers, (v) => { s.gates.require_distinct_approvers = v; changed(); }),
    h("div", { class: "row" }, h("span", {}, "Sign-offs expire after (days)"),
      h("input", { type: "number", class: "small", min: 1, max: 90, value: s.gates.max_signoff_age_days,
        "aria-label": "Sign-off expiry in days",
        oninput: (e) => { const n = Number(e.target.value); s.gates.max_signoff_age_days = Number.isInteger(n) ? n : e.target.value; changed(); } })));

  const regionChips = preset.regions.length
    ? h("div", { class: "regions" }, ...preset.regions.map((r) => h("label", { class: "chip" },
        h("input", { type: "checkbox", checked: s.residency.allowed_regions.includes(r), onchange: (e) => {
          const set = new Set(s.residency.allowed_regions);
          if (e.target.checked) set.add(r); else set.delete(r);
          s.residency.allowed_regions = preset.regions.filter((x) => set.has(x));
          changed();
        } }), r)))
    : h("p", { class: "note" }, "This preset has no region rules. Pick a residency preset to restrict regions.");
  const residency = h("fieldset", {}, h("legend", {}, "Residency & security (G2)"),
    h("select", { "aria-label": "Residency preset", value: s.residency.preset, onchange: (e) => {
      s.residency.preset = e.target.value;
      const allowed = (state.presets[e.target.value] || { regions: [] }).regions;
      s.residency.allowed_regions = s.residency.allowed_regions.filter((r) => allowed.includes(r));
      changed(true);
    } }, ...Object.keys(state.presets).map((name) => h("option", { value: name }, name))),
    h("p", { class: "note" }, preset.description),
    h("div", { class: "row" },
      ...[["warn", "Warn: report what would block"], ["enforce", "Enforce: block the deploy"]].map(([v, label]) =>
        h("label", { class: "chip" }, h("input", { type: "radio", name: "enforcement", checked: s.residency.enforcement === v,
          onchange: () => { s.residency.enforcement = v; changed(); } }), label))),
    h("p", { class: "note" }, preset.regions.length ? "Allowed regions (none ticked: every region the preset allows)" : ""),
    regionChips);

  const prod = h("fieldset", {}, h("legend", {}, "Prod"),
    switchRow("Automatic rollback", "Restore the previous prod version when the new one fails its smoke checks.",
      s.prod.rollback_on_smoke_failure, (v) => { s.prod.rollback_on_smoke_failure = v; changed(true); }),
    h("p", { class: "note" }, "Drift check: is prod still running what the last release approved?"),
    scheduleField("drift", s.prod, "drift_check"));

  const harvest = h("fieldset", {}, h("legend", {}, "Learning from real use"),
    h("p", { class: "note" }, "Trace harvest: real conversations → candidate test cases → pull request for review."),
    scheduleField("harvest", s.harvest, "schedule"));

  document.getElementById("form").replaceChildren(environments, gates, signoffs, residency, prod, harvest);
}

// ---------------------------------------------------------------- preview

function stepBox(step) {
  const classes = ["step", step.on ? step.kind : "off", step.toggle ? "toggle" : ""].join(" ");
  const box = h("div", {
    class: classes, title: step.toggle ? (step.on ? "Click to turn off" : "Click to turn on") : null,
    tabindex: step.toggle ? "0" : null, role: step.toggle ? "button" : null,
    "aria-pressed": step.toggle ? String(step.on) : null,
  }, h("b", {}, step.title), h("small", {}, step.detail));
  if (step.roles) {
    box.append(h("div", { class: "roles" }, ...step.roles.map((r) =>
      h("span", { class: r.uat ? "uat" : "", title: `GitHub environment: ${r.environment}` }, r.title + (r.uat ? " · then UAT" : "")))));
  }
  if (step.toggle) {
    const flip = () => {
      const [section, key] = step.toggle.split(".");
      state.spec[section][key] = !step.on;
      if (step.toggle === "environments.test" && step.on) clearUat();
      changed(true);
    };
    box.addEventListener("click", flip);
    box.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(); } });
  }
  return box;
}

function renderDiagram(plan, stale) {
  const diagram = document.getElementById("diagram");
  diagram.style.opacity = stale ? "0.45" : "1";
  if (!plan) { diagram.replaceChildren(); return; }
  diagram.replaceChildren(...plan.map((lane) => h("div", { class: "lane" },
    h("div", { class: "lane-name" }, lane.lane),
    h("div", { class: "steps" }, ...lane.steps.map((step, i) => h("div", { class: "step-wrap" },
      i ? h("span", { class: "arrow", "aria-hidden": "true" }, "→") : null, stepBox(step)))))));
}

function diffBlock(text) {
  return h("pre", {}, ...text.split("\n").map((line) => {
    let cls = "";
    if (line.startsWith("@@")) cls = "hunk";
    else if (line.startsWith("+") && !line.startsWith("+++")) cls = "add";
    else if (line.startsWith("-") && !line.startsWith("---")) cls = "del";
    return h("span", { class: cls }, line + "\n");
  }));
}

function showProblems(problems) {
  const box = document.getElementById("problems");
  box.hidden = !problems.length;
  box.replaceChildren(h("b", {}, "Fix these before saving:"), h("ul", {}, ...problems.map((p) => h("li", {}, p))));
}

function renderPreview() {
  const p = state.preview;
  const save = document.getElementById("save");
  if (!p.ok) {
    showProblems(p.problems);
    renderDiagram(state.lastPlan, true);
    save.disabled = true;
    return;
  }
  showProblems([]);
  state.lastPlan = p.plan;
  renderDiagram(p.plan, false);
  document.getElementById("plan").textContent = p.text;
  const files = document.getElementById("files");
  if (!p.files.length) {
    files.replaceChildren(h("div", { class: "empty" }, "Nothing to change: pipeline.yaml, the workflows and the settings already match these choices."));
  } else {
    files.replaceChildren(...p.files.map((f) => h("details", {},
      h("summary", {}, h("code", {}, f.path), h("span", { class: `tag ${f.status}` }, f.status)), diffBlock(f.diff))));
  }
  save.disabled = !p.files.length;
  updateStatus();
}

async function runPreview() {
  const mine = ++sequence;
  try {
    const result = await api("/api/preview", { agent: state.agent, spec: cleanSpec() });
    if (mine !== sequence) return;
    state.preview = result;
    renderPreview();
  } catch (error) {
    showProblems([error.message]);
  }
}

function updateStatus() {
  const status = document.getElementById("status");
  const files = state.preview && state.preview.ok ? state.preview.files.length : null;
  let text;
  if (!state.exists) text = "No pipeline.yaml yet: showing the defaults";
  else if (isDirty()) text = "Unsaved changes";
  else if (files) text = `Saved settings, but ${files} generated file(s) are out of date`;
  else text = "pipeline.yaml and generated files are in sync";
  status.textContent = text;
  status.classList.toggle("dirty", !state.exists || isDirty() || Boolean(files));
}

// ---------------------------------------------------------------- load and save

async function loadAgent(name) {
  const data = await api(`/api/spec?agent=${encodeURIComponent(name)}`);
  state.agent = data.agent;
  state.kind = data.kind;
  state.exists = data.exists;
  state.presets = data.presets;
  state.spec = data.spec;
  state.spec.gates.signoffs = (state.spec.gates.signoffs || []).map((r) => ({ ...r }));
  state.spec.residency.allowed_regions = state.spec.residency.allowed_regions || [];
  state.custom = { drift: false, harvest: false };
  state.savedJson = JSON.stringify(cleanSpec());
  state.lastPlan = null;
  document.getElementById("kind").textContent = `${data.kind} agent`;
  document.getElementById("saved").hidden = true;
  renderForm();
  if (data.error) showProblems([data.error]);
  updateStatus();
  await runPreview();
}

async function loadAgents(select) {
  const { agents } = await api("/api/agents");
  state.agents = agents;
  const picker = document.getElementById("agent");
  picker.replaceChildren(...agents.map((a) => h("option", { value: a.name },
    a.name + (a.has_pipeline ? "" : " (no pipeline.yaml yet)"))));
  picker.value = select || (agents.find((a) => a.has_pipeline) || agents[0] || {}).name || "";
  return picker.value;
}

async function save() {
  const button = document.getElementById("save");
  button.disabled = true;
  const sent = cleanSpec();
  try {
    const result = await api("/api/save", { agent: state.agent, spec: sent });
    if (!result.ok) { showProblems(result.problems); return; }
    state.exists = true;
    state.savedJson = JSON.stringify(sent);
    const box = document.getElementById("saved");
    box.hidden = false;
    box.replaceChildren(
      h("b", {}, `Saved. ${result.written.length} file(s) written:`),
      h("ul", {}, ...result.written.map((p) => h("li", {}, h("code", {}, p)))),
      h("div", {}, "Next: review the changes, commit them and open a pull request. G1 checks the generated files match pipeline.yaml."),
      result.new_environments.length
        ? h("div", {}, "Create these GitHub environments, with each role's people as required reviewers: ",
          ...result.new_environments.map((e) => h("code", {}, e)))
        : null);
    await loadAgents(state.agent);
    await runPreview();
  } catch (error) {
    showProblems([error.message]);
  } finally {
    updateStatus();
  }
}

document.getElementById("save").addEventListener("click", save);
document.getElementById("revert").addEventListener("click", () => {
  if (!isDirty() || confirm("Discard your changes and reload pipeline.yaml?")) loadAgent(state.agent);
});
document.getElementById("agent").addEventListener("change", (e) => {
  if (state.exists && isDirty() && !confirm("Discard your unsaved changes?")) {
    e.target.value = state.agent;
    return;
  }
  loadAgent(e.target.value);
});
window.addEventListener("beforeunload", (e) => { if (state.exists && isDirty()) e.preventDefault(); });

loadAgents().then((name) => (name ? loadAgent(name) : showProblems(["No agent folders with an agent.json under cicd/agents."])))
  .catch((error) => showProblems([error.message]));
