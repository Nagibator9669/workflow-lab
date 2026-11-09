"use strict";

const $ = (id) => document.getElementById(id);
const titles = {intake: "Request intake", import: "Contact import", webhooks: "Webhook inbox"};
const statuses = {new: "To do", in_progress: "In progress", done: "Done"};
const sampleCSV = [
  "name,email,company",
  "  Mira Lane  , MIRA@example.com , Cedar Studio",
  "Rowan Bell,rowan@example.com,Northline",
  "Mira again,mira@example.com,Cedar Studio",
  "Jules Kim,not-an-email,Fieldwork",
  ",noor@example.com,Northline",
  "Noor Ellis,noor@example.com,Northline",
  "Taylor Reed,taylor@example.com,=1+1"
].join("\n");
let state = null;
let exportCSV = "";
let toastTimer;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function notify(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4500);
}

function showError(error) {
  $("error-banner").textContent = error.message || "Something went wrong. Please try again.";
  $("error-banner").hidden = false;
}

async function api(path, body, method = "POST") {
  const options = body === undefined ? {} : {
    method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
  };
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error("Cannot reach the local server. Start it with python app.py, then reload."); }
  const result = await response.json();
  if (!response.ok) {
    const message = Array.isArray(result.detail)
      ? result.detail.map((issue) => issue.loc.slice(1).join(" ") + ": " + issue.msg).join(". ")
      : result.detail;
    throw new Error(message || "The request could not be completed.");
  }
  return result;
}

async function action(buttons, work) {
  buttons.forEach((button) => { button.disabled = true; });
  $("error-banner").hidden = true;
  try { await work(); } catch (error) { showError(error); }
  finally { buttons.forEach((button) => { button.disabled = false; }); }
}

function formatDue(value) {
  const [year, month, day] = value.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString("en-GB", {day: "numeric", month: "short"});
}

function taskCard(task) {
  const card = element("article", "task-card");
  card.append(element("span", "priority " + task.priority, task.priority.charAt(0).toUpperCase() + task.priority.slice(1) + " priority"));
  card.append(element("h3", "", task.title));
  const meta = element("div", "task-meta");
  const owner = element("span", "task-owner");
  owner.append(element("span", "avatar", task.owner.charAt(0).toUpperCase()), document.createTextNode(task.owner));
  const due = element("time", "", formatDue(task.due_date));
  due.dateTime = task.due_date;
  due.title = "Due " + task.due_date;
  meta.append(owner, due);
  const select = element("select");
  select.setAttribute("aria-label", "Status for " + task.title);
  Object.entries(statuses).forEach(([value, label]) => {
    const option = element("option", "", label);
    option.value = value;
    select.append(option);
  });
  select.value = task.status;
  select.addEventListener("change", () => action([select], async () => {
    try {
      await api("/api/tasks/" + task.id, {status: select.value}, "PATCH");
      await refresh();
      notify("Task moved to " + statuses[select.value].toLowerCase() + ".");
    } catch (error) { select.value = task.status; throw error; }
  }));
  card.append(meta, select);
  return card;
}

function renderBoard() {
  const board = $("task-board");
  board.replaceChildren();
  $("task-count").textContent = state.metrics.total;
  Object.entries(statuses).forEach(([key, title]) => {
    const lane = element("section", "task-lane " + key);
    lane.setAttribute("aria-label", title);
    const heading = element("div", "lane-heading");
    const label = element("span");
    const dot = element("span", "lane-dot");
    dot.setAttribute("aria-hidden", "true");
    label.append(dot, document.createTextNode(title));
    heading.append(label, element("span", "", state.metrics[key]));
    lane.append(heading);
    const tasks = state.tasks.filter((task) => task.status === key);
    tasks.forEach((task) => lane.append(taskCard(task)));
    if (!tasks.length) lane.append(element("p", "lane-empty", "No tasks here yet."));
    board.append(lane);
  });
}

function renderAttempts() {
  const metrics = state.metrics;
  $("attempt-count").textContent = metrics.accepted + metrics.duplicate + metrics.rejected;
  const summary = $("delivery-summary");
  summary.replaceChildren();
  [["accepted", "accepted"], ["duplicate", "duplicates prevented"], ["rejected", "rejected or conflicting"]].forEach(([key, label]) => {
    const item = element("span");
    item.append(element("strong", "", metrics[key]), document.createTextNode(" " + label));
    summary.append(item);
  });
  const rows = $("attempt-rows");
  rows.replaceChildren();
  if (!state.attempts.length) {
    const row = element("tr");
    const cell = element("td", "attempt-empty", "No deliveries yet. Send an event to see the checks in action.");
    cell.colSpan = 3;
    row.append(cell);
    rows.append(row);
  }
  state.attempts.forEach((attempt) => {
    const row = element("tr");
    const outcome = element("td");
    const badgeClass = ["rejected", "conflict"].includes(attempt.outcome) ? "warning" : attempt.outcome === "duplicate" ? "neutral" : "";
    outcome.append(element("span", "result-badge " + badgeClass, attempt.outcome));
    const timestamp = new Date(attempt.created_at);
    row.append(element("td", "", attempt.event_id), outcome, element("td", "", timestamp.toLocaleTimeString("en-GB") + " · " + timestamp.toLocaleDateString("en-GB", {day: "numeric", month: "short"})));
    rows.append(row);
  });
}

function eventPreview() {
  if (state) $("event-json").textContent = JSON.stringify({id: $("event-id").value, type: "task.requested", task: state.sample_task}, null, 2);
}

async function refresh() {
  try {
    state = await api("/api/state");
    $("connection").textContent = "Connected";
    $("connection").className = "connection online";
    renderBoard();
    renderAttempts();
    eventPreview();
  } catch (error) {
    $("connection").textContent = "Offline";
    $("connection").className = "connection offline";
    throw error;
  }
}

function navigate() {
  if (location.hash === "#main") return;
  const panel = Object.hasOwn(titles, location.hash.slice(1)) ? location.hash.slice(1) : "intake";
  document.querySelectorAll(".demo-panel").forEach((section) => { section.hidden = section.id !== panel; });
  document.querySelectorAll("nav a").forEach((link) => {
    if (link.hash === "#" + panel) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  $("breadcrumb").textContent = titles[panel];
  document.title = titles[panel] + " · Workflow Lab";
  $("error-banner").hidden = true;
}

function invalidateIntake() {
  $("task-fields").disabled = true;
  $("review-note").textContent = "Preview this message to review its fields here.";
  $("review-note").className = "review-note";
}

function loadMessage(complete = true) {
  if (!state) return;
  const task = state.sample_task;
  $("message").value = complete
    ? task.title + " by " + task.due_date + ". Assign to " + task.owner + ". Priority: high."
    : "Please review the studio proposal. It needs someone to take ownership and agree on a date.";
  invalidateIntake();
}

$("load-complete").addEventListener("click", () => loadMessage());
$("load-missing").addEventListener("click", () => loadMessage(false));
$("message").addEventListener("input", invalidateIntake);
$("intake-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action([$("extract-button")], async () => {
    const result = await api("/api/intake/preview", {message: $("message").value});
    $("task-title").value = result.title;
    $("task-owner").value = result.owner;
    $("task-date").value = result.due_date;
    $("task-priority").value = result.priority;
    $("task-fields").disabled = false;
    $("review-note").className = "review-note" + (result.missing.length ? " attention" : "");
    const names = {title: "title", owner: "owner", due_date: "due date"};
    $("review-note").textContent = result.missing.length
      ? "Needs your input: " + result.missing.map((key) => names[key]).join(", ") + ". Nothing has been created."
      : "Fields found. Check the details before creating the task.";
    $("task-title").focus();
  });
});
$("task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  action([event.submitter], async () => {
    await api("/api/tasks", {title: $("task-title").value, owner: $("task-owner").value, due_date: $("task-date").value, priority: $("task-priority").value});
    invalidateIntake();
    $("review-note").textContent = "Task created and saved. Preview another message to keep going.";
    await refresh();
    notify("Task created. You’ll find it in To do.");
  });
});

function invalidateCSV() {
  exportCSV = "";
  $("import-result").hidden = true;
  $("import-empty").hidden = false;
}
function loadCSV() {
  $("csv-text").value = sampleCSV;
  $("csv-file").value = "";
  invalidateCSV();
}
$("load-csv").addEventListener("click", loadCSV);
$("csv-text").addEventListener("input", invalidateCSV);
$("csv-file").addEventListener("change", () => action([], async () => {
  const file = $("csv-file").files[0];
  if (!file) return;
  invalidateCSV();
  if (file.size > 120000) throw new Error("Choose a UTF-8 CSV smaller than 120 KB.");
  try { $("csv-text").value = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer()); }
  catch { throw new Error("This file is not valid UTF-8. Save it as CSV UTF-8 and try again."); }
  notify("File loaded. Check contacts to see the report.");
}));
$("import-form").addEventListener("submit", (event) => {
  event.preventDefault();
  invalidateCSV();
  action([event.submitter], async () => {
    const result = await api("/api/import/preview", {csv_text: $("csv-text").value});
    exportCSV = result.csv;
    $("ready-count").textContent = result.ready;
    $("review-count").textContent = result.review;
    $("safe-count").textContent = result.escaped;
    $("download-csv").disabled = result.ready === 0;
    $("table-caption").textContent = result.total > 50 ? "Showing first 50 of " + result.total + " rows · export includes all ready contacts" : result.total + " contacts checked";
    const table = $("contact-rows");
    table.replaceChildren();
    result.rows.slice(0, 50).forEach((contact) => {
      const row = element("tr");
      row.append(element("td", "", contact.row), element("td", "", contact.name || "—"), element("td", "", contact.email || "—"), element("td", "", contact.company || "—"));
      const cell = element("td");
      cell.append(element("span", "result-badge " + (contact.issues.length ? "warning" : ""), contact.issues.length ? "Needs review" : "Ready"));
      if (contact.issues.length) cell.append(element("span", "result-detail", contact.issues.join(" · ")));
      else if (contact.safe_export) cell.append(element("span", "result-detail", "Formula prefix escaped on export"));
      row.append(cell);
      table.append(row);
    });
    $("import-result").hidden = false;
    $("import-empty").hidden = true;
    notify(result.ready + " contacts ready. " + result.review + " need review.");
  });
});
$("download-csv").addEventListener("click", () => {
  if (!exportCSV) return;
  const url = URL.createObjectURL(new Blob([exportCSV], {type: "text/csv;charset=utf-8"}));
  const link = element("a");
  link.href = url;
  link.download = "workflow-lab-clean-contacts.csv";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  notify("Clean CSV downloaded. Rows needing review were excluded.");
});

async function sendEvent(signature) {
  if (!$("event-id").reportValidity()) return;
  await action([$("send-valid"), $("send-invalid")], async () => {
    const result = await api("/api/events/simulate", {event_id: $("event-id").value, signature});
    const receipt = $("receipt");
    receipt.replaceChildren();
    receipt.className = "receipt-empty " + result.outcome;
    receipt.append(element("div", "receipt-icon", {accepted: "✓", duplicate: "↻", rejected: "×", conflict: "!"}[result.outcome]));
    receipt.append(element("h3", "", {accepted: "Verified. Created.", duplicate: "Replay caught.", rejected: "Signature rejected.", conflict: "Event ID conflict."}[result.outcome]));
    receipt.append(element("p", "", result.message));
    receipt.append(element("p", "receipt-code", "HTTP " + result.http_status + (result.task_id ? " · Task #" + result.task_id : " · No task created")));
    await refresh();
  });
}
$("event-id").addEventListener("input", eventPreview);
$("send-valid").addEventListener("click", () => sendEvent("valid"));
$("send-invalid").addEventListener("click", () => sendEvent("invalid"));
$("reset-open").addEventListener("click", () => $("reset-dialog").showModal());
$("reset-dialog").addEventListener("close", () => {
  if ($("reset-dialog").returnValue !== "reset") return;
  action([$("reset-open")], async () => {
    await api("/api/reset", {});
    $("receipt").className = "receipt-empty";
    $("receipt").replaceChildren(element("div", "receipt-icon", "✓"), element("h3", "", "Waiting for an event"), element("p", "", "Send the same event twice. The second attempt keeps the original task."));
    invalidateIntake();
    await refresh();
    notify("Example workspace restored.");
  });
});

window.addEventListener("hashchange", navigate);
navigate();
loadCSV();
refresh().then(() => loadMessage()).catch(showError);
