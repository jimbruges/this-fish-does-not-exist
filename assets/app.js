const NUM = ["generate_interval_s", "draw_duration_s", "waveform"];
const BOOL = ["progressive", "use_llm", "portrait_flip"];
const SEL = ["mode"];
const TEXT = ["timezone"];

const $ = (id) => document.getElementById(id);

async function api(method, path, body) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  return r.json();
}

function fillSettings(s) {
  NUM.forEach((k) => { if (k in s) $(k).value = s[k]; });
  BOOL.forEach((k) => { if (k in s) $(k).checked = !!s[k]; });
  SEL.forEach((k) => { if (k in s) $(k).value = s[k]; });
  TEXT.forEach((k) => { if (k in s) $(k).value = s[k]; });
  if ("min_interval_s" in s) $("minint").textContent = s.min_interval_s;
  if ("now" in s) $("now").textContent = s.now;
}

function readSettings() {
  const out = {};
  NUM.forEach((k) => { out[k] = Number($(k).value); });
  BOOL.forEach((k) => { out[k] = $(k).checked; });
  SEL.forEach((k) => { out[k] = $(k).value; });
  TEXT.forEach((k) => { out[k] = $(k).value.trim(); });
  return out;
}

async function loadTimezones() {
  try {
    const r = await api("GET", "/timezones");
    const dl = $("tzlist");
    (r.zones || []).forEach((z) => {
      const o = document.createElement("option");
      o.value = z;
      dl.appendChild(o);
    });
  } catch (e) { /* picker just falls back to free text */ }
}

let lastCount = -1;

async function refresh() {
  try {
    const s = await api("GET", "/status");
    const busy = s.status !== "ready";
    $("st").textContent = s.status;
    $("st").className = "pill " + (s.last_error ? "error" : busy ? "busy" : "ready");
    $("count").textContent = s.fish_count;
    $("name").textContent = s.last_name || "—";
    $("when").textContent = s.last_generated_at || "—";
    $("src").textContent = s.last_text_source || "—";
    $("src").className = "pill" + (s.last_text_source === "llm" ? " ready" : "");
    $("err").textContent = s.last_error || "";
    $("generate").disabled = busy;
    if (s.fish_count !== lastCount) {
      lastCount = s.fish_count;
      const p = await api("GET", "/preview");
      if (p.image) $("preview").src = p.image;
    }
  } catch (e) {
    $("err").textContent = String(e);
  }
}

// don't stomp on a textarea the user is selecting text in
function setArea(id, value) {
  const el = $(id);
  if (document.activeElement !== el && el.value !== value) el.value = value;
}

async function refreshLlm() {
  try {
    const l = await api("GET", "/llm");
    setArea("llm_prompt", l.prompt || "");
    setArea("llm_raw", l.raw || "");
    setArea("llm_text", l.text || "");
    $("src2").textContent = l.source || "—";
    $("src2").className = "pill" + (l.source === "llm" ? " ready" : "");
    $("llmerr").textContent = l.error ? " " + l.error : "";
  } catch (e) { /* ignore */ }
}

$("generate").onclick = () => api("POST", "/generate").then(refresh);
$("save").onclick = async () => {
  fillSettings(await api("POST", "/settings", readSettings()));
  $("save").textContent = "Saved ✓";
  setTimeout(() => ($("save").textContent = "Save settings"), 1200);
};

loadTimezones();
api("GET", "/settings").then(fillSettings);
refresh();
refreshLlm();
setInterval(refresh, 1000);
setInterval(refreshLlm, 3000);
// keep the clock preview honest without re-reading the whole form
setInterval(async () => {
  try {
    const s = await api("GET", "/settings");
    if ("now" in s) $("now").textContent = s.now;
  } catch (e) { /* ignore */ }
}, 15000);
