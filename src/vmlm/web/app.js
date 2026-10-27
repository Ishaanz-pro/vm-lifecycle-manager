"use strict";

// Dashboard for the VM Lifecycle Manager API. No framework, no build step. Every value that
// comes from the server is written with textContent / setAttribute, never innerHTML.

const POLL_MS = 1500;
const BUSY = new Set(["PROVISIONING", "STARTING", "STOPPING", "TERMINATING"]);

const $ = (id) => document.getElementById(id);

const state = {
  apiKey: null,
  authRequired: false,
  selectedId: null,
  lastCreate: null, // { body, key, opId }
  pollTimer: null,
};

// ---- helpers ------------------------------------------------------------------------------

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === false || v == null) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

function uuid() {
  if (globalThis.crypto && crypto.randomUUID) return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

function parseTime(s) {
  if (!s) return null;
  // The API returns UTC; treat a timestamp without a zone designator as UTC.
  return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z");
}

function ago(s) {
  const t = parseTime(s);
  if (!t) return "";
  const sec = Math.max(0, Math.round((Date.now() - t.getTime()) / 1000));
  if (sec < 5) return "just now";
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  return `${Math.floor(sec / 3600)}h ago`;
}

function clock(s) {
  const t = parseTime(s);
  return t ? t.toLocaleTimeString([], { hour12: false }) : "";
}

const short = (id) => (id ? id.slice(0, 8) : "");
const gib = (mib) => (mib / 1024).toFixed(mib % 1024 ? 1 : 0);

function pill(text, extra = "") {
  const busy = BUSY.has(text) || text === "PENDING" ? " busy" : "";
  const cls = text === "RUNNING" && extra === "op" ? "op-running" : text.toLowerCase();
  return el("span", { class: `pill ${cls}${busy}`, text });
}

function toast(message, kind = "") {
  const node = el("div", { class: `toast ${kind}`, text: message });
  $("toasts").append(node);
  setTimeout(() => node.remove(), kind === "error" ? 8000 : 5000);
}

// ---- API client ---------------------------------------------------------------------------

class ApiError extends Error {
  constructor(status, body) {
    super(body.message || `HTTP ${status}`);
    this.status = status;
    this.code = body.code || "error";
    this.details = body.details || {};
  }
}

function logCall(method, path, status, requestId, ms) {
  const cls = status === 0 ? "s0" : `s${String(status)[0]}`;
  const li = el(
    "li",
    {},
    el("span", { class: "method", text: method }),
    el("span", {}, `${path} `, el("span", { class: cls, text: status === 0 ? "network error" : status })),
    el("span", { class: "rid", text: `${ms}ms${requestId ? " · " + requestId.slice(0, 12) : ""}` }),
  );
  const list = $("api-log");
  list.prepend(li);
  while (list.children.length > 8) list.lastChild.remove();
}

async function api(method, path, { body, headers = {}, quiet = false } = {}) {
  const h = { ...headers };
  if (state.apiKey) h["X-API-Key"] = state.apiKey;
  if (body !== undefined) h["Content-Type"] = "application/json";
  const t0 = performance.now();
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: h,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    if (!quiet) logCall(method, path, 0, null, Math.round(performance.now() - t0));
    throw new ApiError(0, { message: "Could not reach the server", code: "network" });
  }
  const ms = Math.round(performance.now() - t0);
  const rid = res.headers.get("x-request-id");
  if (!quiet) logCall(method, path, res.status, rid, ms);
  let data = null;
  try {
    data = await res.json();
  } catch (_) {
    data = null;
  }
  if (!res.ok) throw new ApiError(res.status, (data && data.error) || { message: res.statusText });
  return { status: res.status, data };
}

function errorText(err) {
  if (err.status === 409 && err.code === "insufficient_capacity") {
    return `Rejected (409): ${err.message}`;
  }
  return `${err.status ? `HTTP ${err.status}: ` : ""}${err.message}`;
}

// ---- rendering ----------------------------------------------------------------------------

function setConn(up, text) {
  const node = $("conn");
  node.className = `conn ${up ? "up" : "down"}`;
  node.lastChild.textContent = text;
}

function meter(label, used, total, fmt) {
  const pct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;
  const bar = el("div", { class: `bar${pct >= 100 ? " full" : pct >= 75 ? " hot" : ""}` }, el("span"));
  bar.firstChild.style.width = `${pct}%`;
  return el(
    "div",
    { class: "meter" },
    el("div", { class: "meter-label" }, el("span", { text: label }), el("span", { text: `${fmt(used)} / ${fmt(total)}` })),
    bar,
  );
}

function renderHosts(cap) {
  const root = $("hosts");
  root.replaceChildren(
    ...cap.hosts.map((h) =>
      el(
        "div",
        {},
        el("div", { class: "host-top" }, el("span", { class: "host-name", text: h.name }), pill(h.status)),
        meter("vCPU reserved", h.reserved_vcpus, h.total_vcpus, String),
        meter("Memory reserved", h.reserved_memory_mib, h.total_memory_mib, (m) => `${gib(m)} GiB`),
      ),
    ),
  );
}

function actionButton(label, kind, enabled, onClick) {
  return el("button", { class: `btn small ${kind}`, type: "button", disabled: !enabled, onclick: onClick, text: label });
}

function renderInstances(items, hostNames) {
  const showTerminated = $("show-terminated").checked;
  const rows = items
    .filter((i) => showTerminated || i.state !== "TERMINATED")
    .sort((a, b) => parseTime(b.created_at) - parseTime(a.created_at));
  $("instances-empty").hidden = rows.length > 0;
  $("instances").replaceChildren(
    ...rows.map((i) => {
      const busy = BUSY.has(i.state);
      const nameBtn = el("button", {
        type: "button",
        text: i.name,
        "aria-label": `Show details for ${i.name}`,
        onclick: () => selectInstance(i.id),
      });
      return el(
        "tr",
        { class: i.id === state.selectedId ? "sel" : "" },
        el("td", { class: "name" }, nameBtn),
        el("td", {}, pill(i.state)),
        el("td", { text: hostNames.get(i.host_id) || "" }),
        el("td", { text: `${i.vcpus} vCPU · ${gib(i.memory_mib)} GiB` }),
        el("td", { class: "muted", text: ago(i.created_at) }),
        el(
          "td",
          { class: "actions" },
          actionButton("Start", "", !busy && i.state === "STOPPED", () => act(i, "start")),
          actionButton("Stop", "", !busy && i.state === "RUNNING", () => act(i, "stop")),
          actionButton(
            "Terminate",
            "danger",
            !busy && ["RUNNING", "STOPPED", "FAILED"].includes(i.state),
            () => act(i, "terminate"),
          ),
        ),
      );
    }),
  );
}

function kv(label, value, mono = false) {
  return el("div", {}, el("dt", { text: label }), el("dd", { class: mono ? "mono" : "", text: value ?? "" }));
}

function renderDetail(inst, ops, events, hostNames) {
  const opList = el("ul", { class: "timeline" });
  for (const o of ops) {
    const what = `${o.action} → ${o.target_state}`;
    const meta = [
      `attempt ${o.attempts}/${o.max_attempts}`,
      o.error ? `${o.error_kind || "error"}: ${o.error}` : null,
      o.request_id ? `request ${o.request_id.slice(0, 12)}` : null,
    ].filter(Boolean);
    opList.append(
      el(
        "li",
        {},
        el("time", { text: clock(o.created_at) }),
        el(
          "div",
          {},
          el("span", { class: "ev", text: `${what} ` }),
          pill(o.status, "op"),
          el("div", { class: "meta", text: meta.join(" · ") }),
        ),
      ),
    );
  }

  const evList = el("ul", { class: "timeline" });
  for (const e of events) {
    const change = e.from_state || e.to_state ? `${e.from_state || "–"} → ${e.to_state || "–"}` : "";
    evList.append(
      el(
        "li",
        {},
        el("time", { text: clock(e.created_at) }),
        el(
          "div",
          {},
          el("span", { class: "ev", text: e.event_type }),
          change ? el("span", { text: `  ${change}` }) : null,
          e.detail ? el("div", { class: "meta", text: e.detail }) : null,
        ),
      ),
    );
  }

  $("detail").replaceChildren(
    el(
      "dl",
      { class: "kv" },
      kv("Instance", inst.name),
      kv("State", `${inst.state} (desired ${inst.desired_state})`),
      kv("Host", hostNames.get(inst.host_id) || "none"),
      kv("Version", String(inst.version)),
      kv("Id", inst.id, true),
      kv("Provider id", inst.provider_id, true),
      inst.failure_reason ? kv("Failure", inst.failure_reason) : null,
    ),
    el("h3", { text: "Operations (newest first)" }),
    ops.length ? opList : el("p", { class: "muted", text: "None." }),
    el("h3", { text: "Audit events (newest first)" }),
    events.length ? evList : el("p", { class: "muted", text: "None." }),
  );
}

// ---- actions ------------------------------------------------------------------------------

async function act(inst, action) {
  const [method, path] =
    action === "terminate"
      ? ["DELETE", `/v1/instances/${inst.id}`]
      : ["POST", `/v1/instances/${inst.id}/${action}`];
  try {
    const { data } = await api(method, path, { headers: { "Idempotency-Key": uuid() } });
    toast(`${action} accepted (202): operation ${short(data.id)} queued`, "ok");
    state.selectedId = inst.id;
  } catch (err) {
    toast(errorText(err), "error");
  }
  refresh();
}

async function createVm(ev) {
  ev.preventDefault();
  const body = {
    name: $("f-name").value.trim(),
    vcpus: Number($("f-vcpus").value),
    memory_mib: Number($("f-mem").value),
  };
  const key = uuid();
  try {
    const { status, data } = await api("POST", "/v1/instances", { body, headers: { "Idempotency-Key": key } });
    state.lastCreate = { body, key, opId: data.id };
    $("replay").disabled = false;
    state.selectedId = data.instance_id;
    toast(`Create accepted (${status}): operation ${short(data.id)} queued`, "ok");
    $("f-name").value = randomName();
  } catch (err) {
    toast(errorText(err), "error");
  }
  refresh();
}

async function replayCreate() {
  const last = state.lastCreate;
  if (!last) return;
  try {
    const { status, data } = await api("POST", "/v1/instances", {
      body: last.body,
      headers: { "Idempotency-Key": last.key },
    });
    toast(
      `Replay returned HTTP ${status} with operation ${short(data.id)}` +
        (data.id === last.opId ? " (the same one): no second VM was created" : ""),
      "ok",
    );
  } catch (err) {
    toast(errorText(err), "error");
  }
  refresh();
}

function randomName() {
  const tail = Math.random().toString(36).slice(2, 6);
  return `vm-${tail}`;
}

function selectInstance(id) {
  state.selectedId = id;
  refresh();
}

// ---- polling ------------------------------------------------------------------------------

async function refresh() {
  try {
    const [cap, inst] = await Promise.all([
      api("GET", "/v1/hosts/capacity", { quiet: true }),
      api("GET", "/v1/instances?limit=100", { quiet: true }),
    ]);
    const hostNames = new Map(cap.data.hosts.map((h) => [h.host_id, h.name]));
    renderHosts(cap.data);
    renderInstances(inst.data.items, hostNames);
    if (state.selectedId) {
      const current = inst.data.items.find((i) => i.id === state.selectedId);
      if (current) {
        const [ops, events] = await Promise.all([
          api("GET", `/v1/instances/${current.id}/operations?limit=50`, { quiet: true }),
          api("GET", `/v1/instances/${current.id}/events?limit=50`, { quiet: true }),
        ]);
        const newest = (a, b) => parseTime(b.created_at) - parseTime(a.created_at) || (b.id > a.id ? 1 : -1);
        renderDetail(current, ops.data.items.sort(newest), events.data.items.sort(newest), hostNames);
      }
    }
    setConn(true, "live");
  } catch (err) {
    if (err.status === 401 || err.status === 403) {
      $("key-card").hidden = false;
      setConn(false, "key required");
    } else {
      setConn(false, "offline, retrying");
    }
  }
}

function schedule() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    if (!document.hidden) await refresh();
    schedule();
  }, POLL_MS);
}

// ---- boot ---------------------------------------------------------------------------------

function loadStoredKey() {
  try {
    return sessionStorage.getItem("vmlm-key");
  } catch (_) {
    return null;
  }
}

function storeKey(key) {
  try {
    sessionStorage.setItem("vmlm-key", key);
  } catch (_) {
    /* private mode: keep it in memory only */
  }
}

async function boot() {
  $("f-name").value = randomName();
  $("presets").addEventListener("click", (ev) => {
    const chip = ev.target.closest(".chip");
    if (!chip) return;
    $("f-vcpus").value = chip.dataset.v;
    $("f-mem").value = chip.dataset.m;
  });
  $("create-form").addEventListener("submit", createVm);
  $("replay").addEventListener("click", replayCreate);
  $("show-terminated").addEventListener("change", refresh);
  $("key-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    state.apiKey = $("key-input").value.trim();
    storeKey(state.apiKey);
    $("key-input").value = "";
    $("key-card").hidden = true;
    refresh();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });

  try {
    const res = await fetch("/ui/config", { cache: "no-store" });
    const cfg = await res.json();
    state.authRequired = cfg.auth_required;
    state.apiKey = cfg.api_key || loadStoredKey();
  } catch (_) {
    /* /ui/config is optional; fall through and let the first API call report the problem */
  }
  if (state.authRequired && !state.apiKey) $("key-card").hidden = false;
  await refresh();
  schedule();
}

boot();
