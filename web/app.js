// Skeddy mini app
const tg = window.Telegram?.WebApp;
tg?.ready?.();
tg?.expand?.();

const USER_ID = tg?.initDataUnsafe?.user?.id ?? new URLSearchParams(location.search).get("user_id") ?? "";
const API = "/api";

const SCHEDULE_TYPES = [
  { id: "once", icon: "\u{1F4CC}", label: "Specific time", desc: "Fire once at a set datetime" },
  { id: "every_day", icon: "\u{1F4C5}", label: "Every day", desc: "Fire at HH:MM every day" },
  { id: "interval", icon: "\u{1F3B2}", label: "Interval", desc: "Every Ns (or random in min-max)" },
  { id: "weekdays", icon: "\u{1F5D3}\uFE0F", label: "Days of the week", desc: "At HH:MM on selected weekdays" },
  { id: "monthdays", icon: "\u{1F4C6}", label: "Days of the month", desc: "At HH:MM on selected day numbers" },
  { id: "daterange", icon: "\u{1F501}", label: "Date range", desc: "At HH:MM every day in [start, end]" },
];

const WEEKDAY_PILLS = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];

let formState = {
  id: null, text: "", kind: "task", note: "", message_template: "",
  image_id: null, schedules: [],
};

function $(sel) { return document.querySelector(sel); }
function $all(sel) { return document.querySelectorAll(sel); }

function toast(msg, type = "ok") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = `toast show ${type}`;
  setTimeout(() => { el.className = "toast"; }, 2200);
}

async function api(path, opts = {}) {
  const url = `${API}${path}${path.includes("?") ? "&" : "?"}user_id=${encodeURIComponent(USER_ID)}`;
  const res = await fetch(url, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

let tab = "active"; // active | completed

async function loadList() {
  const main = $("#main");
  main.innerHTML = `<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>`;
  try {
    const data = await api("/reminders");
    renderTabs();
    const items = data.items.filter(r => tab === "active" ? !r.completed : r.completed);
    if (!items.length) {
      main.innerHTML = `<div class="empty"><span class="big">\u{1F4CB}</span>No ${tab} reminders yet.<br/>Create one here or via chat.</div>`;
      return;
    }
    main.innerHTML = items.map(renderCard).join("");
    items.forEach(r => {
      $(`#done-${r.id}`)?.addEventListener("click", () => markDone(r.id));
      $(`#del-${r.id}`)?.addEventListener("click", () => del(r.id));
      $(`#edit-${r.id}`)?.addEventListener("click", () => openEdit(r));
      $all(`.snooze-${r.id}`).forEach(b => {
        b.addEventListener("click", () => snooze(r.id, parseInt(b.dataset.secs, 10)));
      });
    });
  } catch (e) {
    main.innerHTML = `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  }
}

function renderTabs() {
  const existing = $("#tabs");
  if (existing) existing.remove();
  const div = document.createElement("div");
  div.className = "tabs";
  div.id = "tabs";
  div.innerHTML = `
    <button class="${tab === "active" ? "active" : ""}" data-tab="active">Active</button>
    <button class="${tab === "completed" ? "active" : ""}" data-tab="completed">Completed</button>
  `;
  $("#main").prepend(div);
  div.querySelectorAll("button").forEach(b => {
    b.addEventListener("click", () => { tab = b.dataset.tab; loadList(); });
  });
}

function renderCard(r) {
  const kindLabel = { reminder: "Reminder", task: "Task", task_no_notify: "Silent" }[r.kind] ?? r.kind;
  const sched = SCHEDULE_TYPES.find(s => s.id === r.schedule_type) ?? {};
  const schedIcon = sched.icon ?? "\u{2753}";
  const schedLabel = sched.label ?? r.schedule_type;
  const snoozeBtns = (r.kind === "task" && !r.completed)
    ? [60, 300, 900, 3600].map(s => `<button class="snooze snooze-${r.id}" data-secs="${s}">+${s < 3600 ? s/60 + "m" : s/3600 + "h"}</button>`).join("")
    : "";
  const imgTag = r.image_id ? " \u{1F5BC}" : "";
  return `
    <div class="card" data-id="${r.id}">
      <div class="top">
        <div class="chip">${schedIcon}</div>
        <div class="body">
          <div class="title">${escapeHtml(r.text)}${imgTag}</div>
          <div class="meta">${schedLabel} &middot; next: ${r.next_fire_local ?? "?"}</div>
          ${r.note ? `<div class="note">${escapeHtml(r.note)}</div>` : ""}
        </div>
        <div class="right">
          <span class="badge ${r.completed ? "muted" : ""}">${kindLabel}</span>
        </div>
      </div>
      <div class="actions">
        ${r.completed ? "" : `<button class="done" id="done-${r.id}">\u2705 Done</button>`}
        ${snoozeBtns}
        <span class="spacer"></span>
        <button id="edit-${r.id}">Edit</button>
        <button class="danger" id="del-${r.id}">Delete</button>
      </div>
    </div>
  `;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}

async function markDone(rid) {
  try { await api(`/reminders/${rid}/done`, { method: "POST" }); toast("Completed"); loadList(); }
  catch (e) { toast(e.message, "err"); }
}
async function del(rid) {
  if (!confirm("Delete this reminder?")) return;
  try { await api(`/reminders/${rid}`, { method: "DELETE" }); toast("Deleted"); loadList(); }
  catch (e) { toast(e.message, "err"); }
}
async function snooze(rid, secs) {
  try { await api(`/reminders/${rid}/snooze?seconds=${secs}`, { method: "POST" }); toast(`Snoozed +${secs}s`); loadList(); }
  catch (e) { toast(e.message, "err"); }
}

// ---------------------------------------------------------------------------
// Image upload (uploaded to /api/images, stored via the Vault)
// ---------------------------------------------------------------------------

let imgObjectUrl = null;

function resetImageUI(imageId) {
  formState.image_id = imageId ?? null;
  $("#img-error").textContent = "";
  $("#img-preview").style.display = "none";
  if (imgObjectUrl) { URL.revokeObjectURL(imgObjectUrl); imgObjectUrl = null; }
}

function showImagePreview(src) {
  const box = $("#img-preview");
  $("#img-preview-img").src = src;
  box.style.display = "block";
}

async function onImagePicked(file) {
  $("#img-error").textContent = "";
  if (!file) return;
  if (file.size > 5 * 1024 * 1024) {
    $("#img-error").textContent = "Image is larger than 5 MB";
    return;
  }
  imgObjectUrl = URL.createObjectURL(file);
  showImagePreview(imgObjectUrl);
  try {
    const fd = new FormData();
    fd.append("file", file);
    const res = await api("/images", { method: "POST", body: fd });
    formState.image_id = res.image_id;
    toast("Image uploaded");
  } catch (e) {
    $("#img-error").textContent = e.message;
    formState.image_id = null;
  }
}

function onImageRemoved() {
  if (formState.image_id) {
    api(`/images/${formState.image_id}`, { method: "DELETE" }).catch(() => {});
  }
  resetImageUI(null);
}

// ---------------------------------------------------------------------------
// Create / Edit form
// ---------------------------------------------------------------------------

function openCreate() {
  formState = { id: null, text: "", kind: "task", note: "", message_template: "", image_id: null, schedules: [] };
  formState.raw = [];
  $("#form-title").textContent = "New reminder";
  $("#f-text").value = "";
  $("#f-note").value = "";
  $("#f-template").value = "";
  $all('input[name="kind"]').forEach(r => { r.checked = r.value === "task"; });
  resetImageUI(null);
  renderScheduleList();
  $("#form-backdrop").classList.add("show");
}

function openEdit(r) {
  formState = {
    id: r.id, text: r.text, kind: r.kind, note: r.note || "",
    message_template: r.message_template || "", image_id: r.image_id || null,
    schedules: [r].map(x => x),
  };
  formState.raw = [r];
  $("#form-title").textContent = `Edit #${r.id}`;
  $("#f-text").value = r.text;
  $("#f-note").value = r.note || "";
  $("#f-template").value = r.message_template || "";
  $all('input[name="kind"]').forEach(rad => { rad.checked = rad.value === r.kind; });
  resetImageUI(r.image_id || null);
  if (r.image_id) showImagePreview(`${API}/images/${r.image_id}?user_id=${encodeURIComponent(USER_ID)}`);
  renderScheduleList();
  $("#form-backdrop").classList.add("show");
}

function renderScheduleList() {
  const list = $("#schedule-list");
  if (!formState.raw || !formState.raw.length) { list.innerHTML = ""; return; }
  list.innerHTML = formState.raw.map((r, i) => {
    const s = SCHEDULE_TYPES.find(x => x.id === r.schedule_type) ?? {};
    return `
      <div class="schedule-row">
        <div class="label">${s.icon ?? ""} ${escapeHtml(scheduleDetail(r))}
          <div class="sub">${s.label ?? r.schedule_type}</div>
        </div>
        <div>
          <button class="icon-btn" data-act="edit-sched" data-idx="${i}">\u{270E}</button>
          <button class="icon-btn" data-act="del-sched" data-idx="${i}">\u{1F5D1}</button>
        </div>
      </div>
    `;
  }).join("");
  list.querySelectorAll("[data-act]").forEach(b => {
    b.addEventListener("click", () => {
      const idx = parseInt(b.dataset.idx, 10);
      if (b.dataset.act === "del-sched") { formState.raw.splice(idx, 1); renderScheduleList(); }
      if (b.dataset.act === "edit-sched") { editSchedule(idx); }
    });
  });
}

function scheduleDetail(r) {
  switch (r.schedule_type) {
    case "once": return r.at ?? "";
    case "every_day": return r.time_of_day ?? "09:00";
    case "interval": {
      const lo = r.min_seconds ?? 0, hi = r.max_seconds ?? lo;
      if (lo === hi) return `every ${fmtDur(lo)}`;
      return `${fmtDur(lo)} \u2013 ${fmtDur(hi)} (random)`;
    }
    case "weekdays": return `${(r.weekdays || []).map(i => WEEKDAY_PILLS[i]).join(", ")} at ${r.time_of_day ?? "09:00"}`;
    case "monthdays": return `days ${(r.monthdays || []).join(", ")} at ${r.time_of_day ?? "09:00"}`;
    case "daterange": return `${r.range_start} \u2192 ${r.range_end} at ${r.time_of_day ?? "09:00"}`;
    default: return "";
  }
}

function fmtDur(s) {
  if (s < 60) return s + "s";
  if (s < 3600) return Math.round(s/60) + "m";
  if (s < 86400) return Math.round(s/3600) + "h";
  return Math.round(s/86400) + "d";
}

let editingSchedIdx = null;

function addSchedule() {
  editingSchedIdx = null;
  const opts = $("#sched-options");
  opts.innerHTML = SCHEDULE_TYPES.map(s => `
    <div class="option" data-id="${s.id}">
      <div><div class="t">${s.icon} ${s.label}</div><div class="s">${s.desc}</div></div>
      <div class="chev">\u203A</div>
    </div>
  `).join("");
  opts.querySelectorAll(".option").forEach(o => {
    o.addEventListener("click", () => {
      const id = o.dataset.id;
      $("#sched-picker-backdrop").classList.remove("show");
      openScheduleEditor(id);
    });
  });
  $("#sched-picker-backdrop").classList.add("show");
}

function editSchedule(idx) {
  editingSchedIdx = idx;
  const r = formState.raw[idx];
  openScheduleEditor(r.schedule_type, r);
}

function openScheduleEditor(type, existing) {
  const list = $("#schedule-list");
  const wrap = document.createElement("div");
  wrap.style.margin = "10px 0";
  wrap.style.background = "var(--tg-panel)";
  wrap.style.borderRadius = "10px";
  wrap.style.padding = "12px";
  const s = SCHEDULE_TYPES.find(x => x.id === type);
  wrap.innerHTML = `
    <div style="font-weight:600; margin-bottom:8px;">${s.icon} ${s.label}</div>
    ${scheduleFieldsHtml(type, existing)}
    <div style="display:flex; gap:6px; margin-top:10px;">
      <button id="sched-ok" style="flex:1; background:var(--accent); color:var(--tg-button-text, #06281f); border:0; padding:10px; border-radius:8px; font-weight:600; cursor:pointer;">OK</button>
      <button id="sched-cancel" style="background:var(--tg-panel); color:var(--tg-text); border:0; padding:10px; border-radius:8px; cursor:pointer;">Cancel</button>
    </div>
  `;
  list.appendChild(wrap);
  wrap.querySelectorAll("[data-weekday]").forEach(p => {
    p.addEventListener("click", () => p.classList.toggle("active"));
  });
  wrap.querySelectorAll("[data-monthday]").forEach(p => {
    p.addEventListener("click", () => p.classList.toggle("active"));
  });
  $("#sched-ok").addEventListener("click", () => {
    const d = scheduleFieldsRead(wrap, type);
    if (editingSchedIdx !== null) {
      formState.raw[editingSchedIdx] = { ...formState.raw[editingSchedIdx], schedule_type: type, ...d };
    } else {
      formState.raw = formState.raw || [];
      formState.raw.push({ schedule_type: type, ...d, text: "", kind: "task" });
    }
    wrap.remove();
    renderScheduleList();
  });
  $("#sched-cancel").addEventListener("click", () => wrap.remove());
}

function scheduleFieldsHtml(type, r) {
  const tod = r?.time_of_day ?? "09:00";
  switch (type) {
    case "once":
      return `<div class="field"><label>At</label><input id="sf-at" type="datetime-local" value="${r?.at ?? ""}"/></div>`;
    case "every_day":
      return `<div class="field"><label>Time</label><input id="sf-tod" type="time" value="${tod}"/></div>`;
    case "interval": {
      const lo = r?.min_seconds ?? 600, hi = r?.max_seconds ?? 1200;
      return `
        <div class="field"><label>Min (seconds)</label><input id="sf-lo" type="number" value="${lo}" min="1"/></div>
        <div class="field"><label>Max (seconds) \u2014 set equal to min for fixed interval</label><input id="sf-hi" type="number" value="${hi}" min="1"/></div>
      `;
    }
    case "weekdays":
      return `
        <div class="field"><label>Time</label><input id="sf-tod" type="time" value="${tod}"/></div>
        <div class="field"><label>Weekdays</label>
          <div class="pill-row">
            ${WEEKDAY_PILLS.map((w, i) => `<div class="pill ${r?.weekdays?.includes(i) ? "active" : ""}" data-weekday="${i}">${w}</div>`).join("")}
          </div>
        </div>
      `;
    case "monthdays":
      return `
        <div class="field"><label>Time</label><input id="sf-tod" type="time" value="${tod}"/></div>
        <div class="field"><label>Days of month</label>
          <div class="pill-row">
            ${Array.from({length: 31}, (_, i) => i + 1).map(d => `<div class="pill ${r?.monthdays?.includes(d) ? "active" : ""}" data-monthday="${d}">${d}</div>`).join("")}
          </div>
        </div>
      `;
    case "daterange":
      return `
        <div class="field"><label>Time</label><input id="sf-tod" type="time" value="${tod}"/></div>
        <div class="field"><label>Start date</label><input id="sf-start" type="date" value="${r?.range_start ?? ""}"/></div>
        <div class="field"><label>End date</label><input id="sf-end" type="date" value="${r?.range_end ?? ""}"/></div>
      `;
    default: return "";
  }
}

function scheduleFieldsRead(wrap, type) {
  const val = (sel) => wrap.querySelector(sel)?.value;
  const tod = val("#sf-tod") || "09:00";
  switch (type) {
    case "once": {
      const v = val("#sf-at");
      return { at: v ? new Date(v).toISOString() : null, time_of_day: null };
    }
    case "every_day": return { time_of_day: tod };
    case "interval": {
      const lo = parseInt(val("#sf-lo") || "60", 10);
      const hi = parseInt(val("#sf-hi") || String(lo), 10);
      return { min_seconds: lo, max_seconds: hi };
    }
    case "weekdays": {
      const days = [...wrap.querySelectorAll("[data-weekday].active")].map(p => parseInt(p.dataset.weekday, 10));
      return { time_of_day: tod, weekdays: days };
    }
    case "monthdays": {
      const days = [...wrap.querySelectorAll("[data-monthday].active")].map(p => parseInt(p.dataset.monthday, 10));
      return { time_of_day: tod, monthdays: days };
    }
    case "daterange": return { time_of_day: tod, range_start: val("#sf-start"), range_end: val("#sf-end") };
    default: return {};
  }
}

async function saveForm() {
  const text = $("#f-text").value.trim();
  if (!text) { toast("Enter a reminder text", "err"); return; }
  if (!formState.raw || !formState.raw.length) { toast("Add at least one schedule", "err"); return; }
  const kind = ($('input[name="kind"]:checked') ?? {}).value ?? "task";
  const me = await api("/me");
  const tz = me.timezone || me.default_tz;
  const body = (sched) => ({
    text, kind, note: $("#f-note").value.trim() || null,
    message_template: $("#f-template").value.trim() || null,
    image_id: formState.image_id ?? null,
    schedule_type: sched.schedule_type, tz,
    at: sched.at ?? null, time_of_day: sched.time_of_day ?? null,
    min_seconds: sched.min_seconds ?? null, max_seconds: sched.max_seconds ?? null,
    weekdays: sched.weekdays ?? [], monthdays: sched.monthdays ?? [],
    range_start: sched.range_start ?? null, range_end: sched.range_end ?? null,
  });
  try {
    if (formState.id === null) {
      for (const sched of formState.raw) {
        await api("/reminders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body(sched)) });
      }
      toast(`Created ${formState.raw.length} reminder(s)`);
    } else {
      await api(`/reminders/${formState.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body(formState.raw[0])) });
      toast("Updated");
    }
    $("#form-backdrop").classList.remove("show");
    loadList();
  } catch (e) { toast(e.message, "err"); }
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

const TIMEZONES = [
  "Europe/Amsterdam", "Europe/Berlin", "Europe/Dublin", "Europe/Kyiv", "Europe/Lisbon",
  "Europe/London", "Europe/Madrid", "Europe/Paris", "Europe/Rome", "Europe/Warsaw",
  "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
  "America/Anchorage", "America/Honolulu", "America/Toronto", "America/Vancouver",
  "America/Sao_Paulo", "America/Buenos_Aires", "America/Mexico_City",
  "Asia/Dubai", "Asia/Jerusalem", "Asia/Kolkata", "Asia/Bangkok", "Asia/Jakarta",
  "Asia/Singapore", "Asia/Hong_Kong", "Asia/Shanghai", "Asia/Seoul", "Asia/Tokyo",
  "Australia/Perth", "Australia/Sydney", "Australia/Melbourne", "Australia/Brisbane",
  "Pacific/Auckland", "UTC"
];

function populateTimezones(selectedTz) {
  const sel = document.getElementById("s-tz");
  if (!sel) return;
  sel.innerHTML = "";
  const tzs = [...TIMEZONES];
  if (selectedTz && !tzs.includes(selectedTz)) {
    tzs.unshift(selectedTz);
  }
  for (const tz of tzs) {
    const opt = document.createElement("option");
    opt.value = tz;
    opt.textContent = tz.replace(/_/g, " ");
    if (tz === selectedTz) opt.selected = true;
    sel.appendChild(opt);
  }
}

async function openSettings() {
  try {
    const s = await api("/settings");
    const currentTz = s.timezone || s.default_tz || "UTC";
    populateTimezones(currentTz);
    document.getElementById("s-snooze").checked = !!s.hide_snooze;
    document.getElementById("settings-backdrop").classList.add("show");
  } catch (e) { toast(e.message, "err"); }
}

async function saveSettings() {
  const tz = $("#s-tz").value.trim() || null;
  const hide_snooze = $("#s-snooze").checked;
  try {
    await api("/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ timezone: tz, hide_snooze }) });
    toast("Settings saved");
    $("#settings-backdrop").classList.remove("show");
    loadList();
  } catch (e) { toast(e.message, "err"); }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

$("#fab-create").addEventListener("click", openCreate);
$("#btn-cancel-form").addEventListener("click", () => $("#form-backdrop").classList.remove("show"));
$("#btn-save").addEventListener("click", saveForm);
$("#btn-settings").addEventListener("click", openSettings);
$("#btn-close-settings").addEventListener("click", () => $("#settings-backdrop").classList.remove("show"));
$("#btn-save-settings").addEventListener("click", saveSettings);
$("#btn-close").addEventListener("click", () => tg?.close?.());
$("#add-schedule-row").addEventListener("click", addSchedule);
$("#btn-close-sched").addEventListener("click", () => $("#sched-picker-backdrop").classList.remove("show"));
$("#img-tile").addEventListener("click", () => $("#f-image-file").click());
$("#f-image-file").addEventListener("change", (e) => {
  onImagePicked(e.target.files[0]);
  e.target.value = "";
});
$("#img-remove").addEventListener("click", onImageRemoved);

// deep link: #settings opens settings
if (location.hash === "#settings") openSettings();

loadList();
