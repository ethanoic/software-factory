"use strict";
// Factory board client. Renders /api/board every few seconds.
// Every piece of ticket data is untrusted (issue titles come from GitHub), so text is only ever set
// with textContent and links are only built from http(s) URLs. No innerHTML anywhere.

const POLL_MS = 5000;
const DOING = {
  ingest: "Cloning the repo", triage: "Triaging", plan: "Planning", freeze_tests: "Freezing the tests",
  generate: "Writing code", sandbox: "Running the tests", qa: "Security scan", repair: "Repairing",
  compile_pr: "Opening the PR",
};

let lastBody = "";
let lastOk = 0;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

function safeUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : null;
  } catch { return null; }
}

function link(url, label, cls) {
  const href = safeUrl(url);
  if (!href) return null;
  return el("a", { href, class: cls, target: "_blank", rel: "noopener noreferrer", text: label });
}

function ago(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const d = Math.round(s / 86400);
  return `${d} day${d === 1 ? "" : "s"} ago`;
}

function clock(iso) {
  return iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
}

function ref(card) {
  return card.ref || card.ticket_id;
}

// ------------------------------------------------------------------------------------------ vitals
function renderVitals(health) {
  const box = document.getElementById("vitals");
  const state = el("span", { class: `state${health.online ? "" : " down"}` },
    el("span", { class: "dot", "aria-hidden": "true" }),
    el("span", { text: health.online ? "Running" : "Not running" }));
  const items = [state];
  if (health.seconds_since_heartbeat !== null && health.seconds_since_heartbeat !== undefined) {
    const seen = new Date(Date.now() - health.seconds_since_heartbeat * 1000).toISOString();
    items.push(el("span", { text: `Last seen ${ago(seen)}` }));
  }
  if (health.daily_limit !== null && health.daily_limit !== undefined) {
    items.push(el("span", { text: `${health.runs_left} of ${health.daily_limit} runs left today` }));
  }
  if (health.repos && health.repos.length) {
    items.push(el("span", { text: `Watching ${health.repos.join(", ")}` }));
  }
  box.replaceChildren(...items);
}

// ------------------------------------------------------------------------------------------ your move
function renderStep(s) {
  const li = el("li", {}, el("span", { text: s.text }));
  const a = s.url ? link(s.url, s.url_label || "Open", "go") : null;
  if (a) li.append(" ", a);
  if (s.command) {
    const button = el("button", { type: "button", text: "Copy" });
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(s.command);
        button.textContent = "Copied";
      } catch {
        button.textContent = "Select and copy";
      }
      setTimeout(() => { button.textContent = "Copy"; }, 2000);
    });
    li.append(el("div", { class: "cmd" }, el("code", { text: s.command }), button));
  }
  return li;
}

function renderMoves(actions, cards) {
  const list = document.getElementById("moves");
  document.getElementById("moves-count").textContent = actions.length ? `(${actions.length})` : "";
  if (!actions.length) {
    list.replaceChildren(el("li", { class: "empty-moves" },
      el("strong", { text: "Nothing needs you right now." }),
      el("span", { text: "Add the factory:ready label to an issue to start a ticket." })));
    return;
  }
  const byId = new Map(cards.map((c) => [c.id, c]));
  list.replaceChildren(...actions.map((a) => {
    const card = a.card ? byId.get(a.card) : null;
    return el("li", { class: `move ${a.tone}`, id: a.card ? `move-${a.card}` : null },
      el("h3", { text: a.title }),
      card && card.title ? el("p", { class: "subject", text: card.title }) : null,
      el("p", { class: "why", text: a.why }),
      a.reason ? el("p", { class: "reason", text: `Reason given: ${a.reason}` }) : null,
      el("ol", { class: "steps" }, ...a.steps.map(renderStep)));
  }));
}

// ------------------------------------------------------------------------------------------ the line
function tagState(card) {
  switch (card.status) {
    case "queued": return { tone: "queued", text: `Number ${card.position} in the queue` };
    case "running": {
      const doing = DOING[card.stage] || "Working";
      return { tone: "moving", text: card.stage === "repair" ? `${doing}, attempt ${card.repair_attempts + 1} of 3` : doing };
    }
    case "parked": return { tone: "paused",
      text: `Paused: Claude plan limit. ${card.resume_at ? `Retries around ${clock(card.resume_at)}` : "Retries hourly"}` };
    case "halted": return { tone: "stopped", text: "Stopped. See Your move" };
    case "awaiting_approval": return { tone: "yours", text: "Waiting for your approval" };
    case "pr_open": return { tone: "yours", text: "PR waiting for your review" };
    default: return { tone: "moving", text: card.status };
  }
}

function renderTag(card) {
  const st = tagState(card);
  const links = el("span", { class: "links" },
    card.action ? link(`${location.origin}/#move-${card.id}`, "What to do", null) : null,
    link(card.issue_url, `Issue #${card.number}`, null),
    link(card.pr_url, "PR", null),
    link(card.trace_url, "Trace", null));
  if (card.action) {                                // stay on this page: jump to the matching instruction
    const jump = links.firstChild;
    jump.removeAttribute("target");
    jump.addEventListener("click", (e) => {
      e.preventDefault();
      const target = document.getElementById(`move-${card.id}`);
      if (target) { target.scrollIntoView({ block: "start" }); target.setAttribute("tabindex", "-1"); target.focus(); }
    });
  }
  return el("li", { class: `tag ${st.tone}`, role: "listitem" },
    el("span", { class: "ref", text: ref(card) }),
    card.title ? el("span", { class: "title", text: card.title }) : null,
    el("span", { class: "state", text: `${st.text}, ${ago(card.updated_at)}` }),
    links.childNodes.length ? links : null);
}

function renderLine(stations, cards) {
  const line = document.getElementById("line");
  line.replaceChildren(...stations.map((s) => {
    const here = cards.filter((c) => c.station === s.id);
    const tones = new Set(here.map((c) => tagState(c).tone));
    const cls = ["station",
      here.some((c) => c.status === "running") ? "has-work" : "",
      tones.has("yours") ? "has-yours" : "",
      tones.has("stopped") ? "has-stop" : ""].filter(Boolean).join(" ");
    return el("div", { class: cls, role: "group", "aria-label": `${s.name}: ${here.length} ticket${here.length === 1 ? "" : "s"}` },
      el("div", { class: "station-head" },
        el("span", { class: "station-name", text: s.name }),
        el("span", { class: "station-count", text: here.length ? String(here.length) : "" })),
      here.length ? el("ul", { class: "tags" }, ...here.map(renderTag)) : null);
  }));
}

// ------------------------------------------------------------------------------------------ finished
function renderFinished(items) {
  const list = document.getElementById("finished");
  if (!items.length) {
    list.replaceChildren(el("li", {}, el("span", { class: "none", text: "Nothing finished in the last 7 days." })));
    return;
  }
  list.replaceChildren(...items.map((c) => {
    const merged = c.status === "done";
    const outcome = merged ? "PR merged" : `Not taken: ${c.detail || "rejected at triage"}`;
    return el("li", {},
      el("span", { class: "ref", text: ref(c) }),
      el("span", { class: "title" }, el("span", { text: c.title }), " ",
        el("span", { class: `outcome${merged ? " merged" : ""}`, text: outcome })),
      el("span", { class: "when", text: ago(c.updated_at) }));
  }));
}

// ------------------------------------------------------------------------------------------ loop
async function refresh() {
  const notice = document.getElementById("notice");
  try {
    const res = await fetch("/api/board", { cache: "no-store" });
    const body = await res.text();
    if (!res.ok) throw new Error(JSON.parse(body).error || `HTTP ${res.status}`);
    lastOk = Date.now();
    notice.hidden = true;
    const data = JSON.parse(body);
    const comparable = body.replace(/"generated_at":"[^"]*"/, "").replace(/"seconds_since_heartbeat":\d+/, "");
    renderVitals(data.health);
    if (comparable !== lastBody) {                    // re-render only on change, so focus and "Copied" survive
      lastBody = comparable;
      renderMoves(data.actions, [...data.cards, ...data.finished]);
      renderLine(data.stations, data.cards);
      renderFinished(data.finished);
    }
  } catch (err) {
    notice.textContent = `The board can't load its data (${err.message}). Is the factory running? Try: make up`;
    notice.hidden = false;
  }
  document.getElementById("updated").textContent =
    lastOk ? `Updated ${new Date(lastOk).toLocaleTimeString()}` : "Not loaded yet";
}

refresh();
setInterval(refresh, POLL_MS);
