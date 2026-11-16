// SmartHealth documentation site: renders the generated data (docs/data/*.json, written by
// scripts/gen_docs_data.py and kept current by tests/meta/test_docs_data.py) and every
// Mermaid diagram. No build step. Served over http (GitHub Pages, or
// `python -m http.server -d docs` locally): browsers block fetch() from file://.
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs";

const dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
mermaid.initialize({ startOnLoad: false, theme: dark ? "dark" : "neutral", securityLevel: "loose",
  flowchart: { htmlLabels: true, curve: "basis" }, er: { layoutDirection: "TB" } });

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const table = (head, rows) =>
  `<div class="table-wrap"><table><thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead>` +
  `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
const repo = "https://github.com/sadainiqrar/smarthealth/blob/main/";
const src = (path) => `<a href="${repo}${esc(path)}"><code>${esc(path)}</code></a>`;
const id = (s) => s.replace(/[^A-Za-z0-9]/g, "_");

async function load(name) {
  const response = await fetch(`data/${name}`);
  if (!response.ok) throw new Error(`${name}: ${response.status}`);
  return response.json();
}

// -- API -------------------------------------------------------------------------------
function renderApi(openapi) {
  const byTag = {};
  for (const [path, ops] of Object.entries(openapi.paths)) {
    for (const [method, op] of Object.entries(ops)) {
      const tag = (op.tags || ["other"])[0];
      (byTag[tag] ||= []).push([method, path, op.summary || "", (op.description || "").split("\n\n")[0]]);
    }
  }
  const order = ["auth", "patients", "providers", "slots", "appointments", "waitlist", "clinical",
    "notifications", "analytics", "users", "system"];
  const tags = Object.keys(byTag).sort((a, b) => (order.indexOf(a) + 99) % 99 - (order.indexOf(b) + 99) % 99);
  $("#api-tables").innerHTML = tags.map((tag) =>
    `<h3>${esc(tag)}</h3>` + table(["Method", "Path", "What it does"], byTag[tag].map(([m, p, s, d]) =>
      [`<span class="pill m-${m}">${m.toUpperCase()}</span>`, `<code>${esc(p)}</code>`,
       `<b>${esc(s)}</b>${d ? `<br><span style="color:var(--soft)">${esc(d)}</span>` : ""}`]))).join("");
  $("#api-count").textContent = Object.values(byTag).flat().length;
}

function renderSwagger() {
  const target = $("#swagger");
  if (!target || !window.SwaggerUIBundle) return;
  window.SwaggerUIBundle({ url: "data/openapi.json", dom_id: "#swagger", docExpansion: "none",
    defaultModelsExpandDepth: 0, deepLinking: false, supportedSubmitMethods: [] });
}

// -- events, tasks, workflows ----------------------------------------------------------
function renderCatalog(catalog) {
  const consumersOf = {};
  for (const c of catalog.consumers) for (const t of c.topics) (consumersOf[t] ||= []).push(c);
  $("#kafka-topics").innerHTML = table(["Topic", "Kind", "Emitted by", "Consumed by", "Schema"],
    catalog.topics.map((t) => [
      `<code>${esc(t.name)}</code><br><span style="color:var(--faint)">v${t.schema_version}, ${t.partitions} partitions</span>`,
      t.kind === "state" ? "state (compacted)" : "event (7 days)",
      t.emitted_by.map(src).join("<br>"),
      (consumersOf[t.name] || []).map((c) => `<code>${esc(c.name)}</code>`).join("<br>") || "—",
      src(t.schema)]));
  $("#kafka-consumers").innerHTML = table(["Consumer group", "Runs in", "Reads", "Does"],
    catalog.consumers.map((c) => [`<code>${esc(c.name)}</code>`, esc(c.service),
      c.topics.map((t) => `<code>${esc(t)}</code>`).join("<br>"), `${esc(c.does)}<br>${src(c.code)}`]));

  // producer module -> topic -> consumer, drawn from the same data.
  const lines = ["flowchart LR"];
  const producers = new Set();
  lines.push('subgraph P["Part A modules (emit via the outbox)"]');
  for (const t of catalog.topics) for (const p of t.emitted_by) {
    const mod = p.split("/")[2] || p;
    if (!producers.has(mod)) { producers.add(mod); lines.push(`  p_${id(mod)}["${mod}"]`); }
  }
  lines.push("end", 'subgraph K["Kafka topics"]');
  for (const t of catalog.topics) lines.push(`  t_${id(t.name)}(["${t.name}"])`);
  lines.push("end");
  for (const svc of [...new Set(catalog.consumers.map((c) => c.service))]) {
    lines.push(`subgraph S_${id(svc)}["${svc}"]`);
    for (const c of catalog.consumers.filter((c) => c.service === svc)) lines.push(`  c_${id(c.name)}["${c.name}"]`);
    lines.push("end");
  }
  const seen = new Set();
  for (const t of catalog.topics) for (const p of t.emitted_by) {
    const edge = `p_${id(p.split("/")[2] || p)} --> t_${id(t.name)}`;
    if (!seen.has(edge)) { seen.add(edge); lines.push(edge); }
  }
  for (const c of catalog.consumers) for (const t of c.topics) lines.push(`t_${id(t)} --> c_${id(c.name)}`);
  $("#kafka-map").textContent = lines.join("\n");

  $("#celery-tasks").innerHTML = table(["Task", "Queue", "Schedule (Beat)", "Does"],
    catalog.celery_tasks.map((t) => [`<code>${esc(t.name)}</code>`, `<code>${esc(t.queue)}</code>`,
      t.schedule ? `every ${esc(t.schedule.replace(/^0:/, "").replace("1 day, 0:00:00", "24 h"))}` : "on demand", esc(t.does)]));
  $("#temporal-workflows").innerHTML = table(["Workflow", "Type name", "Does"],
    catalog.temporal_workflows.map((w) => [`<code>${esc(w.name)}</code>`, `<code>${esc(w.type)}</code>`, esc(w.does)]));
  $("#temporal-activities").innerHTML = catalog.temporal_activities.map((a) => `<code>${esc(a)}</code>`).join(" ");
  for (const [sel, n] of [["#n-topics", catalog.topics.length], ["#n-consumers", catalog.consumers.length],
    ["#n-workflows", catalog.temporal_workflows.length], ["#n-tasks", catalog.celery_tasks.length]]) {
    const el = $(sel); if (el) el.textContent = n;
  }
}

// -- data ------------------------------------------------------------------------------
function erDiagram(db, columns = true) {
  const lines = ["erDiagram"];
  const names = new Set(db.tables.map((t) => t.name));
  for (const t of db.tables) {
    if (!columns) { lines.push(`  ${t.name}`); continue; }
    lines.push(`  ${t.name} {`);
    for (const c of t.columns) {
      const type = c.type.split("(")[0].replace(/[^A-Za-z0-9_]/g, "_").toLowerCase() || "text";
      const keys = [c.primary_key && "PK", c.references.length && "FK"].filter(Boolean).join(",");
      lines.push(`    ${type} ${c.name}${keys ? " " + keys : ""}`);
    }
    lines.push("  }");
  }
  for (const t of db.tables) for (const c of t.columns) for (const ref of c.references) {
    const parent = ref.split(".")[0];
    if (names.has(parent)) lines.push(`  ${parent} ||--o{ ${t.name} : "${c.name}"`);
  }
  return lines.join("\n");
}

function renderSchema(schema) {
  const tabs = $("#db-tabs"), panes = $("#db-panes");
  schema.databases.forEach((db, i) => {
    const button = document.createElement("button");
    button.textContent = `${db.name} · ${db.tables.length} tables`;
    button.onclick = () => {
      tabs.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === button));
      panes.querySelectorAll(".db-pane").forEach((p, j) => (p.hidden = j !== i));
    };
    tabs.append(button);
    const pane = document.createElement("div");
    pane.className = "db-pane";
    pane.hidden = i !== 0;
    pane.innerHTML =
      `<p class="lede">Owner: <b>${esc(db.owner)}</b>. Schema managed: ${esc(db.migrations)}.</p>` +
      `<div class="diagram"><pre class="mermaid">${esc(erDiagram(db, false))}</pre>` +
      `<div class="caption">How the tables relate (foreign keys as lines), drawn from the code's own schema definitions.</div></div>` +
      (db.tables.length > 3 ? `<details><summary>Full diagram with every column</summary><div class="inner">` +
        `<div class="diagram" style="border:none"><pre class="mermaid">${esc(erDiagram(db))}</pre></div></div></details>` :
        `<div class="diagram"><pre class="mermaid">${esc(erDiagram(db))}</pre></div>`) +
      db.tables.map((t) => `<details><summary><code>${esc(t.name)}</code> — ${t.columns.length} columns` +
        `${t.doc ? ` · <span style="font-weight:400;color:var(--soft)">${esc(t.doc)}</span>` : ""}</summary><div class="inner">` +
        table(["Column", "Type", "Null", "Key / references"], t.columns.map((c) => [
          `<code>${esc(c.name)}</code>`, `<code>${esc(c.type)}</code>`, c.nullable ? "yes" : "no",
          [c.primary_key && "PK", c.unique && "unique", ...c.references.map((r) => `→ <code>${esc(r)}</code>`)]
            .filter(Boolean).join(" ")])) +
        (t.constraints.length || t.indexes.length
          ? `<p style="font-size:0.85rem;color:var(--soft)">Constraints: ${t.constraints.map((c) => `<code>${esc(c)}</code>`).join(" ") || "—"}` +
            `<br>Indexes: ${t.indexes.map((c) => `<code>${esc(c)}</code>`).join(" ") || "—"}</p>` : "") +
        `</div></details>`).join("");
    panes.append(pane);
    if (i === 0) button.classList.add("on");
  });
  $("#n-tables").textContent = schema.databases.reduce((n, db) => n + db.tables.length, 0);
}

// -- page behaviour --------------------------------------------------------------------
function navigation() {
  const links = [...document.querySelectorAll("nav.side a[href^='#']")];
  const observer = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) {
      links.forEach((a) => a.classList.toggle("on", a.getAttribute("href") === `#${e.target.id}`));
    }
  }, { rootMargin: "0px 0px -70% 0px" });
  document.querySelectorAll("main section[id]").forEach((s) => observer.observe(s));
  $(".menu").onclick = () => $("nav.side").classList.toggle("open");
  links.forEach((a) => a.addEventListener("click", () => $("nav.side").classList.remove("open")));
}

async function main() {
  navigation();
  try {
    const [openapi, catalog, schema] = await Promise.all(
      ["openapi.json", "catalog.json", "schema.json"].map(load));
    renderApi(openapi);
    renderCatalog(catalog);
    renderSchema(schema);
    renderSwagger();
  } catch (error) {
    document.querySelectorAll(".needs-data").forEach((el) => {
      el.innerHTML = `<div class="warn">Could not load the generated data (${esc(error.message)}). ` +
        `Open this site over http — GitHub Pages, or <code>python -m http.server -d docs</code> — not as a file.</div>`;
    });
  }
  // Render every diagram, including the ones generated above. Hidden panes are rendered
  // when first shown, since Mermaid cannot measure text inside display:none.
  await mermaid.run({ nodes: [...document.querySelectorAll(".mermaid")].filter((n) => n.offsetParent !== null) });
  document.addEventListener("click", async (e) => {
    if (e.target.closest("#db-tabs button") || e.target.closest("details > summary")) {
      await new Promise((r) => setTimeout(r, 30));
      const pending = [...document.querySelectorAll(".mermaid:not([data-processed])")].filter((n) => n.offsetParent !== null);
      if (pending.length) await mermaid.run({ nodes: pending });
    }
  });
}

main();
