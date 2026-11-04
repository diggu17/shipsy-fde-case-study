const state = { addressTab: "none", addressPick: null, addressSearch: "", accuracyOpen: false, planTab: "drivers", focusStop: null, editOpen: false, boardOpen: false, areaNames: [], view: "data", clients: [], shift: null, addresses: null, doc: null, draft: null, check: null, selected: null, dirty: false, accuracy: null, proposal: null };
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
let map = null, layers = null;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const problems = body?.detail?.problems;
    const error = new Error(problems ? problems.map((p) => p.message).join("; ") : `Request failed (${response.status})`);
    error.status = response.status;
    error.problems = problems ? problems.map((p) => p.path ? `${p.path}: ${p.message}` : p.message) : [error.message];
    throw error;
  }
  return body;
}
const json = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const shiftPath = () => `/clients/${encodeURIComponent($("client").value)}/hubs/${encodeURIComponent($("hub").value)}/shifts/${encodeURIComponent($("date").value)}`;

function show(text, ok = false) {
  const box = $("message");
  box.hidden = !text;
  box.className = ok ? "message ok" : "message";
  clearTimeout(show.timer);
  if (ok && text) show.timer = setTimeout(() => { box.hidden = true; }, 5000);
  if (Array.isArray(text)) box.innerHTML = `Fix these first:<ul>${text.slice(0, 12).map((t) => `<li>${esc(t)}</li>`).join("")}</ul>`;
  else box.textContent = text || "";
}
function busy(text) { $("status").textContent = text; }

async function guarded(action, working, done) {
  show("");
  busy(working || "");
  try {
    await action();
    if (done) show(done, true);
  } catch (error) {
    show(error.problems || error.message);
  }
  busy("");
  render();
}

async function loadClients() {
  state.clients = await api("/clients");
  const select = $("client");
  const current = select.value;
  select.innerHTML = state.clients.map((c) => `<option value="${esc(c.client_id)}">${esc(c.name)}</option>`).join("");
  if (state.clients.some((c) => c.client_id === current)) select.value = current;
  fillHubs();
}
function fillHubs() {
  const client = state.clients.find((c) => c.client_id === $("client").value);
  $("hub").innerHTML = (client?.hubs || []).map((h) => `<option>${esc(h)}</option>`).join("");
}
async function optional(path) {
  try { return await api(path); } catch (error) { if (error.status === 404 || error.status === 409) return null; throw error; }
}
async function loadShift() {
  state.shift = state.addresses = state.doc = state.draft = state.check = state.accuracy = null;
  state.selected = null; state.focusStop = null; state.dirty = false;
  if (!$("client").value || !$("date").value) return render();
  await guarded(async () => {
    state.shift = await optional(shiftPath());
    state.addresses = state.shift ? await optional(`${shiftPath()}/addresses`) : null;
    state.areaNames = (await optional(`/clients/${encodeURIComponent($("client").value)}/areas`)) || [];
    if (state.addresses) setDoc(await optional(`${shiftPath()}/plans/latest`));
  }, "Loading");
}
function setDoc(doc) {
  state.doc = doc;
  state.draft = doc ? structuredClone(doc.plan) : null;
  state.check = doc ? doc.check : null;
  state.dirty = false;
  if (doc && !doc.plan.routes.some((r) => r.route_id === state.selected)) state.selected = null;
}

function render() {
  document.querySelectorAll(".stages button").forEach((b) => b.toggleAttribute("aria-current", false));
  document.querySelector(`.stages button[data-view="${state.view}"]`).setAttribute("aria-current", "page");
  if (state.view !== "plan" && map) { map.remove(); map = null; }
  updateStages();
  ({ data: renderData, addresses: renderAddresses, plan: renderPlan, setup: renderSetup })[state.view]();
}

const PROBLEM_GROUPS = {
  weight_unit: ["Weight looks wrong for the box", "We suggest a corrected weight. If it looks right, use it."],
  impossible_weight: ["Weight is missing or impossible", "Enter the weight from the shop's record, or leave the parcel out."],
  too_big: ["Too big for every vehicle", "This is usually a wrong size or weight. Enter the right value, or leave the parcel out."],
};
const ISSUE_GROUPS = [["corrected", "Fixed automatically"], ["flagged", "Flagged for someone to look at"], ["rejected", "Rejected"]];

function updateStages() {
  const open = state.shift ? ["consignments", "drivers", "vehicles"].flatMap((f) => state.shift[f]).filter((r) => r.result === "held" && !r.decision).length : 0;
  const waiting = state.addresses ? state.addresses.assessments.filter((a) => a.verification && a.verification.status !== "answered").length : 0;
  const text = {
    data: state.shift ? `${state.shift.report.consignments.plannable} ready${open ? `, ${open} need a decision` : ""}` : "Nothing uploaded yet",
    addresses: !state.shift ? "Waiting for files" : state.addresses ? `${waiting} waiting on customers` : "Not checked yet",
    plan: state.doc ? `${state.doc.status === "approved" ? "Approved" : "Draft"}, version ${state.doc.version}` : "No plan yet",
  };
  Object.entries(text).forEach(([view, value]) => { document.querySelector(`[data-view="${view}"] small`).textContent = value; });
}
function parcelSummary(report) {
  const parts = [["accepted", "Nothing wrong"], ["corrected", "Fixed automatically"], ["flagged", "Flagged"], ["held", "Need your decision"], ["rejected", "Rejected"]];
  const total = parts.reduce((sum, [key]) => sum + report[key], 0);
  const blocked = report.accepted + report.corrected + report.flagged - report.plannable;
  return `<div class="summary-main"><div class="summary-number"><b>${report.plannable}</b><span>parcels ready to plan, from ${report.received} rows received</span></div>
    <div class="share-bar" aria-hidden="true">${parts.map(([key]) => `<span class="share-${key}" style="width:${report[key] / total * 100}%"></span>`).join("")}</div>
    <ul class="legend">${parts.map(([key, label]) => `<li><span class="key share-${key}"></span><b>${report[key]}</b> ${label}</li>`).join("")}${report.duplicates_removed ? `<li><b>${report.duplicates_removed}</b> Duplicates removed</li>` : ""}${blocked > 0 ? `<li><b>${blocked}</b> Flagged and can't go on a route</li>` : ""}</ul></div>`;
}
function renderData() {
  const held = state.shift ? ["consignments", "drivers", "vehicles"].flatMap((file) => state.shift[file].filter((r) => r.result === "held").map((r) => ({ ...r, file }))) : [];
  const undecided = held.filter((r) => !r.decision);
  const issues = state.shift ? state.shift.report.issues.filter((i) => i.result !== "held") : [];
  const form = `<form id="upload-form"><div class="files">
      <label>Consignments <input type="file" name="consignments" accept=".csv" required></label>
      <label>Drivers <input type="file" name="drivers" accept=".csv" required></label>
      <label>Vehicles <input type="file" name="vehicles" accept=".csv" required></label>
    </div><div class="actions"><button class="primary">Check these files</button></div></form>`;
  const groups = {};
  undecided.forEach((row) => { (groups[row.findings.find((f) => f.severity === "held")?.code || "other"] ||= []).push(row); });
  const report = state.shift?.report;
  $("view").innerHTML = `<div class="page">
  ${state.shift ? `<section class="sheet"><div class="sheet-head"><div><h2>What we found</h2><p>Upload ${esc(state.shift.version)} for ${esc(state.shift.plan_date)}. Every row is accounted for, and bad rows never stop the good ones.</p></div></div>
    <div class="summary">${parcelSummary(report.consignments)}
      <div class="summary-side">
        <div><b>${report.drivers.plannable} of ${report.drivers.received}</b><span>drivers usable${report.drivers.flagged ? `, ${report.drivers.flagged} flagged` : ""}</span></div>
        <div><b>${report.vehicles.plannable} of ${report.vehicles.received}</b><span>vehicles usable${report.vehicles.flagged ? `, ${report.vehicles.flagged} flagged` : ""}</span></div>
      </div></div></section>
  <section class="sheet"><h2>Needs your decision (${undecided.length})</h2>
    ${undecided.length ? `<p>These stay off the plan until you decide. Anything undecided is reported as unassigned, so nothing is planned on a guess.</p>${[...Object.keys(PROBLEM_GROUPS), ...Object.keys(groups).filter((code) => !PROBLEM_GROUPS[code])].filter((code) => groups[code]).map((code) => decisionGroup(code, groups[code])).join("")}` : `<p class="empty">Nothing is waiting for a decision.</p>`}
    ${held.length > undecided.length ? `<details class="group"><summary>Left out (${held.length - undecided.length})</summary>${held.filter((r) => r.decision).map((r) => `<p class="left-out"><strong>${esc(r.id)}</strong> ${esc(r.findings.map((f) => f.message).join("; "))}</p>`).join("")}</details>` : ""}
    <div class="next-step"><span>${undecided.length ? "You can continue now and come back to these." : "The files are ready."}</span><button class="primary" id="to-addresses">Continue to addresses</button></div>
  </section>
  <section class="sheet"><details><summary>Other messages (${issues.length})</summary>
    ${ISSUE_GROUPS.map(([result, label]) => { const items = issues.filter((i) => i.result === result); return items.length ? `<details class="group"><summary>${label} (${items.length})</summary>${items.map((i) => `<p class="issue"><strong>${esc(i.id)}</strong> ${i.messages.map(esc).join("<br>")}</p>`).join("")}</details>` : ""; }).join("")}
  </details></section>
  <section class="sheet"><details><summary>Replace today's files</summary><p class="hint">A new upload becomes the newest version, and earlier versions are kept.</p>${form}</details></section>`
    : `<section class="sheet"><h2>Check today's files</h2><p>Upload the shift's three files. Bad rows are fixed, flagged, or held for you, and the rest carry on.</p>${form}</section>`}
  <section class="sheet"><details><summary>Area list and delivery history</summary>
    <p class="hint">These change rarely. The address check needs them.</p>
    <form id="reference-form"><div class="files">
      <label>Area list <input type="file" name="areas" accept=".csv"></label>
      <label>Delivery history <input type="file" name="history" accept=".csv"></label>
    </div><div class="actions"><button>Upload these files</button></div></form>
  </details></section></div>`;
  $("upload-form").onsubmit = (event) => {
    event.preventDefault();
    const body = new FormData(event.target);
    guarded(async () => {
      await api(`${shiftPath()}/uploads`, { method: "POST", body });
      state.shift = await api(shiftPath());
      state.addresses = null; setDoc(null);
    }, "Checking files", "Files checked. Review anything that needs a decision.");
  };
  $("reference-form").onsubmit = (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const client = `/clients/${encodeURIComponent($("client").value)}`;
    guarded(async () => {
      const notes = [];
      if (form.get("areas").size) { const r = await api(`${client}/areas`, { method: "POST", body: file(form.get("areas")) }); notes.push(`${r.areas} areas saved`); }
      if (form.get("history").size) { const r = await api(`${client}/delivery-history`, { method: "POST", body: file(form.get("history")) }); notes.push(`${r.kept} of ${r.received} history rows kept`); }
      show(notes.join(". ") || "Choose a file first.", true);
    }, "Uploading");
  };
  if ($("to-addresses")) $("to-addresses").onclick = () => { state.view = "addresses"; render(); };
  document.querySelectorAll("[data-resolve]").forEach((button) => { button.onclick = () => resolve(button); });
}
const file = (blob) => { const data = new FormData(); data.append("file", blob); return data; };
function decisionGroup(code, rows) {
  const [title, advice] = PROBLEM_GROUPS[code] || ["Other rows to check", "Enter the right value, or leave the row out."];
  return `<div class="decision-group"><h3>${esc(title)} (${rows.length})</h3><p>${esc(advice)}</p>${rows.map(heldRow).join("")}</div>`;
}
function heldRow(row) {
  const message = row.findings.map((f) => f.message).join("; ");
  const tooBig = row.findings.some((f) => f.code === "too_big");
  const fields = row.file === "consignments"
    ? (tooBig ? [["size", "Size in cm, like 60x40x40"], ["weight_kg", "Weight in kg"]] : [["weight_kg", "Weight in kg"], ["size", "Size in cm, like 60x40x40"]])
    : row.file === "drivers" ? [["licence", "Licence"]] : [["type", "Vehicle type"]];
  const target = `data-file="${esc(row.file)}" data-id="${esc(row.id)}"`;
  const label = row.file === "consignments" ? typedAddress(row.id) : row.values?.name || row.file;
  return `<article class="decision">
    <div class="decision-what"><strong>${esc(row.id)}</strong> <span>${esc(label)}</span><p>${esc(message)}</p></div>
    <div class="decision-actions">
      ${row.suggestion ? `<button class="primary" data-resolve="accept_suggestion" ${target}>Use ${esc(Object.values(row.suggestion)[0])} kg</button>` : ""}
      <button data-resolve="leave_out" ${target}>Leave out</button>
      <details class="enter-value"><summary>Enter the right value</summary><div class="inline">
        <select aria-label="Value to change" data-field>${fields.map(([value, text]) => `<option value="${value}">${text}</option>`).join("")}</select>
        <input aria-label="New value" data-value>
        <button data-resolve="enter_values" ${target}>Save value</button></div></details>
    </div></article>`;
}
function resolve(button) {
  const action = button.dataset.resolve;
  const cell = button.closest(".decision");
  const body = { action };
  if (action === "enter_values") {
    const field = cell.querySelector("[data-field]").value;
    const value = cell.querySelector("[data-value]").value.trim();
    if (field === "size") {
      const sides = value.split(/[^0-9.]+/).filter(Boolean);
      if (sides.length !== 3) return show("Enter the size as three numbers, like 60x40x40.");
      body.values = { length_cm: sides[0], width_cm: sides[1], height_cm: sides[2] };
    } else {
      body.values = { [field]: value };
    }
  }
  guarded(async () => {
    await api(`${shiftPath()}/${button.dataset.file}/${encodeURIComponent(button.dataset.id)}/resolve`, json("POST", body));
    state.shift = await api(shiftPath());
  }, "Saving decision");
}

function verificationLabel(status) {
  return { whatsapp_triggered: "WhatsApp triggered", needs_agent: "Needs an agent", answered: "Customer answered" }[status];
}
const CONFIRM_TABS = [["none", "No location"], ["rough", "Rough location"], ["agent", "Needs an agent"], ["answered", "Answered"]];
function confirmTab(a) {
  if (a.verification.status === "answered") return "answered";
  if (a.verification.status === "needs_agent") return "agent";
  return a.routable ? "rough" : "none";
}
const NOTE_NAMES = { outside_service_area: "Outside the service area", note_conflicts_with_slot: "Note conflicts with the booked slot", note_conflicts_with_cod: "Note conflicts with cash on delivery", repeated_address_failures: "Repeated \"address not found\" failures" };

function renderAddresses() {
  if (!state.shift) { $("view").innerHTML = `<div class="page"><section class="sheet"><h2>Check addresses</h2><p class="empty">Upload today's files first.</p></section></div>`; return; }
  const data = state.addresses;
  const steps = `<ol class="method"><li><b>Past deliveries</b><span>Our own drivers have been to this door.</span></li><li><b>Geocoder</b><span>Accepted only if its result passes four checks.</span></li><li><b>Ask the customer</b><span>On WhatsApp, asking only for what's missing.</span></li></ol>`;
  if (!data) {
    $("view").innerHTML = `<div class="page"><section class="sheet"><h2>Check addresses</h2><p>Every address gets a location and a confidence level, in this order:</p>${steps}<div class="actions"><button class="primary" id="assess">Check addresses</button></div></section></div>`;
    wireAssess();
    return;
  }
  const rows = data.assessments;
  const waiting = rows.filter((a) => a.verification);
  const fromHistory = rows.filter((a) => a.source === "phone_and_area" || a.source === "same_place").length;
  const accepted = data.summary.by_geocoding.accepted || 0;
  const lookedUp = ["accepted", "not_found", "service_error", "rejected_not_specific", "rejected_partial_match", "rejected_too_far", "rejected_area_centre"].reduce((sum, key) => sum + (data.summary.by_geocoding[key] || 0), 0);
  const flagged = rows.filter((a) => a.findings.length);
  const notes = {};
  flagged.forEach((a) => a.findings.forEach((f) => { (notes[f.code] ||= []).push({ id: a.consignment_id, message: f.message }); }));
  $("view").innerHTML = `<div class="page">
  <section class="sheet"><div class="sheet-head"><div><h2>Check addresses</h2><p>Past deliveries first, then the geocoder, then the customer. An unclear address always has a next step.</p></div>
      <div class="actions"><button id="assess">Check addresses again</button><button class="primary" id="to-plan">Continue to the plan</button></div></div>
    <ol class="flow">
      <li><b>${rows.length}</b><span>Addresses checked</span></li>
      <li><b>${fromHistory}</b><span>Found in past deliveries</span></li>
      <li><b>${accepted}</b><span>Found by the geocoder, from ${lookedUp} lookups</span></li>
      <li class="ask"><b>${rows.length - fromHistory - accepted}</b><span>Need the customer to confirm</span></li>
    </ol>
    <p class="quality-line">${["GOOD", "INCOMPLETE", "AMBIGUOUS", "INVALID"].map((q) => `<span class="tag tag-${q}">${q[0] + q.slice(1).toLowerCase()} ${data.summary.by_quality[q] || 0}</span>`).join(" ")}</p>
  </section>
  <section class="sheet"><h2>WhatsApp confirmations (${waiting.length})</h2>
    <p>The customer is asked to confirm the date and what's missing. If there's no reply, a parcel with a location still goes out with a driver call, and one with no location moves to tomorrow. <strong>Simulated: no messages are sent from this prototype.</strong></p>
    <div class="confirm-tabs" id="confirm-tabs" role="tablist"></div>
    <div class="confirm-grid">
      <div class="confirm-left"><input id="confirm-search" type="search" aria-label="Find a parcel" placeholder="Find a parcel or address" value="${esc(state.addressSearch)}"><div class="confirm-list" id="confirm-list"></div></div>
      <div class="confirm-detail" id="confirm-detail"></div>
    </div>
  </section>
  <section class="sheet"><details><summary>Notes and area flags (${flagged.length})</summary>
    ${Object.entries(notes).map(([code, items]) => `<details class="group"><summary>${esc(NOTE_NAMES[code] || code)} (${items.length})</summary>${items.map((n) => `<p class="issue"><strong>${esc(n.id)}</strong> ${esc(n.message)}</p>`).join("")}</details>`).join("")}
  </details></section>
  <section class="sheet"><details id="accuracy-section" ${state.accuracyOpen ? "open" : ""}><summary>How accurate are these locations?</summary>
    <p class="hint">Compare with a file of verified locations. It's used only for this score, and never changes an address.</p>
    <form id="accuracy-form" class="inline"><input type="file" name="file" accept=".csv" required><button>Score accuracy</button></form>
    ${state.accuracy ? accuracyResult(state.accuracy) : ""}</details></section></div>`;
  wireAssess();
  $("to-plan").onclick = () => { state.view = "plan"; render(); };
  $("confirm-search").oninput = (event) => { state.addressSearch = event.target.value; paintConfirmations(); };
  $("accuracy-section").ontoggle = (event) => { state.accuracyOpen = event.target.open; };
  $("accuracy-form").onsubmit = (event) => {
    event.preventDefault();
    const body = new FormData(event.target);
    guarded(async () => { state.accuracy = await api(`${shiftPath()}/addresses/accuracy`, { method: "POST", body }); state.accuracyOpen = true; }, "Scoring");
  };
  paintConfirmations();
}
function wireAssess() {
  $("assess").onclick = () => guarded(async () => {
    state.addresses = await api(`${shiftPath()}/addresses`, { method: "POST" });
    state.areaNames = (await optional(`/clients/${encodeURIComponent($("client").value)}/areas`)) || [];
    setDoc(null);
    state.doc = null;
  }, "Checking addresses", "Addresses checked.");
}
function paintConfirmations() {
  const waiting = state.addresses.assessments.filter((a) => a.verification);
  const counts = Object.fromEntries(CONFIRM_TABS.map(([id]) => [id, waiting.filter((a) => confirmTab(a) === id).length]));
  if (!counts[state.addressTab]) state.addressTab = CONFIRM_TABS.find(([id]) => counts[id])?.[0] || "none";
  $("confirm-tabs").innerHTML = CONFIRM_TABS.map(([id, label]) => `<button role="tab" aria-selected="${state.addressTab === id}" data-confirm-tab="${id}" ${counts[id] ? "" : "disabled"}>${label} (${counts[id]})</button>`).join("");
  const search = state.addressSearch.trim().toLowerCase();
  const list = waiting.filter((a) => confirmTab(a) === state.addressTab && (!search || `${a.consignment_id} ${typedAddress(a.consignment_id)}`.toLowerCase().includes(search)));
  if (!list.some((a) => a.consignment_id === state.addressPick)) state.addressPick = list[0]?.consignment_id ?? null;
  $("confirm-list").innerHTML = list.length ? list.map((a) => `<button class="confirm-row ${a.consignment_id === state.addressPick ? "selected" : ""}" data-parcel="${esc(a.consignment_id)}">
      <strong>${esc(a.consignment_id)}</strong><span class="status-${esc(a.verification.status)}">${esc(verificationLabel(a.verification.status))}</span>
      <span class="confirm-address">${esc(typedAddress(a.consignment_id))}</span></button>`).join("") : `<p class="empty">Nothing matches.</p>`;
  const picked = list.find((a) => a.consignment_id === state.addressPick);
  $("confirm-detail").innerHTML = picked ? confirmDetail(picked) : "";
  document.querySelectorAll("[data-confirm-tab]").forEach((button) => { button.onclick = () => { state.addressTab = button.dataset.confirmTab; state.addressPick = null; paintConfirmations(); }; });
  document.querySelectorAll("[data-parcel]").forEach((button) => { button.onclick = () => { state.addressPick = button.dataset.parcel; paintConfirmations(); }; });
  document.querySelectorAll("[data-customer-area]").forEach((button) => {
    button.onclick = () => {
      const id = button.dataset.customerArea;
      const area = button.closest(".answer").querySelector("select").value;
      if (!area) return show("Choose the customer's area first.");
      guarded(async () => {
        await api(`${shiftPath()}/addresses/${encodeURIComponent(id)}/customer-area`, json("POST", { area_name: area }));
        state.addresses = await api(`${shiftPath()}/addresses`);
      }, "Saving", `${id} updated. Create a new plan to use this answer.`);
    };
  });
  document.querySelectorAll("[data-confirm]").forEach((button) => {
    button.onclick = () => {
      const cell = button.closest(".answer");
      const body = { lat: Number(cell.querySelector("[data-lat]").value), lng: Number(cell.querySelector("[data-lng]").value) };
      guarded(async () => {
        await api(`${shiftPath()}/addresses/${encodeURIComponent(button.dataset.confirm)}/confirm`, json("POST", body));
        state.addresses = await api(`${shiftPath()}/addresses`);
      }, "Saving", `${button.dataset.confirm} confirmed.`);
    };
  });
}
function confirmDetail(a) {
  const id = a.consignment_id;
  const status = a.verification.status;
  const phone = rowFor(id)?.values?.customer_phone;
  const areas = a.area_options?.length ? a.area_options : state.areaNames;
  const facts = [
    ["Address as typed", typedAddress(id)],
    ["Why we're asking", a.reason],
    ["If there's no reply", a.routable ? "Goes on a route, and the driver calls first" : "Moves to tomorrow"],
  ];
  return `<div class="detail-head"><h3>${esc(id)}</h3><span class="tag tag-${esc(a.quality)}">${esc(a.quality[0] + a.quality.slice(1).toLowerCase())}</span><span class="status-${esc(status)}">${esc(verificationLabel(status))}</span></div>
    ${status === "needs_agent" ? `<p class="agent-note">This parcel has no phone number, so an agent needs to contact the shop.</p>` : ""}
    ${a.verification.message ? `<div class="chat"><span class="chat-to">To ${esc(phone || "no phone number")}</span><div class="bubble">${esc(a.verification.message)}</div><span class="chat-note">Simulated, not sent</span></div>` : ""}
    <dl class="facts">${facts.map(([term, value]) => `<dt>${esc(term)}</dt><dd>${esc(value)}</dd>`).join("")}</dl>
    ${status === "answered" ? `<p class="answered">Answered. ${a.area ? `Area: ${esc(a.area)}.` : ""} Create a new plan to use this answer.</p>` : `<div class="answer"><label>Customer's answer <select aria-label="Customer's area" data-area-for="${esc(id)}"><option value="">Choose area</option>${areas.map((name) => `<option>${esc(name)}</option>`).join("")}</select></label><button class="primary" data-customer-area="${esc(id)}">Save answer</button>
      <details><summary>Enter a location instead</summary><div class="inline"><input aria-label="Latitude" placeholder="Latitude" data-lat><input aria-label="Longitude" placeholder="Longitude" data-lng><button data-confirm="${esc(id)}">Confirm</button></div></details></div>`}`;
}
function accuracyResult(result) {
  const row = (label, s) => `<tr><td>${esc(label)}</td><td class="number">${s.located} of ${s.verified}</td><td class="number">${s.median_m ?? "-"} m</td><td class="number">${s.within_100m}</td><td class="number">${s.within_250m}</td><td class="number">${s.worst_m ?? "-"} m</td></tr>`;
  const base = result.area_centre_baseline, ours = result.overall;
  const most = Math.max(base.median_m || 0, ours.median_m || 0, 1);
  const bar = (label, s, kind) => `<div class="accuracy-bar"><span>${esc(label)}</span><div><i class="${kind}" style="width:${(s.median_m || 0) / most * 100}%"></i></div><b>${s.median_m ?? "-"} m</b></div>`;
  return `<div class="accuracy-bars"><h4>Median distance from the true door</h4>${bar("Area centre alone", base, "base")}${bar("Our address check", ours, "ours")}</div>
  <table><thead><tr><th>Group</th><th class="number">Located</th><th class="number">Median error</th><th class="number">Within 100 m</th><th class="number">Within 250 m</th><th class="number">Worst error</th></tr></thead><tbody>
  ${row("Area centre alone", base)}${row("Our address check", ours)}${Object.entries(result.by_quality).map(([q, s]) => row(q[0] + q.slice(1).toLowerCase(), s)).join("")}</tbody></table>`;
}

const minutes = (time) => { const [h, m] = time.split(":").map(Number); return h * 60 + m; };
const BOARD_START = 8 * 60, BOARD_END = 22 * 60;
const x = (time) => `${Math.min(100, Math.max(0, (time - BOARD_START) / (BOARD_END - BOARD_START) * 100))}%`;
const money = (value) => `${esc(state.doc?.settings?.currency || "")} ${Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;

const ROUTE_COLOURS = ["#0f7c7a", "#c2410c", "#4338ca", "#b91c7c", "#15803d", "#a16207", "#0369a1", "#7e22ce", "#be123c", "#4d7c0f", "#0e7490", "#9a3412", "#1d4ed8", "#86198f", "#166534", "#854d0e", "#334155", "#6d28d9", "#9f1239", "#047857"];
const ROAD_SERVICE = "https://router.project-osrm.org/route/v1/driving/";
const roadLines = new Map();
let drawCount = 0;

const routeColour = (id) => {
  const index = state.draft.routes.findIndex((r) => r.route_id === id);
  return ROUTE_COLOURS[Math.max(index, 0) % ROUTE_COLOURS.length];
};
const driverName = (id) => state.shift.drivers.find((d) => d.id === id)?.values?.name || id;
const vehicleLabel = (id) => {
  const type = state.shift.vehicles.find((v) => v.id === id)?.values?.type || "";
  const words = type.replace(/_/g, " ").toLowerCase();
  return words ? words[0].toUpperCase() + words.slice(1) : id;
};
const parcelCount = (route) => route.stops.reduce((total, stop) => total + stop.consignment_ids.length, 0);
const rowFor = (id) => state.shift.consignments.find((r) => r.id === id);
const assessmentFor = (id) => state.addresses.assessments.find((a) => a.consignment_id === id);
function typedAddress(id) {
  const values = rowFor(id)?.values || {};
  return [values.address_line_1, values.address_line_2, values.area].filter(Boolean).join(", ") || "No address given";
}
function slotText(id) {
  const values = rowFor(id)?.values || {};
  return values.slot_start && values.slot_end ? `${values.slot_start} to ${values.slot_end}` : "Any time";
}

function renderPlan() {
  if (!state.addresses) { $("view").innerHTML = `<section class="sheet"><h2>Review and approve the plan</h2><p class="empty">Check the addresses first.</p></section>`; return; }
  if (!state.draft) {
    $("view").innerHTML = `<section class="sheet"><h2>Review and approve the plan</h2><p>Create a plan for this shift. It takes about a minute.</p><div class="actions"><button class="primary" id="generate">Create the plan</button></div></section>`;
    $("generate").onclick = generate;
    return;
  }
  const metrics = state.check.metrics;
  const hard = state.check.violations.filter((v) => v.severity === "hard");
  const approved = state.doc.status === "approved" && !state.dirty;
  const accepted = state.doc.approved_with_alerts?.length || 0;
  const stateText = state.dirty ? "Unsaved changes" : approved ? (accepted ? `Approved with ${accepted} ${accepted === 1 ? "alert" : "alerts"}` : "Approved") : "Draft";
  const figure = (value, label, warn) => `<div class="${warn ? "warn" : ""}"><b>${value}</b><span>${label}</span></div>`;
  $("view").innerHTML = `
  <div class="plan-page">
    <div class="plan-head">
      <div class="plan-state"><strong class="state-${approved ? "approved" : state.dirty ? "dirty" : "draft"}">${stateText}, version ${state.doc.version}</strong>
        <span class="${hard.length ? "blocked" : "clear"}">${hard.length ? `${hard.length} over a limit, your call` : "No limits broken"}</span></div>
      <div class="figures">
        ${figure(metrics.routed, "On routes")}
        ${figure(metrics.unassigned, "Unassigned", metrics.unassigned)}
        ${figure(metrics.late.count, "Late", metrics.late.count)}
        ${figure(`${Math.round(metrics.overtime.minutes)} min`, "Overtime")}
        ${figure(money(Math.round(metrics.total_cost)), "Total cost")}
        ${figure(money(metrics.cost_per_routed || 0), "Per parcel")}
        ${figure(esc(state.doc.failure?.expected_failed ?? "-"), "Expected failures")}
      </div>
      <div class="plan-actions">
        ${state.dirty ? `<button id="undo">Undo changes</button><button id="save">Save changes</button>` : ""}
        <button class="approve" id="approve" ${approved ? "disabled" : ""}>${approved ? "Plan approved" : "Approve the plan"}</button>
        <details class="menu" id="more-menu"><summary>More</summary><div class="menu-items">
          <button id="export">Export the plan</button>
          <button id="absent-open">A driver can't work</button>
          <button id="generate">Create a new plan</button>
        </div></details>
      </div>
    </div>
    ${state.doc.method === "backup" ? `<div class="plan-alert">This is the simple backup plan, because the solver didn't finish: ${esc(state.doc.solver_error)}</div>` : ""}
    ${hard.length ? `<div class="plan-alert"><strong>Over a limit.</strong> You can still approve this plan, and the call is yours.${hard.map(problem).join("")}</div>` : ""}
    <div class="plan-grid" id="plan-grid">
      <aside class="side" id="side-panel"></aside>
      <div class="map-wrap"><div id="map"></div><div class="map-key" id="map-key"></div></div>
      <aside class="deliveries" id="route-panel"></aside>
    </div>
    <details class="board-wrap" ${state.boardOpen ? "open" : ""}><summary>Time board, every driver across the day</summary><section class="board" id="board" aria-label="Routes across the day"></section></details>
  </div>`;
  document.querySelector(".board-wrap").ontoggle = (event) => { state.boardOpen = event.target.open; };
  wirePlan();
  paintPanels();
  startMap();
  drawRoutes(true);
}
function problem(v) {
  return `<button class="problem ${v.severity}" ${v.route_id ? `data-route="${esc(v.route_id)}"` : ""}>${esc(v.message)}</button>`;
}
function paintPanels() {
  $("plan-grid").classList.toggle("driver-chosen", Boolean(state.selected));
  $("side-panel").innerHTML = sidePanel();
  $("route-panel").innerHTML = state.selected ? routePanel() : "";
  $("board").innerHTML = board();
  wirePanels();
}
function sidePanel() {
  const soft = state.check.violations.filter((v) => v.severity === "soft" && v.rule !== "unassigned");
  const tabs = [["drivers", `Drivers (${state.draft.routes.length})`], ["issues", `Issues (${soft.length})`], ["unassigned", `Unassigned (${state.draft.unassigned.length})`]];
  const body = { drivers: driverList, issues: () => issueList(soft), unassigned: unassignedList }[state.planTab]();
  return `<div class="tabs" role="tablist">${tabs.map(([id, label]) => `<button role="tab" aria-selected="${state.planTab === id}" data-tab="${id}">${label}</button>`).join("")}</div><div class="side-body">${body}</div>`;
}
function driverList() {
  const times = Object.fromEntries(state.check.routes.map((r) => [r.route_id, r]));
  return `<button class="driver-row ${state.selected ? "" : "selected"}" data-all-drivers><span class="driver-main">All drivers</span><span class="driver-sub">The whole plan on the map</span></button>` + state.draft.routes.map((route) => {
    const check = times[route.route_id];
    const late = check?.late_consignments?.length || 0;
    const overtime = Math.round(check?.overtime_minutes || 0);
    return `<button class="driver-row ${route.route_id === state.selected ? "selected" : ""}" data-route="${esc(route.route_id)}">
      <span class="swatch" style="background:${routeColour(route.route_id)}"></span>
      <span class="driver-main">${esc(driverName(route.driver_id))}</span>
      <span class="driver-sub">${esc(vehicleLabel(route.vehicle_id))}, ${parcelCount(route)} parcels${check ? `, back ${esc(check.return_hub)}` : ""}</span>
      ${late || overtime ? `<span class="driver-flags">${late ? `<span class="flag late">${late} late</span>` : ""}${overtime ? `<span class="flag">${overtime} min overtime</span>` : ""}</span>` : ""}
    </button>`;
  }).join("");
}
function issueList(soft) {
  if (!soft.length) return `<p class="empty">No late deliveries or overtime.</p>`;
  const names = { late: "Late deliveries", overtime: "Drivers on overtime" };
  const groups = {};
  soft.forEach((v) => { (groups[v.rule] ||= []).push(v); });
  return Object.entries(groups).map(([rule, items]) => `<details class="group" open><summary>${esc(names[rule] || rule)} (${items.length})</summary>
    ${items.map((v) => `<button class="problem soft" ${v.route_id ? `data-route="${esc(v.route_id)}"` : ""}><strong>${esc(v.route_id ? driverName(state.draft.routes.find((r) => r.route_id === v.route_id)?.driver_id) : "")}</strong> ${esc(v.message)}</button>`).join("")}</details>`).join("");
}
function unassignedGroup(entry) {
  const reason = entry.reason || "";
  if (reason.startsWith("Waiting for the customer")) return ["Waiting for the customer", "Customer, then an agent. Moves to tomorrow if there's no reply."];
  if (reason.startsWith("Needs a decision")) return ["Waiting for your decision", "Hub manager, on page 1."];
  if (reason.startsWith("Rejected")) return ["Rejected rows", "Nobody. These aren't real orders."];
  if (reason.includes("cash limit")) return ["Over the cash limit", "Hub manager. Contact the customer about payment."];
  if (reason === "Moved off the route by the manager") return [reason, "Hub manager. Add it to a route, or move it to tomorrow."];
  if (rowFor(entry.consignment_id)?.values?.handling === "CHILLED") return ["Chilled, and the chilled van is full", "Hub manager. Add a chilled van, or move to tomorrow."];
  return ["Ran out of time on the road", "Hub manager. Add it to a route, or move to tomorrow."];
}
function unassignedList() {
  if (!state.draft.unassigned.length) return `<p class="empty">Every parcel is on a route.</p>`;
  const groups = new Map();
  state.draft.unassigned.forEach((entry) => {
    const [name, next] = unassignedGroup(entry);
    if (!groups.has(name)) groups.set(name, { next, items: [] });
    groups.get(name).items.push(entry);
  });
  return [...groups.entries()].map(([name, group]) => `<details class="group" open><summary>${esc(name)} (${group.items.length})</summary><p class="group-next">${esc(group.next)}</p>${group.items.map(unassignedRow).join("")}</details>`).join("");
}
function ask(title, body, yes) {
  $("decide-title").textContent = title;
  $("decide-body").innerHTML = body;
  $("decide-yes").textContent = yes;
  $("decide").showModal();
  return new Promise((resolve) => {
    const finish = (answer) => { $("decide").close(); resolve(answer); };
    $("decide-yes").onclick = () => finish(true);
    $("decide-no").onclick = () => finish(false);
    $("decide").oncancel = (event) => { event.preventDefault(); finish(false); };
  });
}
function alertList(alerts) {
  return `<ul class="alert-list">${alerts.map((a) => `<li>${esc(a.message)}</li>`).join("")}</ul>`;
}
function hoursText(value) { return value === undefined ? "-" : `${(value / 60).toFixed(1)} h`; }
function moveTable(before, after) {
  const line = (label, now, then) => `<tr><td>${label}</td><td class="number">${now}</td><td class="number"><strong>${then}</strong></td></tr>`;
  const route = after.working_minutes !== undefined ? line("Working time", hoursText(before.working_minutes), hoursText(after.working_minutes))
    + line("Back at the hub", esc(before.return_hub ?? "-"), esc(after.return_hub ?? "-"))
    + line("Overtime", `${Math.round(before.overtime_minutes ?? 0)} min`, `${Math.round(after.overtime_minutes)} min`) : "";
  return `<table><thead><tr><th></th><th class="number">Now</th><th class="number">After</th></tr></thead><tbody>${route}
    ${line("Late deliveries in the plan", before.late, after.late)}${line("Plan cost", money(before.total_cost), money(after.total_cost))}</tbody></table>`;
}
async function moveParcel(id, routeId) {
  const target = state.draft.routes.find((r) => r.route_id === routeId);
  const name = target ? driverName(target.driver_id) : "";
  let result;
  try {
    result = await api(`${shiftPath()}/plans/latest/preview-move`, json("POST", { plan: state.draft, consignment_id: id, route_id: routeId }));
  } catch (error) { show(error.problems || error.message); render(); return; }
  if (result.alerts.length) {
    const body = `<p>Doing this will lead to:</p>${alertList(result.alerts)}${moveTable(result.before, result.after)}<p class="hint">The limits are here to inform you. The call is yours.</p>`;
    if (!await ask(`Moving ${id} to ${name || "unassigned"}`, body, "Move anyway")) { render(); return; }
  }
  state.draft = result.plan;
  state.check = result.check;
  state.dirty = true;
  state.planTab = "drivers";
  if (routeId) { state.selected = routeId; state.focusStop = result.position; }
  else { state.focusStop = null; if (!result.plan.routes.some((r) => r.route_id === state.selected)) state.selected = null; }
  show(routeId ? `${id} moved to ${name}.` : `${id} moved off the plan.`, true);
  render();
}
function routeOptions(except) {
  return state.draft.routes.filter((r) => r.route_id !== except).map((r) => `<option value="${esc(r.route_id)}">${esc(driverName(r.driver_id))}</option>`).join("");
}
function unassignedRow(entry) {
  const canRoute = assessmentFor(entry.consignment_id)?.routable;
  return `<div class="unassigned-row"><strong>${esc(entry.consignment_id)}</strong><span>${esc(typedAddress(entry.consignment_id))}</span>
    ${canRoute ? `<select aria-label="Route for ${esc(entry.consignment_id)}" data-add="${esc(entry.consignment_id)}"><option value="">Add to a route</option>${routeOptions()}</select>` : ""}</div>`;
}
function routePanel() {
  const route = state.draft.routes.find((r) => r.route_id === state.selected);
  const times = state.check.routes.find((r) => r.route_id === state.selected);
  if (!route) return "";
  const drivers = state.shift.drivers.filter((d) => d.result !== "rejected");
  const vehicles = state.shift.vehicles.filter((v) => v.result !== "rejected");
  const stops = times ? times.stops : route.stops;
  return `<div class="route-title"><span class="swatch big" style="background:${routeColour(route.route_id)}"></span>
      <div><h3>${esc(driverName(route.driver_id))}</h3><span class="driver-sub">${esc(vehicleLabel(route.vehicle_id))}, route ${esc(route.route_id)}, ${esc(route.driver_id)}, ${esc(route.vehicle_id)}</span></div>
      <button class="link" data-all-drivers>All drivers</button></div>
    ${times ? `<p class="route-facts">Leaves ${esc(times.depart_hub)}, back ${esc(times.return_hub)}<br>${parcelCount(route)} parcels, ${stops.length} stops, ${times.distance_km} km, ${money(times.cost.total)}</p>` : ""}
    <details class="route-edit" id="route-edit" ${state.editOpen ? "open" : ""}><summary>Change driver, vehicle, or departure</summary><div class="route-fields">
      <label>Driver <select id="route-driver">${drivers.map((d) => `<option value="${esc(d.id)}" ${d.id === route.driver_id ? "selected" : ""}>${esc(d.values?.name || d.id)}</option>`).join("")}</select></label>
      <label>Vehicle <select id="route-vehicle">${vehicles.map((v) => `<option value="${esc(v.id)}" ${v.id === route.vehicle_id ? "selected" : ""}>${esc(v.id)} ${esc(vehicleLabel(v.id))}</option>`).join("")}</select></label>
      <label>Leaves at <input type="time" id="route-depart" value="${esc(route.depart_hub)}"></label>
    </div></details>
    <div class="delivery-list">${stops.map((stop, index) => delivery(stop, index, stops.length, route, times)).join("")}</div>`;
}
function delivery(stop, index, total, route, times) {
  const late = times?.late_consignments || [];
  const ids = stop.consignment_ids;
  const isLate = ids.some((c) => late.includes(c));
  const approximate = ids.some((c) => assessmentFor(c)?.quality !== "GOOD");
  const focused = state.focusStop === index;
  const waits = stop.eta && stop.delivery_start && stop.eta !== stop.delivery_start;
  const action = assessmentFor(ids[0])?.action;
  return `<article class="delivery ${focused ? "focused" : ""}" id="delivery-${index}">
    <button class="delivery-head" data-stop="${index}">
      <span class="seq ${isLate ? "late" : approximate ? "approximate" : ""}">${esc(stop.seq)}</span>
      <span class="delivery-time">${esc(stop.delivery_start || stop.eta || "")}</span>
      <span class="pill ${isLate ? "late" : "ok"}">${stop.eta ? (isLate ? "Late" : "On time") : "Not checked"}</span>
      ${approximate ? `<span class="pill approximate">Approximate location</span>` : ""}
      <span class="delivery-address">${esc(typedAddress(ids[0]))}</span>
      <span class="delivery-meta">${ids.map(esc).join(", ")}, slot ${esc(slotText(ids[0]))}${waits ? `, arrives ${esc(stop.eta)} and waits` : ""}</span>
    </button>
    ${focused ? `<div class="delivery-more">
      ${action && action !== "None" ? `<p>${esc(action)}</p>` : ""}
      ${ids.map((c) => `<div class="reassign"><span>${esc(c)}${ids.length > 1 ? `, ${esc(typedAddress(c))}` : ""}</span><select aria-label="Deliver ${esc(c)} with" data-reassign="${esc(c)}"><option value="">Deliver with another driver</option>${routeOptions(route.route_id)}<option value="__none">Take off the plan</option></select></div>`).join("")}
      <div class="controls">
        <button data-move="-1" data-index="${index}" ${index === 0 ? "disabled" : ""} aria-label="Move stop earlier">Earlier</button>
        <button data-move="1" data-index="${index}" ${index === total - 1 ? "disabled" : ""} aria-label="Move stop later">Later</button>
      </div></div>` : ""}
  </article>`;
}
function board() {
  const hours = [];
  for (let h = 8; h <= 22; h += 2) hours.push(`<span class="tick" style="left:${x(h * 60)}">${String(h).padStart(2, "0")}:00</span>`);
  const shiftOf = (id) => state.shift.drivers.find((d) => d.id === id)?.values;
  return `<div class="board-row head"><span></span><div class="track" style="background:none">${hours.join("")}</div></div>` + state.check.routes.map((route) => {
    const shift = shiftOf(route.driver_id);
    const shiftBar = shift?.shift_start && shift?.shift_end ? `<div class="shift" style="left:${x(minutes(shift.shift_start))};width:calc(${x(minutes(shift.shift_end))} - ${x(minutes(shift.shift_start))})"></div>` : "";
    const start = minutes(route.depart_hub), end = minutes(route.return_hub);
    const dots = route.stops.map((s) => `<span class="dot ${s.consignment_ids.some((c) => route.late_consignments.includes(c)) ? "late" : ""}" style="left:${x(minutes(s.delivery_start))}" title="${esc(s.consignment_ids.join(", "))} at ${esc(s.delivery_start)}"></span>`).join("");
    return `<div class="board-row ${route.route_id === state.selected ? "selected" : ""}"><button class="board-label" data-route="${esc(route.route_id)}">${esc(driverName(route.driver_id))}</button>
      <div class="track">${shiftBar}<div class="bar" style="left:${x(start)};width:calc(${x(end)} - ${x(start)})"></div>${dots}</div></div>`;
  }).join("");
}
function selectRoute(id) {
  state.selected = id;
  state.focusStop = null;
  state.planTab = "drivers";
  paintPanels();
  map?.invalidateSize({ animate: false });
  drawRoutes(true);
}
function focusStop(index, fromMap) {
  state.focusStop = state.focusStop === index && !fromMap ? null : index;
  paintPanels();
  drawRoutes(false);
  if (fromMap) $(`delivery-${index}`)?.scrollIntoView({ block: "nearest" });
}
function renumber(route) { route.stops.forEach((stop, index) => { stop.seq = index + 1; }); }
function pruneRoutes() {
  state.draft.routes = state.draft.routes.filter((r) => r.stops.length);
  if (!state.draft.routes.some((r) => r.route_id === state.selected)) state.selected = null;
}
async function recheck() {
  await guarded(async () => {
    state.check = await api(`${shiftPath()}/plans/check`, json("POST", state.draft));
    state.dirty = true;
  }, "Checking the plan");
}
function wirePlan() {
  document.querySelectorAll(".plan-alert [data-route]").forEach((button) => { button.onclick = () => selectRoute(button.dataset.route); });
  document.querySelectorAll("#generate").forEach((button) => { button.onclick = generate; });
  $("approve").onclick = async () => {
    const hard = state.check.violations.filter((v) => v.severity === "hard");
    if (hard.length && !await ask("Approve with alerts?", `<p>This plan goes over these limits. They'll be saved with the plan and shown in the export.</p>${alertList(hard)}`, "Approve anyway")) return;
    guarded(async () => {
      if (state.dirty) await saveDraft();
      setDoc(await api(`${shiftPath()}/plans/latest/approve`, { method: "POST" }));
    }, "Approving", "Plan approved.");
  };
  if (state.dirty) {
    $("save").onclick = () => guarded(saveDraft, "Saving", "Changes saved.");
    $("undo").onclick = () => { setDoc(state.doc); render(); };
  }
  $("export").onclick = exportPlan;
  $("absent-open").onclick = () => { $("more-menu").open = false; openAbsent(); };
}
function wirePanels() {
  document.querySelectorAll("#side-panel [data-route], #route-panel [data-route], #board [data-route]").forEach((button) => { button.onclick = () => selectRoute(button.dataset.route); });
  document.querySelectorAll("[data-all-drivers]").forEach((button) => { button.onclick = () => selectRoute(null); });
  document.querySelectorAll("[data-tab]").forEach((button) => { button.onclick = () => { state.planTab = button.dataset.tab; paintPanels(); }; });
  document.querySelectorAll("[data-stop]").forEach((button) => { button.onclick = () => focusStop(Number(button.dataset.stop)); });
  document.querySelectorAll("[data-add]").forEach((select) => {
    select.onchange = () => {
      if (!select.value) return;
      moveParcel(select.dataset.add, select.value);
    };
  });
  const route = state.draft.routes.find((r) => r.route_id === state.selected);
  if (!route) return;
  $("route-edit").ontoggle = (event) => { state.editOpen = event.target.open; };
  $("route-driver").onchange = (e) => { route.driver_id = e.target.value; recheck(); };
  $("route-vehicle").onchange = (e) => { route.vehicle_id = e.target.value; recheck(); };
  $("route-depart").onchange = (e) => { route.depart_hub = e.target.value; recheck(); };
  document.querySelectorAll("[data-move]").forEach((button) => {
    button.onclick = () => {
      const index = Number(button.dataset.index), target = index + Number(button.dataset.move);
      [route.stops[index], route.stops[target]] = [route.stops[target], route.stops[index]];
      state.focusStop = target;
      renumber(route); recheck();
    };
  });
  document.querySelectorAll("[data-reassign]").forEach((select) => {
    select.onchange = () => {
      if (select.value) moveParcel(select.dataset.reassign, select.value === "__none" ? null : select.value);
    };
  });
}
async function saveDraft() { setDoc(await api(`${shiftPath()}/plans/latest`, json("PUT", state.draft))); }
async function generate() {
  if ((state.dirty || state.doc?.source === "edited") && !await ask("Create a new plan?", "<p>A new plan starts from scratch, so your manual changes won't carry over. Earlier versions are kept.</p>", "Create a new plan")) return;
  guarded(async () => {
    setDoc(await api(`${shiftPath()}/plans`, { method: "POST" }));
    state.selected = null;
    state.focusStop = null;
  }, "Creating the plan, which takes about a minute", "Plan created. Choose a driver to see their route, then approve the plan.");
}
async function exportPlan() {
  await guarded(async () => {
    if (state.dirty) throw new Error("Save your changes before exporting.");
    const data = await api(`${shiftPath()}/plans/latest/export`);
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    link.download = `plan-${data.client_id}-${data.hub_id}-${data.plan_date}.json`;
    link.click();
    URL.revokeObjectURL(link.href);
  });
}
function startMap() {
  if (typeof L === "undefined") { $("map").textContent = "The map couldn't load, so check the internet connection. Everything else still works."; return; }
  if (map) { map.remove(); map = null; }
  map = L.map("map");
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 18, attribution: "&copy; OpenStreetMap contributors, routes by OSRM" }).addTo(map);
  layers = L.layerGroup().addTo(map);
}
function hubMarker(hub, name) {
  const icon = L.divIcon({ className: "", html: `<span class="pin hub">Hub</span>`, iconSize: [38, 22], iconAnchor: [19, 11] });
  return L.marker(hub, { icon, zIndexOffset: 500 }).bindTooltip(`Hub ${esc(name)}`);
}
async function roadLine(points) {
  const key = points.map((p) => p.join(",")).join(";");
  if (roadLines.has(key)) return roadLines.get(key);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  let line = null;
  try {
    const path = points.map(([lat, lng]) => `${lng},${lat}`).join(";");
    const response = await fetch(`${ROAD_SERVICE}${path}?overview=full&geometries=geojson`, { signal: controller.signal });
    const body = await response.json();
    const coordinates = body?.routes?.[0]?.geometry?.coordinates;
    if (coordinates?.length) line = coordinates.map(([lng, lat]) => [lat, lng]);
  } catch { line = null; }
  clearTimeout(timer);
  if (line) roadLines.set(key, line);
  return line;
}
function drawRoutes(fit) {
  if (!map) return;
  const draw = ++drawCount;
  layers.clearLayers();
  const settingsHub = state.doc.settings.hubs.find((h) => h.hub_id === $("hub").value);
  const hub = [settingsHub.lat, settingsHub.lng];
  hubMarker(hub, settingsHub.name).addTo(layers);
  const route = state.check.routes.find((r) => r.route_id === state.selected);
  if (!route) {
    const points = [hub];
    state.check.routes.forEach((each) => {
      const colour = routeColour(each.route_id);
      each.stops.forEach((stop) => {
        points.push([stop.lat, stop.lng]);
        L.circleMarker([stop.lat, stop.lng], { radius: 5, color: "#fff", weight: 1, fillColor: colour, fillOpacity: 0.95 }).addTo(layers)
          .bindTooltip(`${esc(driverName(each.driver_id))}, stop ${esc(stop.seq)}`)
          .on("click", () => selectRoute(each.route_id));
      });
    });
    $("map-key").textContent = "Each colour is one driver. Choose a driver to see their route.";
    if (fit) map.fitBounds(points, { padding: [24, 24], animate: false });
    return;
  }
  const colour = routeColour(route.route_id);
  const stops = route.stops.map((s) => [s.lat, s.lng]);
  const path = [hub, ...stops, hub];
  const lineLayer = L.layerGroup().addTo(layers);
  const paintLine = (points, dashed) => {
    lineLayer.clearLayers();
    L.polyline(points, { color: "#fff", weight: 8, opacity: 0.9 }).addTo(lineLayer);
    L.polyline(points, { color: colour, weight: 4, opacity: 1, dashArray: dashed ? "6 8" : null }).addTo(lineLayer);
  };
  if (fit) map.fitBounds(path, { padding: [40, 40], animate: false });
  paintLine(path, true);
  route.stops.forEach((stop, index) => {
    const isLate = stop.consignment_ids.some((c) => route.late_consignments.includes(c));
    const approximate = stop.consignment_ids.some((c) => assessmentFor(c)?.quality !== "GOOD");
    const focused = state.focusStop === index;
    const kind = isLate ? "late" : approximate ? "approximate" : "";
    const icon = L.divIcon({ className: "", html: `<span class="pin ${kind} ${focused ? "focused" : ""}" style="--route:${colour}">${esc(stop.seq)}</span>`, iconSize: [26, 26], iconAnchor: [13, 13] });
    L.marker([stop.lat, stop.lng], { icon, zIndexOffset: focused ? 1000 : 100 }).addTo(layers)
      .bindTooltip(`${esc(stop.seq)}. ${esc(stop.delivery_start)}, ${esc(typedAddress(stop.consignment_ids[0]))}`)
      .on("click", () => focusStop(index, true));
    if (focused) map.panInside([stop.lat, stop.lng], { padding: [40, 40], animate: false });
  });
  const keyFor = (road) => `${road ? "The line follows the roads, in delivery order." : "Approximate line: road directions aren't available."} Square means late, and a dashed ring means an approximate location. Times come from the plan.`;
  $("map-key").textContent = keyFor(false).replace("Approximate line: road directions aren't available.", "Finding the roads...");
  roadLine(path).then((line) => {
    if (draw !== drawCount || !map) return;
    if (line) paintLine(line, false);
    $("map-key").textContent = keyFor(Boolean(line));
  });
}
function openAbsent() {
  state.proposal = null;
  $("absent-result").innerHTML = "";
  $("absent-accept").hidden = true;
  $("absent-driver").innerHTML = state.draft.routes.map((r) => `<option value="${esc(r.driver_id)}">${esc(driverName(r.driver_id))}</option>`).join("");
  $("absent").showModal();
}
async function propose() {
  try {
    state.proposal = await api(`${shiftPath()}/plans/replan`, json("POST", { driver_id: $("absent-driver").value, time: $("absent-time").value }));
    const p = state.proposal;
    $("absent-result").innerHTML = `<h3>Proposed change</h3><ul>${p.changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>
      <p>Parcels on routes go from ${p.routed.before} to ${p.routed.after}, and the plan's cost changes by ${money(p.cost_change)}. The plan is ${p.check.valid ? "valid" : "not valid"}.</p>`;
    $("absent-accept").hidden = false;
  } catch (error) {
    $("absent-result").textContent = error.message;
  }
}
function renderSetup() {
  $("view").innerHTML = `<section class="sheet"><h2>Client setup</h2>
    <p>A client's hub, vehicles, rules, costs, and slots are all settings. Add or change a client here, with no code changes. Mistakes are all listed together, and nothing is saved until they're fixed.</p>
    <div class="actions"><button id="load-settings">Show this client's settings</button><button id="save-settings">Save changes to this client</button><button id="create-client" class="primary">Add as a new client</button></div>
    <textarea id="settings" aria-label="Client settings as JSON" spellcheck="false" style="margin-top:12px" placeholder="Paste a client's settings here, as JSON"></textarea></section>`;
  const parsed = () => { try { return JSON.parse($("settings").value); } catch { throw new Error("That isn't valid JSON, so check for a missing comma or bracket."); } };
  $("load-settings").onclick = async () => {
    try { $("settings").value = JSON.stringify(await api(`/clients/${encodeURIComponent($("client").value)}`), null, 2); show(""); } catch (error) { show(error.problems || error.message); }
  };
  const keepText = (action, done) => async () => {
    const text = $("settings").value;
    show("");
    try { await action(); await loadClients(); show(done, true); } catch (error) { show(error.problems || error.message); }
    $("settings").value = text;
  };
  $("save-settings").onclick = keepText(() => api(`/clients/${encodeURIComponent($("client").value)}`, json("PUT", parsed())), "Client saved.");
  $("create-client").onclick = keepText(() => api("/clients", json("POST", parsed())), "Client added.");
}

document.querySelectorAll(".stages button").forEach((button) => { button.onclick = () => { state.view = button.dataset.view; render(); }; });
$("client").onchange = () => { fillHubs(); loadShift(); };
$("hub").onchange = loadShift;
$("date").onchange = loadShift;
$("absent-propose").onclick = propose;
$("absent-close").onclick = () => $("absent").close();
$("absent-accept").onclick = () => {
  const plan = state.proposal.plan;
  $("absent").close();
  guarded(async () => { setDoc(await api(`${shiftPath()}/plans/latest`, json("PUT", plan))); }, "Saving", "Change accepted. Review it, then approve the plan.");
};
document.addEventListener("click", (event) => {
  const menu = $("more-menu");
  if (menu?.open && !menu.contains(event.target)) menu.open = false;
});
(async () => {
  $("date").value = "2026-10-14";
  await loadClients();
  await loadShift();
  if (state.addresses) state.view = "plan";
  render();
})();
