// Flexible Reminder mini app
const tg = window.Telegram?.WebApp;
tg?.ready?.();
tg?.expand?.();

const USER_ID = tg?.initDataUnsafe?.user?.id ?? new URLSearchParams(location.search).get("user_id") ?? "";
const API = "/api";

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

const WEEKDAY_PILLS = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
const UNIT_SECONDS = { "Seconds": 1, "Minutes": 60, "Hours": 3600, "Days": 86400, "Weeks": 604800 };

// Schedule categories shown as the top-level choices
const SCHEDULE_CATEGORIES = [
  { id: "once", label: "One time", desc: "Fire once at a set date and time" },
  { id: "interval", label: "Interval", desc: "Repeat every N seconds/minutes/hours/days" },
  { id: "complex", label: "Complex", desc: "Advanced recurring schedules" },
];
// Which (sub)schedule_type belongs to which category
function categoryFor(type) {
  if (type === "once") return "once";
  if (type === "interval") return "interval";
  return "complex";
}
const COMPLEX_TYPES = [
  { id: "every_day", label: "Every day", desc: "At HH:MM every day" },
  { id: "every_ndays", label: "Every N days", desc: "Every N days at a time or random window" },
  { id: "weekdays", label: "Days of week", desc: "At HH:MM on selected weekdays" },
  { id: "monthdays", label: "Days of month", desc: "At HH:MM on selected day numbers" },
  { id: "daterange", label: "Date range", desc: "At HH:MM every day in [start, end]" },
  { id: "cron", label: "Cron", desc: "5-field crontab expression" },
];

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

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}

function populateTimezones(select, selected) {
  if (!select) return;
  const tzs = [...TIMEZONES];
  const cur = selected || "UTC";
  if (cur && !tzs.includes(cur)) tzs.unshift(cur);
  select.innerHTML = "";
  for (const tz of tzs) {
    const opt = document.createElement("option");
    opt.value = tz;
    opt.textContent = tz.replace(/_/g, " ") + " (" + tz.split("/").pop() + ")";
    if (tz === cur) opt.selected = true;
    select.appendChild(opt);
  }
}

function fmtDur(s) {
  s = Number(s) || 0;
  if (s < 60) return s + "s";
  if (s < 3600) return Math.round(s/60) + "m";
  if (s < 86400) return Math.round(s/3600) + "h";
  return Math.round(s/86400) + "d";
}

function fmtDT(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

// ---------------------------------------------------------------------------
// View switching
// ---------------------------------------------------------------------------
function showView(id) {
  $all(".view").forEach(v => v.classList.remove("show"));
  $(id).classList.add("show");
}

// ---------------------------------------------------------------------------
// List
// ---------------------------------------------------------------------------
let tab = "active";
let SNOOZE_DURATIONS = [300, 600, 1800];

async function loadList() {
  const main = $("#main");
  main.innerHTML = `<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>`;
  try {
    const data = await api("/reminders");
    // keep snooze durations in sync with user settings
    try { const s = await api("/settings"); if (s.snooze_durations && s.snooze_durations.length) SNOOZE_DURATIONS = s.snooze_durations; } catch {}
    renderTabs();
    const items = data.items.filter(r => tab === "active" ? !r.completed : r.completed);
    if (!items.length) {
      main.innerHTML = `<div class="empty"><span class="big">\u{1F4CB}</span>No ${tab} reminders yet.<br/>Tap + Create to add one.</div>`;
      return;
    }
    main.innerHTML = items.map(renderCard).join("");
    items.forEach(r => {
      $(`#del-${r.id}`)?.addEventListener("click", () => del(r.id));
      $(`#edit-${r.id}`)?.addEventListener("click", () => openEdit(r));
      $(`#snooze-${r.id}`)?.addEventListener("click", () => toggleSnooze(r.id));
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

function kindIcon(r) {
  return r.kind === "countdown" ? "\u{23F3}" : "\u{1F514}";
}

function renderCard(r) {
  const kindLabel = r.kind === "countdown" ? "Countdown" : "Reminder";
  const scheds = (r.schedules && r.schedules.length) ? r.schedules : [r];
  const schedPills = scheds.map(s => `<span class="sched-pill">${escapeHtml(scheduleDetailLabel(s))}</span>`).join("");
  const tags = (r.tags && r.tags.length)
    ? `<div class="tag-holder">${r.tags.map(t => `<span class="tag-chip">#${escapeHtml(t)}</span>`).join("")}</div>`
    : "";


  const imgTag = r.image_id ? " \u{1F5BC}" : "";
  return `
    <div class="card" data-id="${r.id}">
      <div class="top">
        <div class="chip">${kindIcon(r)}</div>
        <div class="body">
          <div class="title">${escapeHtml(r.text)}${imgTag}</div>
          <div class="meta">${schedPills}</div>
          <div class="meta">next: ${r.next_fire_local ?? "?"}</div>
          ${r.note ? `<div class="note">${escapeHtml(r.note)}</div>` : ""}
          ${tags}
        </div>
        <div class="right">
          <span class="badge ${r.completed ? "muted" : ""}">${kindLabel}</span>
        </div>
      </div>
      <div class="actions">
        <button class="danger" id="del-${r.id}">Delete</button>
        <button id="edit-${r.id}">Edit</button>
        <span class="spacer"></span>
        <button class="snooze-toggle" id="snooze-${r.id}">Snooze</button>
        <div class="snooze-opts" data-snooze="${r.id}"></div>
      </div>
    </div>
  `;
}

async function markDone(rid) {
  try { await api(`/reminders/${rid}/done`, { method: "POST" }); toast("Done"); loadList(); }
  catch (e) { toast(e.message, "err"); }
}
async function del(rid) {
  if (!confirm("Delete this reminder?")) return;
  try { await api(`/reminders/${rid}`, { method: "DELETE" }); toast("Deleted"); loadList(); }
  catch (e) { toast(e.message, "err"); }
}

function toggleSnooze(rid) {
  const card = document.querySelector(`[data-id="${rid}"]`);
  if (!card) return;
  const opts = card.querySelector(`[data-snooze="${rid}"]`);
  if (card.classList.contains("snooze-open")) {
    card.classList.remove("snooze-open");
    return;
  }
  opts.innerHTML = SNOOZE_DURATIONS.map(s => `<button class="snooze-opt snooze-${rid}" data-secs="${s}">${fmtDur(s)}</button>`).join("");
  opts.querySelectorAll(".snooze-opt").forEach(b => b.addEventListener("click", () => snooze(rid, parseInt(b.dataset.secs, 10))));
  card.classList.add("snooze-open");
}
async function snooze(rid, secs) {
  try { await api(`/reminders/${rid}/snooze?seconds=${secs}`, { method: "POST" }); toast(`Snoozed +${secs}s`); loadList(); }
  catch (e) { toast(e.message, "err"); }
}

// ---------------------------------------------------------------------------
// Form state
// ---------------------------------------------------------------------------
let formState = {
  mode: "regular", id: null, text: "", note: "", message_template: "",
  image_id: null, tz: "", tags: [], schedules: [],
  target_date: "", notification_time: "09:00", countdown_days_before: 0,
};

function textInput() { return formState.mode === "countdown" ? $("#c-text") : $("#f-text"); }

function switchMode(mode) {
  // Sync text across the two textareas
  const from = formState.mode === "countdown" ? $("#c-text") : $("#f-text");
  const to = mode === "countdown" ? $("#c-text") : $("#f-text");
  if (from && to) to.value = from.value;
  formState.mode = mode;
  $all("#mode-row button").forEach(b => b.classList.toggle("active", b.dataset.mode === mode));
  $("#regular-fields").style.display = mode === "regular" ? "block" : "none";
  $("#countdown-fields").style.display = mode === "countdown" ? "block" : "none";
}

async function openCreate() {
  // fetch default timezone
  let tz = "UTC";
  try { const me = await api("/me"); tz = me.timezone || me.default_tz || "UTC"; } catch {}
  formState = {
    mode: "regular", id: null, text: "", note: "", message_template: "",
    image_id: null, tz, tags: [], schedules: [],
    target_date: "", notification_time: "09:00", countdown_days_before: 0,
  };
  $("#form-head-title").textContent = "Create Reminder";
  resetFormUI();
  showView("#form-view");
}

function openEdit(r) {
  formState = {
    mode: r.kind === "countdown" ? "countdown" : "regular",
    id: r.id, text: r.text, note: r.note || "", message_template: r.message_template || "",
    image_id: r.image_id || null, tz: r.tz || "UTC", tags: r.tags || [],
    schedules: (r.schedules && r.schedules.length) ? r.schedules.map(s => pickSched(s)) : [pickSched(r)],
    target_date: r.target_date || "", notification_time: r.notification_time || "09:00",
    countdown_days_before: r.countdown_days_before || 0,
  };
  $("#form-head-title").textContent = `Edit #${r.id}`;
  resetFormUI();
  showView("#form-view");
}

function pickSched(s) {
  return {
    schedule_type: s.schedule_type, at: s.at, at_min: s.at_min, at_max: s.at_max, time_of_day: s.time_of_day,
    min_seconds: s.min_seconds, max_seconds: s.max_seconds,
    weekdays: s.weekdays || [], monthdays: s.monthdays || [],
    range_start: s.range_start, range_end: s.range_end,
    period_days: s.period_days, random_time: !!s.random_time,
    window_start: s.window_start, window_end: s.window_end, cron_expr: s.cron_expr,
  };
}

function resetFormUI() {
  $("#f-text").value = formState.text;
  $("#c-text").value = formState.text;
  $("#f-note").value = formState.note;
  $("#f-template").value = formState.message_template;
  $("#c-date").value = formState.target_date;
  $("#c-time").value = formState.notification_time;
  $("#c-days").value = formState.countdown_days_before || "";
  populateTimezones($("#f-tz"), formState.tz);
  populateTimezones($("#c-tz"), formState.tz);
  switchMode(formState.mode);
  renderTags();
  renderScheduleList();
  resetImageUI(formState.image_id);
  if (formState.image_id) showImagePreview(`${API}/images/${formState.image_id}?user_id=${encodeURIComponent(USER_ID)}`);
}

// ---------------------------------------------------------------------------
// Tags
// ---------------------------------------------------------------------------
function renderTags() {
  const renders = (wrapId, inputId) => {
    const wrap = $(wrapId);
    if (!wrap) return;
    const chips = formState.tags.map((t, i) => `<span class="chip">#${escapeHtml(t)}<span class="x" data-i="${i}">\u00d7</span></span>`).join("");
    wrap.innerHTML = `${chips}<button class="chip-add" data-add>+ Add tag</button><input id="${inputId}" placeholder="Type a tag and press Enter" style="display:none; flex:1; min-width:120px;"/>`;
  };
  renders("#tag-wrap", "tag-input");
  renders("#c-tag-wrap", "c-tag-input");
  $all("[data-add]").forEach(b => b.addEventListener("click", () => {
    const input = b.parentElement.querySelector("input");
    input.style.display = "block";
    input.focus();
  }));
  $all("#tag-wrap input, #c-tag-wrap input").forEach(inp => {
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        const v = inp.value.trim();
        inp.value = "";
        if (v && !formState.tags.includes(v)) formState.tags.push(v);
        renderTags();
      }
    });
  });
  $all(".chip .x").forEach(x => x.addEventListener("click", () => {
    formState.tags.splice(parseInt(x.dataset.i, 10), 1);
    renderTags();
  }));
}

// ---------------------------------------------------------------------------
// Schedules (regular mode)
// ---------------------------------------------------------------------------
function renderScheduleList() {
  const list = $("#schedule-list");
  if (!formState.schedules.length) { list.innerHTML = ""; return; }
  list.innerHTML = formState.schedules.map((s, i) => `
    <div class="sched-item">
      <div class="label">${escapeHtml(scheduleDetailLabel(s))}
        <div class="sub">${categoryFor(s.schedule_type) === "complex" ? "Complex" : (categoryFor(s.schedule_type) === "interval" ? "Interval" : "One time")}</div>
      </div>
      <div class="actions">
        <button class="icon-btn" data-act="edit-sched" data-idx="${i}">\u270E</button>
        <button class="icon-btn" data-act="del-sched" data-idx="${i}">\u{1F5D1}</button>
      </div>
    </div>
  `).join("");
  list.querySelectorAll("[data-act]").forEach(b => {
    b.addEventListener("click", () => {
      const idx = parseInt(b.dataset.idx, 10);
      if (b.dataset.act === "del-sched") { formState.schedules.splice(idx, 1); renderScheduleList(); }
      if (b.dataset.act === "edit-sched") openScheduleEditor(formState.schedules[idx]);
    });
  });
}

function scheduleDetailLabel(s) {
  const type = s.schedule_type;
  if (type === "once") {
    if (s.at_min && s.at_max) {
      return "One time (random): " + fmtDT(s.at_min) + " – " + fmtDT(s.at_max);
    }
    if (!s.at) return "One time";
    return "One time: " + new Date(s.at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  }
  if (type === "interval") {
    const lo = s.min_seconds ?? 0, hi = s.max_seconds ?? lo;
    if (lo === hi) return "Every " + fmtDur(lo);
    return "Every " + fmtDur(lo) + " – " + fmtDur(hi) + " (random)";
  }
  if (type === "every_day") return "Every day at " + (s.time_of_day || "09:00");
  if (type === "every_ndays") {
    const label = "Every " + (s.period_days || 1) + "d";
    if (s.random_time) return (s.window_start ? label + " at random " + s.window_start + "-" + s.window_end : label + " at random");
    return label + " at " + (s.time_of_day || "09:00");
  }
  if (type === "cron") return "Cron " + (s.cron_expr || "");
  if (type === "weekdays") return (s.weekdays || []).map(i => WEEKDAY_PILLS[i]).join(", ") + " at " + (s.time_of_day || "09:00");
  if (type === "monthdays") return "Days " + (s.monthdays || []).join(", ") + " at " + (s.time_of_day || "09:00");
  if (type === "daterange") return `${s.range_start} \u2192 ${s.range_end} at ${s.time_of_day || "09:00"}`;
  return type;
}

function openSchedulePicker() {
  const opts = $("#sched-options");
  opts.innerHTML = SCHEDULE_CATEGORIES.map(c => `
    <div class="option" data-id="${c.id}">
      <div><div class="t">${c.label}</div><div class="s">${c.desc}</div></div>
      <div class="chev">\u203A</div>
    </div>
  `).join("");
  opts.querySelectorAll(".option").forEach(o => {
    o.addEventListener("click", () => {
      $("#sched-picker-backdrop").classList.remove("show");
      openScheduleEditor(null, o.dataset.id);
    });
  });
  $("#sched-picker-backdrop").classList.add("show");
}

let editingSchedIdx = null;
let editorType = "once";

function openScheduleEditor(existing, category) {
  editingSchedIdx = (existing && formState.schedules.indexOf(existing) !== -1) ? formState.schedules.indexOf(existing) : null;
  editorType = existing ? categoryFor(existing.schedule_type) : (category || "once");
  $("#sched-editor-title").textContent = editingSchedIdx !== null ? "Edit Schedule" : "Schedule";
  const seg = $("#sched-type-row");
  seg.querySelectorAll("button").forEach(b => b.classList.toggle("active", b.dataset.type === editorType));
  renderSchedEditor(existing);
  $("#sched-editor-backdrop").classList.add("show");
}

function renderSchedEditor(existing) {
  const fields = $("#sched-editor-fields");
  if (editorType === "once") {
    const at = existing?.at ? localDT(existing.at) : "";
    const rnd = !!(existing?.at_min && existing?.at_max);
    const atMin = existing?.at_min ? localDT(existing.at_min) : "";
    const atMax = existing?.at_max ? localDT(existing.at_max) : "";
    fields.innerHTML = `
      <div class="field"><label>Time mode</label><div class="seg" id="se-random">
        <button class="${rnd ? "" : "active"}" data-r="0">Fixed</button>
        <button class="${rnd ? "active" : ""}" data-r="1">Random range</button>
      </div></div>
      <div class="field" id="se-fixed-row" style="display:${rnd ? "none" : ""}"><label>Date &amp; time</label><input id="se-at" type="datetime-local" value="${at}"/></div>
      <div class="row2" id="se-window-row" style="display:${rnd ? "" : "none"};">
        <div class="field"><label>From</label><input id="se-at-min" type="datetime-local" value="${atMin}"/></div>
        <div class="field"><label>To</label><input id="se-at-max" type="datetime-local" value="${atMax}"/></div>
      </div>`;
    bindTimeMode();
  } else if (editorType === "interval") {
    const isRnd = !!(existing && existing.min_seconds != null && existing.max_seconds != null && existing.min_seconds !== existing.max_seconds);
    const lo = existing?.min_seconds != null ? existing.min_seconds : 3600;
    const hi = existing?.max_seconds != null ? existing.max_seconds : lo;
    const { n: loN, unit } = secsToNUnit(lo);
    const { n: hiN } = secsToNUnit(hi);
    fields.innerHTML = `
      <div class="field"><label>Repeat mode</label><div class="seg" id="se-random">
        <button class="${isRnd ? "" : "active"}" data-r="0">Fixed</button>
        <button class="${isRnd ? "active" : ""}" data-r="1">Random range</button>
      </div></div>
      <div class="field" id="se-fixed-row" style="display:${isRnd ? "none" : ""}"><label>Every</label><input id="se-n" type="number" min="1" value="${loN}"/></div>
      <div class="row2" id="se-window-row" style="display:${isRnd ? "" : "none"};">
        <div class="field"><label>Between</label><input id="se-lo" type="number" min="1" value="${loN}"/></div>
        <div class="field"><label>And</label><input id="se-hi" type="number" min="1" value="${hiN}"/></div>
      </div>
      <div class="field"><label>Unit</label><select id="se-unit">${Object.keys(UNIT_SECONDS).map(u => `<option ${u===unit?"selected":""}>${u}</option>`).join("")}</select></div>`;
    bindTimeMode();
  } else {
    const st = existing ? existing.schedule_type : "every_day";
    const tod = existing?.time_of_day || "09:00";
    fields.innerHTML = `
      <div class="field"><label>Repeat</label><select id="se-complex-type">${COMPLEX_TYPES.map(c => `<option value="${c.id}" ${c.id===st?"selected":""}>${c.label}</option>`).join("")}</select></div>
      ${complexSubfields(st, existing)}
    `;
    $("#se-complex-type").addEventListener("change", (e) => {
      renderComplexSubfields(e.target.value, existing);
    });
    bindComplexPills();
    bindTimeMode();
  }
}

function timeModeHtml(existing) {
  const tod = existing?.time_of_day || "09:00";
  const rnd = !!existing?.random_time;
  const ws = existing?.window_start || "00:00";
  const we = existing?.window_end || "23:59";
  return `<div class="field"><label>Time mode</label><div class="seg" id="se-random">
      <button class="${rnd ? "" : "active"}" data-r="0">Fixed</button>
      <button class="${rnd ? "active" : ""}" data-r="1">Random</button>
    </div></div>
    <div class="field" id="se-fixed-row" style="display:${rnd ? "none" : ""}"><label>At time</label><input id="se-tod" type="time" value="${tod}"/></div>
    <div class="row2" id="se-window-row" style="display:${rnd ? "" : "none"};">
      <div class="field"><label>From</label><input id="se-ws" type="time" value="${ws}"/></div>
      <div class="field"><label>To</label><input id="se-we" type="time" value="${we}"/></div>
    </div>`;
}

function bindTimeMode() {
  $all("#se-random button").forEach(b => b.addEventListener("click", () => {
    $all("#se-random button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    const fr = $("#se-fixed-row");
    if (fr) fr.style.display = b.dataset.r === "1" ? "none" : "";
    const wr = $("#se-window-row");
    if (wr) wr.style.display = b.dataset.r === "1" ? "" : "none";
  }));
}

function complexSubfields(st, existing) {
  if (st === "weekdays") {
    const sel = existing?.weekdays || [];
    return `<div class="field"><label>Days</label><div class="pill-row">${WEEKDAY_PILLS.map((w,i)=>`<div class="pill ${sel.includes(i)?"active":""}" data-weekday="${i}">${w}</div>`).join("")}</div></div>${timeModeHtml(existing)}`;
  }
  if (st === "monthdays") {
    const sel = existing?.monthdays || [];
    return `<div class="field"><label>Days of month</label><div class="pill-row">${Array.from({length:31},(_,i)=>i+1).map(d=>`<div class="pill ${sel.includes(d)?"active":""}" data-monthday="${d}">${d}</div>`).join("")}</div></div>${timeModeHtml(existing)}`;
  }
  if (st === "daterange") {
    return `<div class="row2"><div class="field"><label>Start date</label><input id="se-start" type="date" value="${existing?.range_start||""}"/></div><div class="field"><label>End date</label><input id="se-end" type="date" value="${existing?.range_end||""}"/></div></div>${timeModeHtml(existing)}`;
  }
  if (st === "every_ndays") {
    return `<div class="field"><label>Every (days)</label><input id="se-period" type="number" min="1" value="${existing?.period_days || 1}"/></div>${timeModeHtml(existing)}`;
  }
  if (st === "cron") {
    return `<div class="field"><label>Cron (minute hour day month weekday)</label><input id="se-cron" placeholder="0 9 * * *" value="${existing?.cron_expr || ""}"/></div>`;
  }
  return timeModeHtml(existing);
}

function renderComplexSubfields(st, existing) {
  const fields = $("#sched-editor-fields");
  fields.innerHTML = `
    <div class="field"><label>Repeat</label><select id="se-complex-type">${COMPLEX_TYPES.map(c => `<option value="${c.id}" ${c.id===st?"selected":""}>${c.label}</option>`).join("")}</select></div>
    ${complexSubfields(st, existing)}
  `;
  $("#se-complex-type").addEventListener("change", (e) => renderComplexSubfields(e.target.value, existing));
  bindComplexPills();
  bindTimeMode();
}

function bindComplexPills() {
  $all("[data-weekday]").forEach(p => p.addEventListener("click", () => p.classList.toggle("active")));
  $all("[data-monthday]").forEach(p => p.addEventListener("click", () => p.classList.toggle("active")));
}

function secsToNUnit(secs) {
  const units = Object.entries(UNIT_SECONDS).sort((a,b) => b[1] - a[1]);
  for (const [unit, mult] of units) {
    if (secs % mult === 0) return { n: secs / mult, unit };
  }
  return { n: secs, unit: "Seconds" };
}

function localDT(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const pad = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function readSchedEditor() {
  if (editorType === "once") {
    const rnd = !!document.querySelector("#se-random button.active")?.dataset.r === "1";
    if (rnd) {
      const lo = $("#se-at-min")?.value, hi = $("#se-at-max")?.value;
      return { schedule_type: "once", at: null,
        at_min: lo ? new Date(lo).toISOString() : null,
        at_max: hi ? new Date(hi).toISOString() : null };
    }
    const v = $("#se-at")?.value;
    return { schedule_type: "once", at: v ? new Date(v).toISOString() : null, time_of_day: null };
  }
  if (editorType === "interval") {
    const rnd = !!document.querySelector("#se-random button.active")?.dataset.r === "1";
    const unit = $("#se-unit")?.value || "Hours";
    if (rnd) {
      const lo = parseInt($("#se-lo")?.value || "1", 10);
      const hi = parseInt($("#se-hi")?.value || String(lo), 10);
      const loSec = Math.max(1, lo) * UNIT_SECONDS[unit];
      const hiSec = Math.max(1, hi) * UNIT_SECONDS[unit];
      return { schedule_type: "interval", min_seconds: Math.min(loSec, hiSec), max_seconds: Math.max(loSec, hiSec) };
    }
    const n = parseInt($("#se-n")?.value || "1", 10);
    const secs = Math.max(1, n) * UNIT_SECONDS[unit];
    return { schedule_type: "interval", min_seconds: secs, max_seconds: secs };
  }
  // complex
  const st = $("#se-complex-type")?.value || "every_day";
  const rnd = !!document.querySelector("#se-random button.active")?.dataset.r === "1";
  const base = { schedule_type: st, time_of_day: $("#se-tod")?.value || "09:00" };
  if (rnd) {
    base.random_time = true;
    base.window_start = $("#se-ws")?.value || "00:00";
    base.window_end = $("#se-we")?.value || "23:59";
    base.time_of_day = null;
  } else {
    base.random_time = false;
    base.window_start = null; base.window_end = null;
  }
  if (st === "weekdays") base.weekdays = [...$all("[data-weekday].active")].map(p => parseInt(p.dataset.weekday, 10));
  if (st === "monthdays") base.monthdays = [...$all("[data-monthday].active")].map(p => parseInt(p.dataset.monthday, 10));
  if (st === "daterange") { base.range_start = $("#se-start")?.value; base.range_end = $("#se-end")?.value; }
  if (st === "every_ndays") { base.period_days = parseInt($("#se-period")?.value || "1", 10) || 1; }
  if (st === "cron") { base.cron_expr = $("#se-cron")?.value.trim(); }
  return base;
}

$("#sched-type-row").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  editorType = btn.dataset.type;
  $all("#sched-type-row button").forEach(b => b.classList.toggle("active", b === btn));
  renderSchedEditor(null);
});

$("#sched-ok").addEventListener("click", () => {
  const d = readSchedEditor();
  if (editingSchedIdx !== null) formState.schedules[editingSchedIdx] = d;
  else formState.schedules.push(d);
  $("#sched-editor-backdrop").classList.remove("show");
  renderScheduleList();
});

$("#sched-cancel").addEventListener("click", () => $("#sched-editor-backdrop").classList.remove("show"));

// ---------------------------------------------------------------------------
// Image upload
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
  if (file.size > 5 * 1024 * 1024) { $("#img-error").textContent = "Image is larger than 5 MB"; return; }
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
  if (formState.image_id) api(`/images/${formState.image_id}`, { method: "DELETE" }).catch(() => {});
  resetImageUI(null);
}

// ---------------------------------------------------------------------------
// Save
// ---------------------------------------------------------------------------
async function saveForm() {
  formState.text = textInput().value.trim();
  formState.note = $("#f-note").value.trim() || null;
  formState.message_template = $("#f-template").value.trim() || null;
  formState.tz = (formState.mode === "countdown" ? $("#c-tz") : $("#f-tz")).value;

  if (!formState.text) { toast("Enter a reminder text", "err"); return; }

  let body;
  if (formState.mode === "countdown") {
    formState.target_date = $("#c-date").value;
    formState.notification_time = $("#c-time").value || "09:00";
    formState.countdown_days_before = parseInt($("#c-days").value || "0", 10) || 0;
    if (!formState.target_date) { toast("Pick a target date", "err"); return; }
    body = {
      kind: "countdown", text: formState.text, tz: formState.tz,
      note: formState.note, image_id: formState.image_id || null,
      message_template: formState.message_template, tags: formState.tags,
      target_date: formState.target_date, notification_time: formState.notification_time,
      countdown_days_before: formState.countdown_days_before,
    };
  } else {
    if (!formState.schedules.length) { toast("Add at least one schedule", "err"); return; }
    body = {
      kind: "reminder", text: formState.text, tz: formState.tz,
      note: formState.note, image_id: formState.image_id || null,
      message_template: formState.message_template, tags: formState.tags,
      schedules: formState.schedules,
    };
  }

  try {
    if (formState.id === null) {
      await api("/reminders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      toast("Created");
    } else {
      await api(`/reminders/${formState.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      toast("Updated");
    }
    showView("#list-view");
    loadList();
  } catch (e) { toast(e.message, "err"); }
}

window.saveForm = saveForm;

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------
async function openSettings() {
  try {
    const s = await api("/settings");
    const cur = s.timezone || s.default_tz || "UTC";
    populateTimezones($("#s-tz"), cur);
    const durs = (s.snooze_durations && s.snooze_durations.length) ? s.snooze_durations : [300, 600, 1800];
    $("#sd1").value = Math.round(durs[0] / 60);
    $("#sd2").value = Math.round(durs[1] / 60);
    $("#sd3").value = Math.round(durs[2] / 60);
    showView("#settings-view");
  } catch (e) { toast(e.message, "err"); }
}
async function saveSettings() {
  const tz = $("#s-tz").value || null;
  const durs = ["sd1", "sd2", "sd3"].map(id => Math.max(1, parseInt($("#"+id).value || "0", 10)) * 60).filter(v => v > 0);
  try {
    await api("/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ timezone: tz, snooze_durations: durs }) });
    SNOOZE_DURATIONS = durs.length ? durs : [300, 600, 1800];
    toast("Settings saved");
    showView("#list-view");
    loadList();
  } catch (e) { toast(e.message, "err"); }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
$("#fab-create").addEventListener("click", openCreate);
$("#btn-back").addEventListener("click", () => { showView("#list-view"); loadList(); });
$("#btn-cancel-form").addEventListener("click", () => { showView("#list-view"); loadList(); });
$("#btn-save").addEventListener("click", saveForm);
$("#btn-settings").addEventListener("click", openSettings);
$("#btn-back-settings").addEventListener("click", () => showView("#list-view"));
$("#btn-close-settings").addEventListener("click", () => showView("#list-view"));
$("#btn-save-settings").addEventListener("click", saveSettings);
$("#btn-close").addEventListener("click", () => tg?.close?.());
$("#btn-close-form").addEventListener("click", () => { showView("#list-view"); loadList(); });
$("#add-schedule").addEventListener("click", openSchedulePicker);
$("#btn-close-sched").addEventListener("click", () => $("#sched-picker-backdrop").classList.remove("show"));
$("#mode-row").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (btn) switchMode(btn.dataset.mode);
});
$("#img-tile").addEventListener("click", () => $("#f-image-file").click());
$("#f-image-file").addEventListener("change", (e) => { onImagePicked(e.target.files[0]); e.target.value = ""; });
$("#img-remove").addEventListener("click", onImageRemoved);

// init
populateTimezones($("#f-tz"), "UTC");
populateTimezones($("#c-tz"), "UTC");
switchMode("regular");
loadList();
