const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
let cfg = null;
let clips = [];
let status = {};
let saveTimer = null;

async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.body = JSON.stringify(opts.json);
    init.headers = { "Content-Type": "application/json" };
  }
  const res = await fetch(path, init);
  if (res.status === 401) { location.href = "/login"; throw new Error("Log in first"); }
  const data = await res.json().catch(() => ({ ok: false, error: res.statusText }));
  if (!data.ok) throw new Error(data.error || "Request failed");
  return data.data;
}

function toast(text, bad = false) {
  const t = $("#toast");
  t.textContent = text;
  t.classList.toggle("bad", bad);
  t.classList.add("show");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove("show"), 3200);
}

async function guard(btn, fn) {
  if (btn) btn.disabled = true;
  try {
    return await fn();
  } catch (e) {
    toast(e.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => (k in n && k !== "list" ? (n[k] = v) : n.setAttribute(k, v)));
  kids.forEach((k) => n.append(k));
  return n;
}

async function loadAll() {
  [cfg, clips, status] = await Promise.all([api("/api/config"), api("/api/clips"), api("/api/status")]);
  renderReactions();
  renderSettings();
  renderStatus();
}

function renderStatus() {
  const pill = $("#state-pill");
  pill.textContent = status.state + (status.enabled ? "" : " (asleep)") + (status.sent ? ` · ${status.sent} sent` : "");
  pill.className = "pill " + status.state;
  $("#dry-run-toggle").checked = status.dry_run;
  const live = ["listening", "connecting"].includes(status.state);
  const go = $("#go-button");
  go.textContent = live ? "Stop" : "Go live";
  go.classList.toggle("danger", live);
  go.classList.toggle("primary", !live);
  if (document.activeElement !== $("#thread-url")) $("#thread-url").value = status.thread_url || "";
  const counts = status.clips || {};
  $("#step-clips").classList.toggle("done", Object.values(counts).some((n) => n > 0));
  $("#step-login").classList.toggle("done", status.browser);
  $("#step-thread").classList.toggle("done", !!status.thread_url);
  $("#step-live").classList.toggle("done", status.state === "listening");
  const sz = status.snooze || { mode: "off" };
  const until = sz.until ? new Date(sz.until * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
  $("#now-status").textContent = sz.mode === "pause" ? `Quiet until ${until}.` : sz.mode === "mentions" ? `Only answering tags until ${until}.` : "Walter is acting normally.";
  $$(".now-btn").forEach((b) => b.classList.toggle("active", sz.mode !== "off" && $("b", b).textContent === sz.label));
  $("#care-note").hidden = !status.care_until;
  $("#remote-section").hidden = !status.browser;
  $("#remote-url").textContent = status.needs_login ? "Facebook wants a login or security check. Finish it on the screen below." : status.url;
  if (status.browser) remoteLoop();
}

function renderReactions() {
  const grid = $("#reaction-grid");
  grid.replaceChildren();
  Object.entries(cfg.reactions).sort().forEach(([name, r]) => {
    const node = $("#reaction-tpl").content.firstElementChild.cloneNode(true);
    const mine = clips.filter((c) => c.reaction === name);
    $("h3", node).textContent = name;
    $(".count", node).textContent = mine.length ? `${mine.length} clip${mine.length > 1 ? "s" : ""}` : "no clips yet";
    node.classList.toggle("empty-bank", !mine.length);
    mine.forEach((c) => {
      const v = el("video", { src: "/clip/" + encodeURI(c.path), muted: true, loop: true, preload: "metadata", playsInline: true });
      v.addEventListener("mouseenter", () => v.play().catch(() => {}));
      v.addEventListener("mouseleave", () => v.pause());
      const del = el("button", { type: "button", title: "Delete " + c.name, textContent: "×" });
      del.addEventListener("click", () => guard(del, async () => {
        if (!confirm(`Delete ${c.name}?`)) return;
        await api("/api/clips/" + encodeURI(c.path), { method: "DELETE" });
        await loadAll();
      }));
      $(".clips", node).append(el("div", { className: "clip" }, v, del, el("span", { textContent: c.duration + "s" + (c.audio ? " ♪" : "") })));
    });
    const kw = $(".kw", node);
    const lines = $(".lines", node);
    kw.value = r.keywords.join(", ");
    lines.value = r.lines.join("\n");
    const commit = () => {
      cfg.reactions[name] = {
        keywords: kw.value.split(",").map((s) => s.trim()).filter(Boolean),
        lines: lines.value.split("\n").map((s) => s.trim()),
      };
      queueSave({ reactions: cfg.reactions });
    };
    kw.addEventListener("input", commit);
    lines.addEventListener("input", commit);
    $(".del-reaction", node).addEventListener("click", () => {
      if (!confirm(`Remove reaction "${name}"? Its clip files stay on disk.`)) return;
      delete cfg.reactions[name];
      queueSave({ reactions: cfg.reactions }, true).then(loadAll);
    });
    const drop = $(".drop", node);
    const input = $("input[type=file]", drop);
    const send = (files) => guard(null, async () => {
      if (!files.length) return;
      const fd = new FormData();
      fd.append("reaction", name);
      [...files].forEach((f) => fd.append("files", f));
      drop.textContent = "Uploading…";
      await api("/api/clips", { method: "POST", body: fd });
      toast(`Added ${files.length} clip(s) to ${name}`);
      await loadAll();
    });
    input.addEventListener("change", () => send(input.files));
    ["dragenter", "dragover"].forEach((e) => drop.addEventListener(e, (ev) => { ev.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((e) => drop.addEventListener(e, () => drop.classList.remove("over")));
    drop.addEventListener("drop", (ev) => { ev.preventDefault(); send(ev.dataTransfer.files); });
    grid.append(node);
  });
}

function queueSave(patch, now = false) {
  clearTimeout(saveTimer);
  return new Promise((resolve) => {
    saveTimer = setTimeout(async () => {
      await guard(null, async () => { cfg = await api("/api/config", { method: "PUT", json: patch }); });
      resolve();
    }, now ? 0 : 600);
  });
}

function getPath(o, p) { return p.split(".").reduce((a, k) => (a ? a[k] : undefined), o); }

function renderSettings() {
  const f = $("#settings-form");
  $$("[data-reactions]", f).forEach((s) => {
    const cur = getPath(cfg, s.name);
    s.replaceChildren(...Object.keys(cfg.reactions).sort().map((n) => el("option", { value: n, textContent: n })));
    s.value = cur;
  });
  const vs = $("#voice-select");
  if (!vs.options.length) vs.append(el("option", { value: cfg.tts_voice, textContent: cfg.tts_voice }));
  $$("[name]", f).forEach((i) => {
    const v = getPath(cfg, i.name);
    if (i.type === "checkbox") i.checked = !!v;
    else if (i.dataset.percent !== undefined) i.value = +(v * 100).toFixed(2);
    else if (i.dataset.list !== undefined) i.value = (v || []).join(", ");
    else if (v !== undefined) i.value = v;
  });
}

function readSettings() {
  const out = {};
  $$("#settings-form [name]").forEach((i) => {
    let v = i.type === "checkbox" ? i.checked : i.value;
    if (i.type === "number") v = Number(v);
    if (i.dataset.percent !== undefined) v = Number(i.value) / 100;
    if (i.dataset.list !== undefined) v = i.value.split(",").map((s) => s.trim()).filter(Boolean);
    const [a, b] = i.name.split(".");
    if (b) (out[a] = out[a] || { ...cfg[a] })[b] = v;
    else out[a] = v;
  });
  return out;
}

function logLine(ev) {
  const li = el("li", { className: ev.level });
  const time = el("time", { textContent: new Date(ev.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) });
  const span = el("span", { textContent: ev.text + " " });
  if (ev.why) span.append(el("em", { textContent: "· " + ev.why }));
  if (ev.file) {
    const a = el("a", { href: "/render/" + ev.file, target: "_blank", textContent: " ▶ view" });
    span.append(a);
  }
  li.append(time, span);
  const list = $("#log-list");
  list.prepend(li);
  while (list.children.length > 300) list.lastChild.remove();
}

let lastEvent = 0;
async function pollLog() {
  try {
    const evs = await api("/api/log?since=" + lastEvent);
    evs.forEach((ev) => {
      lastEvent = Math.max(lastEvent, ev.t);
      logLine(ev);
    });
    if (evs.some((ev) => ["ok", "error", "fire", "info"].includes(ev.level))) refreshStatus();
  } catch (_) {}
  setTimeout(pollLog, 2000);
}

let remoteTimer = null;
let remoteBusy = false;
function remoteLoop() {
  if (remoteTimer) return;
  const img = $("#remote-screen");
  const tick = () => {
    if ($("#remote-section").hidden) { remoteTimer = null; return; }
    if (!remoteBusy && !document.hidden) {
      remoteBusy = true;
      const next = new Image();
      next.onload = () => { img.src = next.src; remoteBusy = false; };
      next.onerror = () => { remoteBusy = false; };
      next.src = "/api/remote/shot?t=" + Date.now();
    }
    remoteTimer = setTimeout(tick, 1200);
  };
  tick();
}

async function remote(action, extra = {}) {
  await guard(null, async () => {
    const res = await api("/api/remote/act", { method: "POST", json: { action, ...extra } });
    $("#remote-url").textContent = res.needs_login ? "Facebook wants a login or security check. Finish it on the screen below." : res.url;
  });
}

$("#remote-screen").addEventListener("click", (e) => {
  const r = e.currentTarget.getBoundingClientRect();
  remote("click", { x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height });
});
$("#remote-type").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = $("#remote-text");
  if (t.value) remote("type", { text: t.value });
  t.value = "";
});
$$("#remote-type [data-key]").forEach((b) => b.addEventListener("click", () => remote("key", { key: b.dataset.key })));
$$("[data-remote]").forEach((b) => b.addEventListener("click", () => {
  const a = b.dataset.remote;
  if (a === "scroll-up") remote("scroll", { dy: -500 });
  else if (a === "scroll-down") remote("scroll", { dy: 500 });
  else if (a === "home") remote("goto", { url: "https://www.facebook.com/messages/" });
  else remote(a);
}));

async function refreshStatus() {
  try { status = await api("/api/status"); renderStatus(); } catch (_) {}
}

function showResult(res) {
  const box = $("#test-result");
  box.replaceChildren();
  box.classList.toggle("empty", !res);
  if (!res) { box.textContent = "No reaction. Walter ignores this one (see the log for why)."; return; }
  if (res.command) { box.textContent = "Walter would reply: " + res.reply; return; }
  const v = el("video", { src: "/render/" + res.file + "?t=" + Date.now(), controls: true, autoplay: true, playsInline: true });
  const meta = el("div", { className: "meta" });
  meta.append("Reaction ", el("b", { textContent: res.reaction }), " · ", res.reason, " · clip ", res.clip, res.line ? ` · says "${res.line}"` : "");
  box.append(v, meta);
}

$("#test-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const btn = $("button", e.target);
  guard(btn, async () => {
    btn.textContent = "Rendering…";
    try {
      showResult(await api("/api/test", { method: "POST", json: { sender: $("#test-sender").value, text: $("#test-text").value } }));
    } finally { btn.textContent = "Render"; }
  });
});

$("#settings-form").addEventListener("submit", (e) => {
  e.preventDefault();
  guard($("button", e.target), async () => {
    cfg = await api("/api/config", { method: "PUT", json: readSettings() });
    renderSettings();
    $("#settings-hint").textContent = "Saved " + new Date().toLocaleTimeString();
  });
});

$("#new-reaction").addEventListener("submit", (e) => {
  e.preventDefault();
  const name = $("#new-reaction-name").value.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "_");
  if (!name) return;
  if (cfg.reactions[name]) return toast("That reaction already exists", true);
  cfg.reactions[name] = { keywords: [name], lines: [""] };
  $("#new-reaction-name").value = "";
  queueSave({ reactions: cfg.reactions }, true).then(loadAll);
});

$("#starter-image").addEventListener("change", (e) => {
  const file = e.target.files[0];
  if (!file) return;
  guard(null, async () => {
    $("#starter-hint").textContent = "Building 8 clips…";
    const fd = new FormData();
    fd.append("image", file);
    const made = await api("/api/starter", { method: "POST", body: fd });
    $("#starter-hint").textContent = `Built ${made.length} clips. Replace them with real animations any time.`;
    await loadAll();
  });
});

const act = (id, action, body) => $(id).addEventListener("click", (e) => guard(e.currentTarget, async () => {
  const res = await api("/api/bot/" + action, { method: "POST", json: body ? body() : {} });
  if (action === "diagnose") {
    const last = res.last_messages.at(-1);
    $("#diag-out").textContent = (res.pane ? "Chat found" : "Chat pane not found") + ` · ${res.row} messages · composer ${res.composer} · upload ${res.file_input} · ` +
      (last ? `last: ${last.outgoing ? "You" : last.sender}: ${last.text}` : "no messages read");
  }
  await refreshStatus();
}));
act("#open-browser", "open");
act("#close-browser", "close");
act("#capture-thread", "capture");
act("#diagnose", "diagnose");

$("#thread-url").addEventListener("change", (e) => {
  const v = e.target.value.trim();
  if (v && !/^https:\/\/(www\.)?(facebook|messenger)\.com\//.test(v)) return toast("Paste a facebook.com/messages link", true);
  queueSave({ thread_url: v }, true).then(refreshStatus);
});

$("#go-button").addEventListener("click", (e) => guard(e.currentTarget, async () => {
  const live = ["listening", "connecting"].includes(status.state);
  await api("/api/bot/" + (live ? "stop" : "start"), { method: "POST", json: {} });
  await refreshStatus();
}));

$("#dry-run-toggle").addEventListener("change", (e) => guard(null, async () => {
  if (!e.target.checked && !confirm("Turn off Dry run? Walter will post real clips in the group chat.")) {
    e.target.checked = true;
    return;
  }
  cfg = await api("/api/config", { method: "PUT", json: { dry_run: e.target.checked } });
  await refreshStatus();
}));

$("#import-login").addEventListener("change", (e) => guard(null, async () => {
  const f = e.target.files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  await api("/api/login/import", { method: "POST", body: fd });
  toast("Login imported");
  e.target.value = "";
  await refreshStatus();
}));

$$(".now-btn").forEach((b) => b.addEventListener("click", () => guard(b, async () => {
  await api("/api/bot/snooze", { method: "POST", json: { mode: b.dataset.mode, minutes: Number(b.dataset.min), label: $("b", b).textContent } });
  await refreshStatus();
})));
$("#care-clear").addEventListener("click", (e) => guard(e.currentTarget, async () => {
  await api("/api/bot/care_clear", { method: "POST", json: {} });
  await refreshStatus();
}));

$("#clear-log").addEventListener("click", () => $("#log-list").replaceChildren());

api("/api/voices").then((vs) => {
  const s = $("#voice-select");
  s.replaceChildren(...vs.map((v) => el("option", { value: v, textContent: v })));
  s.value = cfg ? cfg.tts_voice : "";
}).catch(() => {});

loadAll().catch((e) => toast(e.message, true));
pollLog();
setInterval(refreshStatus, 5000);
