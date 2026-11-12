/* 旅行规划 Agent · 网页版逻辑（原生 JS，无框架、无 CDN、无构建）
   时段计算规则与 travel/export.py 的 day_schedule 保持一致，页面和导出文件不会打架。 */
"use strict";

const $ = (id) => document.getElementById(id);
const DAY_START = 9 * 60, LUNCH_AT = 12 * 60, LUNCH_MIN = 60;
const MAX_IMAGE = 8 * 1024 * 1024;
const HISTORY_KEY = "ai-travel.history", THEME_KEY = "ai-travel.theme";
const DEFAULT_DROP_TEXT = "点这里选图，或把图片拖进来 · 单图 ≤8 MB · 需多模态模型";
const state = { plan: null, session: null, image: null, timer: 0, history: [], abort: null, progressNote: "", model: "", visualOk: null };

/* ---------------- 小工具 ---------------- */
function add(node, tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  node.appendChild(el);
  return el;
}
function clock(minutes) {
  const m = Math.round(minutes) % (24 * 60);
  return String(Math.floor(m / 60)).padStart(2, "0") + ":" + String(m % 60).padStart(2, "0");
}
function ymd(date) {
  return date.getFullYear() + "-" + String(date.getMonth() + 1).padStart(2, "0") + "-" + String(date.getDate()).padStart(2, "0");
}
function slots(spots) {
  let min = DAY_START, lunchDone = false;
  const out = [];
  for (const spot of spots) {
    if (!lunchDone && min >= LUNCH_AT) {
      lunchDone = true;
      out.push({ lunch: true, begin: min, end: min + LUNCH_MIN });
      min += LUNCH_MIN;
    }
    out.push({ spot, begin: min, end: min + spot.minutes });
    min += spot.minutes;
  }
  return out;
}
function status(message, kind) {
  const el = $("status");
  el.textContent = message;
  el.className = "status" + (kind ? " " + kind : "");
}
function store(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* 隐私模式下写失败就只丢历史，不影响主流程 */ }
}
function read(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) || fallback; } catch (_) { return fallback; }
}

/* ---------------- 主题 ---------------- */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  store(THEME_KEY, theme);
}
applyTheme(read(THEME_KEY, window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
$("theme").addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));

/* ---------------- 请求 ---------------- */
async function post(path, body) {
  const headers = { "Content-Type": "application/json" };
  const token = $("token").value.trim();
  if (token) headers["X-Service-Token"] = token;
  const resp = await fetch(path, { method: "POST", headers, body: JSON.stringify(body) });
  const raw = await resp.text();
  if (!resp.ok) {
    let detail = raw;
    try { detail = JSON.parse(raw).detail || raw; } catch (_) { /* 网关返回非 JSON 时原样显示 */ }
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.code = resp.status;
    throw err;
  }
  return JSON.parse(raw);
}

function busy(on, text) {
  $("go").disabled = on;
  $("progress").hidden = !on;
  const cancel = $("cancel");
  if (cancel) cancel.hidden = !on;
  if (on) {
    const started = Date.now();
    clearInterval(state.timer);
    state.progressNote = text || "正在编排…";
    $("progressText").textContent = state.progressNote + " · 已等待 0 秒";
    state.timer = setInterval(() => {
      $("progressText").textContent = state.progressNote + " · 已等待 " + Math.round((Date.now() - started) / 1000) + " 秒";
    }, 1000);
  } else {
    clearInterval(state.timer);
    state.progressNote = "";
  }
}

/* ---------------- 流式请求 ---------------- */
/* 同步 /plan 要让人盯 20-40 秒白屏，所以默认走 SSE；
   浏览器或代理不支持时自动退回一次性 /plan，功能不降级。 */
async function postStream(path, body, onEvent, signal) {
  const headers = { "Content-Type": "application/json" };
  const token = $("token").value.trim();
  if (token) headers["X-Service-Token"] = token;
  const resp = await fetch(path, { method: "POST", headers, body: JSON.stringify(body), signal });
  if (!resp.ok) {
    const raw = await resp.text();
    let detail = raw;
    try { detail = JSON.parse(raw).detail || raw; } catch (_) { /* 网关非 JSON 时原样显示 */ }
    const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    err.code = resp.status;
    err.retryAfter = Number(resp.headers.get("Retry-After") || 0);
    throw err;
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut = buffer.indexOf("\n\n");
    while (cut >= 0) {
      const frame = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      const line = frame.split("\n").find((item) => item.startsWith("data: "));
      if (line) {
        const payload = line.slice(6).trim();
        if (payload === "[DONE]") return;
        try { onEvent(JSON.parse(payload)); } catch (_) { /* 半帧忽略 */ }
      }
      cut = buffer.indexOf("\n\n");
    }
  }
}

async function streamOrPost(body, onEvent, signal) {
  let result = null;
  const tools = [];
  const done = [];
  const trace = [];
  let writing = null;
  let finalMs = null;
  const emit = () => onEvent && onEvent({ tools, done, writing });
  try {
    await postStream("/plan/stream", body, (ev) => {
      if (ev.type === "tool_call") { tools.push(ev.name); emit(); }
      else if (ev.type === "tool_result") {
        done.push(ev.name);
        // 保留耗时与命中条数：结果区能展示「谁慢、谁真的查到了东西」，而不只是一串工具名
        if (ev.kind === "response") { finalMs = ev.ms; }
        else trace.push({ name: ev.name, ms: ev.ms || null, hits: ev.hits || 0, chars: ev.chars || 0 });
        emit();
      }
      // writing 事件来自结构化工具的 args 分片累计：能报「写到第几天」，
      // 不再只有一条不知道在干什么的进度条
      else if (ev.type === "writing") { writing = ev; emit(); }
      else if (ev.type === "plan") {
        result = {
          plan: ev.plan, session_id: ev.session_id,
          input_tokens: ev.input_tokens, output_tokens: ev.output_tokens, elapsed_ms: ev.elapsed_ms,
          cost_yuan: ev.cost_yuan ?? null, trace, final_ms: finalMs,
        };
      } else if (ev.type === "error") { throw new Error(ev.detail || "流式生成失败"); }
    }, signal);
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    if (err.code === 404 || err.code === 405) {
      // 老版本服务端没有 /plan/stream
      return await post("/plan", body);
    }
    throw err;
  }
  if (!result) throw new Error("流结束了但没拿到完整方案");
  return result;
}

/* 把状态码翻译成人能处理的下一步动作，而不是笼统一句「生成失败」 */
function describeError(err) {
  if (err && err.name === "AbortError") return { text: "已取消生成。", kind: "warn" };
  if (err && err.code === 401) return { text: "这个服务启用了令牌鉴权，请在「服务令牌」里填入后重试。", kind: "error", needToken: true };
  if (err && err.code === 402) return { text: "模型账户余额不足（402），与你的输入无关，充值后重试。", kind: "error" };
  if (err && err.code === 422) return { text: "输入有问题：" + err.message, kind: "error" };
  if (err && err.code === 429) return { text: "请求太频繁，稍后自动恢复（服务端限流）。", kind: "error", wait: err.retryAfter || 60 };
  if (err && err.code === 502) return { text: "模型这次没按 schema 返回（502）。系统已自动兜底过一次，再点一次通常就好。", kind: "error" };
  if (err && /Failed to fetch|NetworkError/i.test(String(err.message || err))) {
    return { text: "连不上服务：确认 http://127.0.0.1:8080 仍在运行。", kind: "error" };
  }
  return { text: "生成失败：" + (err && err.message ? err.message : err), kind: "error" };
}

function startCooldown(seconds) {
  const btn = $("go");
  let left = Math.max(1, Math.round(seconds));
  btn.disabled = true;
  btn.textContent = "请等待 " + left + " 秒";
  const tick = setInterval(() => {
    left -= 1;
    if (left <= 0) {
      clearInterval(tick);
      btn.disabled = false;
      btn.textContent = "生成行程";
    } else {
      btn.textContent = "请等待 " + left + " 秒";
    }
  }, 1000);
}

function revealTokenField() {
  const field = $("tokenField");
  if (field) field.hidden = false;
  $("token").focus();
}

function applyImageCapability() {
  const drop = $("drop");
  const hint = $("dropText");
  if (!drop || !hint) return;
  if (state.visualOk) {
    drop.removeAttribute("aria-disabled");
    hint.textContent = DEFAULT_DROP_TEXT;
  } else {
    drop.setAttribute("aria-disabled", "true");
    hint.textContent = "当前模型 " + (state.model || "") + " 不支持读图；把 .env 里 TRAVEL_MODEL 设为 qwen3.5-plus 后这里才可用";
  }
}

async function generate() {
  const request = $("request").value.trim();
  if (request.length < 2) { status("先写清目的地和天数，例如「杭州 3天 2人 喜欢人文 人均预算1500」。", "error"); return; }
  if (state.image && state.visualOk === false) { status("当前模型不支持读图，请先去掉图片或换用 qwen 多模态模型。", "error"); return; }
  const body = { request };
  if ($("follow").checked && state.session) body.session_id = state.session;
  if (state.image) body.image = state.image;

  const ctl = new AbortController();
  state.abort = ctl;
  let cooldown = 0;
  busy(true, "正在查本地知识库与实时天气…");
  status("");
  try {
    const data = await streamOrPost(body, (prog) => {
      const parts = [];
      if (prog.tools.length) parts.push("已调用 " + prog.tools.join(" → "));
      if (prog.done.length) parts.push("已取回 " + prog.done.length + " 份资料");
      if (prog.trace && prog.trace.length) parts.push(prog.trace.map((s) => s.name + (s.ms != null ? " " + s.ms + "ms" : "")).join(" · "));
      if (prog.writing) parts.push("正在编排方案（已写 " + (prog.writing.chars / 1000).toFixed(1) + "k 字符，写到第 " + Math.max(1, prog.writing.days_seen) + " 天）");
      state.progressNote = parts.join(" · ") || "正在思考…";
    }, ctl.signal);
    state.session = data.session_id;
    showPlan(data.plan, { session: data.session_id, tokens: data.input_tokens + data.output_tokens, ms: data.elapsed_ms, cost: data.cost_yuan, trace: data.trace });
    $("follow").checked = true;
    pushHistory(data.plan, data.session_id, data.input_tokens + data.output_tokens);
    status("行程已生成。", "ok");
    // 只有真实生成才滚动到结果；示例/历史态保持原位，否则首屏标题会被顶出视口
    $("result").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) {
    const info = describeError(err);
    status(info.text, info.kind);
    if (info.needToken) revealTokenField();
    cooldown = info.wait || 0;
  } finally {
    state.abort = null;
    busy(false);
    if (cooldown) startCooldown(cooldown);
  }
}

/* ---------------- 渲染 ---------------- */
function showPlan(plan, meta) {
  state.plan = plan;
  const box = $("result");
  box.textContent = "";
  box.hidden = false;

  if (meta.demo) add(box, "span", "demo", "示例数据 · 未调用模型，可直接试下载");
  add(box, "h2", null, plan.destination + " · " + plan.days + " 天");

  const pills = add(box, "ul", "metabar");
  const minutes = plan.plan.reduce((sum, day) => sum + day.spots.reduce((s, x) => s + x.minutes, 0), 0);
  [["accent", "人均 " + Math.round(plan.budget_total_yuan) + " 元"],
   ["", plan.days + " 天 / " + plan.plan.length + " 段"],
   ["", "游玩合计 " + (minutes / 60).toFixed(1) + " 小时"],
   ["", "不含住宿与往返大交通"]].forEach(([cls, text]) => add(pills, "li", "pill" + (cls ? " " + cls : ""), text));

  add(box, "p", "lede", "需求复述：" + plan.traveler_profile);
  if (plan.best_season_note) add(box, "p", "note-soft", "出行提示：" + plan.best_season_note);
  if (plan.summary) add(box, "p", "lede", "结论：" + plan.summary);

  const start = $("start").value ? new Date($("start").value + "T00:00:00") : null;
  plan.plan.forEach((day) => {
    const wrap = add(box, "div", "day");
    const head = add(wrap, "div", "day-head");
    add(head, "h3", null, "第 " + day.day + " 天 · " + day.theme);
    if (start) add(head, "span", "date", new Date(start.getTime() + (day.day - 1) * 864e5).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit", weekday: "short" }));
    const total = day.spots.reduce((sum, spot) => sum + spot.minutes, 0);
    add(wrap, "p", "day-sub", day.base_area + " · 游玩 " + (total / 60).toFixed(1) + " 小时 · 当天人均 " + Math.round(day.day_cost_yuan) + " 元 · " + day.spots.length + " 个游玩点");

    const rail = add(wrap, "div", "rail");
    slots(day.spots).forEach(({ lunch, spot, begin, end }) => {
      if (lunch) row(rail, "spot lunch", clock(begin) + "–" + clock(end), "午餐与休息", "预留 60 分钟，按实际节奏可缩短", "");
      else row(rail, "spot", clock(begin) + "–" + clock(end), spot.name, spot.why, spot.ticket_yuan ? Math.round(spot.ticket_yuan) + " 元" : "免费");
    });
    if (day.meals && day.meals.length) add(wrap, "p", "dayline", "吃：" + day.meals.join(" · "));
    add(wrap, "p", "dayline", "行：" + day.transport);
  });

  if (plan.risks && plan.risks.length) list(box, "需要注意", plan.risks);
  if (plan.references && plan.references.length) list(box, "参考来源", plan.references, true);

  const actions = add(box, "div", "actions");
  const md = add(actions, "button", "primary", "下载 Markdown");
  const copy = add(actions, "button", "ghost", "复制 Markdown");
  const ics = add(actions, "button", "ghost", "下载 .ics 日历");
  md.type = copy.type = ics.type = "button";
  md.addEventListener("click", () => download(["markdown"]));
  copy.addEventListener("click", () => copyMarkdown());
  ics.addEventListener("click", () => download(["ics"]));
  if (meta.ms) {
    const cost = meta.cost == null ? "" : " · 约 ¥" + Number(meta.cost).toFixed(4);
    add(box, "p", "note-soft", "本轮 " + meta.tokens + " token / " + (meta.ms / 1000).toFixed(1) + " 秒" + cost + " · 会话 " + meta.session);
  }
  if (meta.trace && meta.trace.length) {
    const ul = add(box, "ul", "trace");
    if (meta.final_ms != null) meta.trace.push({ name: "生成结构化方案", ms: meta.final_ms, hits: 0, chars: 0 });
    meta.trace.forEach((step) => {
      const li = add(ul, "li");
      add(li, "b", null, step.name);
      add(li, "span", null, (step.ms != null ? step.ms + " ms" : "—") + " · 命中 " + step.hits + " 条 · " + step.chars + " 字符");
    });
  }
}

function row(parent, className, when, title, why, fee) {
  const item = add(parent, "div", className);
  add(item, "span", "when", when);
  const what = add(item, "div", "what");
  add(what, "b", null, title);
  if (why) add(what, "p", null, why);
  add(item, "span", "fee" + (fee === "免费" ? " free" : ""), fee);
  return item;
}

function list(parent, title, items, links) {
  const block = add(parent, "div", "block");
  add(block, "h4", null, title);
  const ul = add(block, "ul");
  items.forEach((text) => {
    const li = add(ul, "li");
    if (links && /^https?:\/\//i.test(text)) {
      const a = document.createElement("a");
      a.href = text; a.target = "_blank"; a.rel = "noreferrer"; a.textContent = text;
      li.appendChild(a);
    } else { li.textContent = text; }
  });
}

/* ---------------- 导出 ---------------- */
async function download(formats) {
  if (!state.plan) return;
  status("正在生成文件…");
  try {
    const data = await post("/export", { plan: state.plan, start_date: $("start").value || null, formats });
    data.files.forEach((file) => {
      const url = URL.createObjectURL(new Blob([file.content], { type: "text/plain;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = url; link.download = file.name; link.click();
      URL.revokeObjectURL(url);
    });
    status("已导出 " + data.files.map((f) => f.name).join("、") + (data.start_date_guessed ? "（起始日按 " + data.start_date + " 推测）" : ""), "ok");
  } catch (err) { status("导出失败：" + err.message, "error"); }
}

/* 复制导出内容：演示时不用再去文件夹里找文件，直接粘进文档或聊天 */
async function copyMarkdown() {
  if (!state.plan) return;
  status("正在生成 Markdown…");
  try {
    const data = await post("/export", { plan: state.plan, start_date: $("start").value || null, formats: ["markdown"] });
    const text = data.files[0].content;
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      // 非安全上下文（比如用局域网 IP 打开）没有 clipboard API，退回临时 textarea
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    status("Markdown 已复制到剪贴板（" + text.length + " 字符）。", "ok");
  } catch (err) {
    status("复制失败：" + err.message, "error");
  }
}

/* ---------------- 历史会话 ---------------- */
function pushHistory(plan, session, tokens) {
  const minutes = plan.plan.reduce((sum, day) => sum + day.spots.reduce((s, x) => s + x.minutes, 0), 0);
  const entry = { session, plan, title: plan.destination + " · " + plan.days + " 天",
    stamp: new Date().toLocaleString("zh-CN", { hour12: false }), tokens, hours: (minutes / 60).toFixed(1) };
  state.history = [entry, ...state.history.filter((item) => item.session !== session)].slice(0, 20);
  store(HISTORY_KEY, state.history);
  renderHistory();
}
function renderHistory() {
  const ul = $("historyList");
  ul.textContent = "";
  const valid = state.history.filter((item) => item && item.plan && Array.isArray(item.plan.plan));
  $("historyEmpty").hidden = valid.length > 0;
  valid.forEach((item) => {
    const li = add(ul, "li");
    const main = add(li, "div", "hmain");
    add(main, "b", null, item.title);
    add(main, "span", null, item.stamp + " · 游玩合计 " + item.hours + " 小时 · " + item.tokens + " token");
    const open = add(li, "button", "link", "打开");
    open.type = "button";
    open.addEventListener("click", () => {
      state.session = item.session;
      showPlan(item.plan, { session: item.session, demo: false });
      status("已载入历史会话，勾上「继续改」就能在它上面追加要求。", "ok");
    });
  });
}
$("clearHistory").addEventListener("click", () => { state.history = []; store(HISTORY_KEY, []); renderHistory(); });

/* ---------------- 图片输入 ---------------- */
function setImage(file) {
  if (!file) return;
  if (!/^image\/(png|jpeg|webp|gif)$/.test(file.type)) { status("只支持 PNG / JPEG / WebP / GIF。", "error"); return; }
  if (file.size > MAX_IMAGE) { status("图片 " + (file.size / 1048576).toFixed(1) + " MB，超过 8 MB 上限。", "error"); return; }
  const reader = new FileReader();
  reader.onload = () => {
    state.image = reader.result;
    $("thumb").src = reader.result;
    $("thumbBox").hidden = false;
    $("dropText").hidden = true;
    $("clearImage").hidden = false;
    status("");
  };
  reader.onerror = () => status("读图失败，换一张试试。", "error");
  reader.readAsDataURL(file);
}
function clearImage() {
  state.image = null;
  $("image").value = "";
  $("thumbBox").hidden = true;
  $("dropText").hidden = false;
  $("clearImage").hidden = true;
}
$("image").addEventListener("change", (event) => setImage(event.target.files[0]));
$("clearImage").addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); clearImage(); });
const drop = $("drop");
["dragover", "dragenter"].forEach((type) => drop.addEventListener(type, (event) => { event.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((type) => drop.addEventListener(type, (event) => { event.preventDefault(); drop.classList.remove("over"); if (type === "drop") setImage(event.dataTransfer.files[0]); }));

/* ---------------- 绑定 ---------------- */
$("go").addEventListener("click", generate);
if ($("cancel")) $("cancel").addEventListener("click", () => { if (state.abort) state.abort.abort(); });
if (!new URLSearchParams(location.search).has("admin") && $("tokenField")) $("tokenField").hidden = true;
$("sample").addEventListener("click", () => {
  const plan = JSON.parse($("sample-plan").textContent);
  state.session = null;
  $("follow").checked = false;
  showPlan(plan, { demo: true });
  status("已载入示例行程（本机内置数据，不调模型）。", "ok");
});
$("chips").addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (chip) { $("request").value = chip.dataset.fill; $("request").focus(); }
});
$("request").addEventListener("keydown", (event) => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) generate(); });

$("start").value = ymd(new Date(Date.now() + 864e5));
$("start").min = ymd(new Date());
state.history = read(HISTORY_KEY, []).filter((item) => item && item.plan);
renderHistory();
fetch("/health").then((r) => r.json()).then((data) => {
  state.model = data.model || "";
  if (data.status === "ok") {
    $("modelTag").textContent = "模型 " + data.model + " · 就绪";
    state.visualOk = /^qwen/i.test(data.model);
    applyImageCapability();
  } else {
    $("modelTag").textContent = "配置缺失：" + data.detail;
    status(data.detail, "error");
  }
}).catch(() => { $("modelTag").textContent = "服务未响应"; });

// URL 参数必须最后处理：?demo=1 会立刻渲染行程，早于出发日期初始化就显示不出日历日
const forced = new URLSearchParams(location.search).get("theme");
if (forced === "dark" || forced === "light") applyTheme(forced);
if (new URLSearchParams(location.search).has("demo")) $("sample").click();
