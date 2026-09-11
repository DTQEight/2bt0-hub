// 2bt0 资源库前端：电影 / 电视剧 / 本地磁力库 / 同步 / 日志

const state = {
  tab: "movie", // movie | tv | local | sync | logs
  page: 1,
  q: "",
  prevRunning: false,
};

const TABS = {
  movie: { source: "bt0", sc: 1, hint: "留空浏览最新电影种子（自带磁力）；输入片名搜索影片库…" },
  tv: { source: "bt0", sc: 2, hint: "留空浏览最新电视剧种子（自带磁力）；输入片名搜索影片库…" },
  local: { source: "local", sc: 0, hint: "搜索本地库已保存的磁力（标题 / hash / 分类），留空浏览全部…" },
  sync: {},
  logs: {},
};

const SECTIONS = { 1: "电影", 2: "电视剧" };

const el = {};
for (const id of ["tabs", "q", "search-form", "status", "list", "pager",
  "list-view", "sync-view", "sync-grid", "schedule-card", "logs-view", "log-box",
  "log-scroll", "log-refresh", "sync-badge", "sync-badge-text", "sync-stop", "toast"]) {
  el[id] = document.getElementById(id);
}

let syncTimer = null;
let logTimer = null;

// ---- 通用 ----

function showToast(message) {
  el.toast.textContent = message;
  el.toast.classList.add("show");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => el.toast.classList.remove("show"), 3200);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function copyText(text) {
  const done = () => showToast("已复制到剪贴板");
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); done(); } catch { showToast("复制失败，请手动复制"); }
  ta.remove();
}

// ---- Tab 切换 ----

function switchTab(tab) {
  state.tab = tab;
  state.page = 1;
  state.q = "";
  el.q.value = "";
  for (const btn of el.tabs.querySelectorAll("button")) {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  }
  const isList = ["movie", "tv", "local"].includes(tab);
  el["list-view"].hidden = !isList;
  el["sync-view"].hidden = tab !== "sync";
  el["logs-view"].hidden = tab !== "logs";
  if (tab === "logs") {
    stopSyncPolling();
    startLogPolling();
    loadLogs();
  } else {
    stopLogPolling();
    if (tab === "sync") {
      refreshSyncUI(false); // 立即渲染 + 启动轮询
      loadSchedule();
    } else {
      applySearchbar();
      load();
      refreshSyncUI(false); // 驱动顶栏同步徽标
    }
  }
}

function applySearchbar() {
  const t = TABS[state.tab];
  el.q.placeholder = t.hint || "搜索…";
}

// ---- 列表加载 ----

async function load() {
  const t = TABS[state.tab];
  if (!t.source) return;
  const params = new URLSearchParams({ source: t.source, page: state.page });
  if (t.sc) params.set("sc", t.sc);
  if (state.q) params.set("q", state.q);

  el.status.className = "status";
  el.status.textContent = "加载中…";
  el.list.replaceChildren();
  el.pager.replaceChildren();

  try {
    const res = await fetch(`/api/items?${params}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    renderList(data);
  } catch (err) {
    el.status.className = "status error";
    el.status.textContent = `加载失败：${err.message}`;
  }
}

function renderList(data) {
  const items = data.items || [];
  const searching = !!state.q;
  const prefix = searching
    ? `搜索“${state.q}”命中 ${data.total_items} 条 · `
    : (state.tab === "local" ? `本地库共 ${data.total_items} 条 · ` : "");
  el.status.textContent = `${prefix}第 ${data.page} / ${data.total_pages} 页`;

  if (!items.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = state.tab === "local"
      ? "本地库为空，去电影 / 电视剧页浏览或点击“同步到本地库”"
      : "没有匹配的结果";
    el.list.appendChild(li);
    return;
  }

  for (const item of items) el.list.appendChild(buildCard(item));
  buildPager(data.page, data.total_pages);
}

function buildCard(item) {
  const li = document.createElement("li");
  li.className = "card";

  const meta = [item.size, item.published_at, item.category]
    .filter(Boolean).map(escapeHtml).join('<span class="sep">·</span>');

  const actions = [];
  if (item.magnet) {
    const hash = (item.extra?.info_hash) || (item.magnet.match(/btih:([0-9a-fA-F]+)/)?.[1] || "");
    actions.push(`<button type="button" class="act copy" data-magnet="${escapeHtml(item.magnet)}">复制磁力</button>`);
    actions.push(`<a class="act" href="${escapeHtml(item.magnet)}">打开</a>`);
    if (hash) actions.push(`<span class="hash" title="info_hash">${escapeHtml(hash.slice(0, 16))}…</span>`);
  } else if (item.detail_url) {
    actions.push(`<a class="act" href="${escapeHtml(item.detail_url)}" target="_blank" rel="noopener noreferrer">官网详情 ↗</a>`);
  }

  li.innerHTML = `
    <div class="card-main">
      <div class="card-title" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</div>
      <div class="card-meta">${meta}</div>
    </div>
    <div class="card-actions">${actions.join("")}</div>`;

  const copyBtn = li.querySelector("button.copy");
  if (copyBtn) copyBtn.addEventListener("click", () => copyText(copyBtn.dataset.magnet));
  return li;
}

function buildPager(page, totalPages) {
  el.pager.replaceChildren();
  if (totalPages <= 1) return;

  const add = (label, target, opts = {}) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    if (opts.current) btn.setAttribute("aria-current", "true");
    if (opts.disabled) btn.disabled = true;
    btn.addEventListener("click", () => {
      state.page = target;
      load();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    el.pager.appendChild(btn);
  };

  add("‹ 上一页", page - 1, { disabled: page <= 1 });

  // 页码窗口：始终含首尾与当前页附近
  const win = new Set([1, totalPages, page - 1, page, page + 1]);
  const sorted = [...win].filter((p) => p >= 1 && p <= totalPages).sort((a, b) => a - b);
  let last = 0;
  for (const p of sorted) {
    if (p - last > 1) {
      const gap = document.createElement("span");
      gap.className = "gap";
      gap.textContent = "…";
      el.pager.appendChild(gap);
    }
    add(String(p), p, { current: p === page });
    last = p;
  }

  add("下一页 ›", page + 1, { disabled: page >= totalPages });
}

// ---- 同步管理页（全量 / 增量 / ETA / 统计） ----

function fmtEta(sec) {
  if (sec == null) return "计算中…";
  if (sec < 90) return "约 1 分钟";
  if (sec < 3600) return `约 ${Math.round(sec / 60)} 分钟`;
  const h = Math.floor(sec / 3600);
  const m = Math.round((sec % 3600) / 60);
  return `约 ${h} 小时 ${m} 分`;
}

function fmtNum(n) {
  return Number(n || 0).toLocaleString("zh-CN");
}

// 单个板块卡片
function syncCardHtml(sc, s) {
  const label = SECTIONS[sc];
  const prog = s.progress && s.progress[String(sc)];
  const isDone = (s.done || []).includes(sc);
  const isRunning = s.running && s.section === sc;
  const catCount = (s.by_category || {})[label] || 0;

  // 状态徽标
  let badge, badgeCls;
  if (isRunning) {
    badge = { full: "全量同步中", resume: "断点续抓中", update: "增量更新中" }[s.mode] || "同步中";
    badgeCls = "run";
  } else if (isDone) {
    badge = "已全量同步"; badgeCls = "done";
  } else if (prog) {
    badge = `已抓到第 ${fmtNum(prog)} 页（未完成）`; badgeCls = "pause";
  } else {
    badge = "未同步"; badgeCls = "none";
  }

  // 进度条（同步中用实时页码，否则用断点）
  const curPage = isRunning ? s.page : (prog || 0);
  const total = isRunning ? s.total_pages : 0;
  const pct = isRunning && total > 0
    ? Math.min(100, (curPage / total) * 100).toFixed(1) : null;
  const progressHtml = isRunning
    ? (total > 0
        ? `<div class="progress"><div class="progress-bar" style="width:${pct}%"></div></div>
           <div class="progress-num">${pct}% · 第 ${fmtNum(curPage)} / ${fmtNum(total)} 页</div>`
        : `<div class="progress"><div class="progress-bar indeterminate"></div></div>
           <div class="progress-num">正在探测板块总页数…</div>`)
    : "";

  // 运行时数据行
  const runRows = isRunning
    ? (s.mode === "update"
        ? `<div class="sync-row"><span>进度</span><span>第 ${fmtNum(s.page)} 页 · 已抓 ${fmtNum(s.fetched)} 条</span></div>`
        : `<div class="sync-row"><span>本次已抓</span><span>${fmtNum(s.fetched)} 条</span></div>
           <div class="sync-row"><span>速度</span><span>${s.speed_ppm > 0 ? `${s.speed_ppm} 页/分钟` : "采样中…"}</span></div>
           <div class="sync-row"><span>预计剩余</span><span class="eta">${fmtEta(s.eta_seconds)}</span></div>`)
    : "";

  // 静态数据行
  const rows = `
    <div class="sync-row"><span>库内${label}</span><span>${fmtNum(catCount)} 条</span></div>
    ${prog ? `<div class="sync-row"><span>断点</span><span>第 ${fmtNum(prog)} 页（可继续）</span></div>` : ""}
    ${isDone ? `<div class="sync-row"><span>状态</span><span>可增量更新追新</span></div>` : ""}
  `;

  // 按钮组：全量 / 增量分开
  const fullBtnLabel = isRunning ? "同步进行中…"
    : prog ? `继续全量同步（第 ${fmtNum(prog + 1)} 页起）` : "开始全量同步";
  const fullDisabled = isRunning ? "disabled" : "";
  const updateDisabled = isRunning || !isDone
    ? `disabled title="${isRunning ? "同步进行中" : "完成全量同步后可用"}"` : "";
  const stopBtn = isRunning ? `<button type="button" class="stop" data-stop="1">停止</button>` : "";

  return `
    <div class="sync-card ${isRunning ? "running" : ""}">
      <div class="sync-card-head">
        <h3>${label}</h3>
        <span class="sbadge ${badgeCls}">${badge}</span>
      </div>
      ${progressHtml}
      <div class="sync-rows">${runRows}${rows}</div>
      <div class="sync-actions">
        <button type="button" class="primary" data-full="${sc}" ${fullDisabled}>${fullBtnLabel}</button>
        <button type="button" data-update="${sc}" ${updateDisabled}>增量更新</button>
        ${stopBtn}
      </div>
    </div>`;
}

// 库统计卡片
function statsCardHtml(s) {
  const cats = s.by_category || {};
  const other = Object.entries(cats)
    .filter(([k]) => k !== SECTIONS[1] && k !== SECTIONS[2])
    .map(([k, v]) => `${k} ${fmtNum(v)}`).join(" · ");
  return `
    <div class="sync-card stats">
      <div class="sync-card-head"><h3>本地数据库</h3><span class="sbadge done">SQLite</span></div>
      <div class="sync-rows">
        <div class="sync-row"><span>库内总数</span><span class="big">${fmtNum(s.db_total)} 条</span></div>
        <div class="sync-row"><span>电影</span><span>${fmtNum(cats[SECTIONS[1]] || 0)} 条</span></div>
        <div class="sync-row"><span>电视剧</span><span>${fmtNum(cats[SECTIONS[2]] || 0)} 条</span></div>
        ${other ? `<div class="sync-row"><span>其他</span><span>${other}</span></div>` : ""}
        <div class="sync-row"><span>数据库大小</span><span>${s.db_size_mb || 0} MB</span></div>
        <div class="sync-row"><span>最近入库</span><span>${escapeHtml(s.last_seen || "—")}</span></div>
      </div>
      <div class="stats-hint">浏览 / 搜索 / 同步获得的磁力都会自动入库，按 info_hash 去重</div>
    </div>`;
}

// 每日定时任务卡片
function scheduleCardHtml(cfg) {
  const on = !!cfg.enabled;
  const opts = Array.from({ length: 24 }, (_, h) =>
    `<option value="${h}"${h === cfg.hour ? " selected" : ""}>${String(h).padStart(2, "0")}:00</option>`
  ).join("");
  return `
    <div class="sync-card">
      <div class="sync-card-head">
        <h3>每日增量更新</h3>
        <span class="sbadge ${on ? "run" : "none"}">${on ? "已开启" : "已关闭"}</span>
      </div>
      <div class="sync-rows">
        <div class="sync-row"><span>执行内容</span><span>电影 → 电视剧（依次）</span></div>
        <div class="sync-row"><span>每天时间</span><span><select class="sched-select" data-sched="hour">${opts}</select></span></div>
        <div class="sync-row"><span>下次执行</span><span class="${on ? "eta" : ""}">${
          on ? escapeHtml(cfg.next_run || "—") : "已关闭"}</span></div>
      </div>
      <div class="sync-actions">
        <button type="button" data-sched="toggle">${on ? "关闭定时任务" : "开启定时任务"}</button>
      </div>
      <div class="stats-hint">到点自动依次对两个板块跑增量更新（只扫前 10 页追新）。若当时已有同步在进行，当天自动跳过。</div>
    </div>`;
}

async function loadSchedule() {
  let cfg;
  try {
    const res = await fetch("/api/schedule");
    cfg = await res.json();
    if (!res.ok) throw new Error(cfg.detail || `HTTP ${res.status}`);
  } catch { return; }
  renderSchedule(cfg);
}

function renderSchedule(cfg) {
  el["schedule-card"].innerHTML = scheduleCardHtml(cfg);
  el["schedule-card"].querySelector('[data-sched="toggle"]').addEventListener(
    "click", () => saveSchedule({ enabled: !cfg.enabled }));
  el["schedule-card"].querySelector('[data-sched="hour"]').addEventListener(
    "change", (e) => saveSchedule({ hour: Number(e.target.value) }));
}

async function saveSchedule(patch) {
  try {
    const res = await fetch("/api/schedule", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    const cfg = await res.json();
    if (!res.ok) throw new Error(cfg.detail || `HTTP ${res.status}`);
    renderSchedule(cfg);
    showToast(cfg.enabled
      ? `定时任务已开启：每天 ${String(cfg.hour).padStart(2, "0")}:00 自动增量更新`
      : "定时任务已关闭");
  } catch (err) {
    showToast(`保存失败：${err.message}`);
    loadSchedule(); // 失败时回到服务端真实状态
  }
}

function renderSyncPage(s) {
  el["sync-grid"].innerHTML =
    syncCardHtml(1, s) + syncCardHtml(2, s) + statsCardHtml(s);
  // 绑定按钮事件（innerHTML 重建后需重绑）
  for (const btn of el["sync-grid"].querySelectorAll("[data-full]")) {
    btn.addEventListener("click", () => startSync(Number(btn.dataset.full)));
  }
  for (const btn of el["sync-grid"].querySelectorAll("[data-update]")) {
    btn.addEventListener("click", () => startSync(Number(btn.dataset.update), "update"));
  }
  for (const btn of el["sync-grid"].querySelectorAll("[data-stop]")) {
    btn.addEventListener("click", stopSync);
  }
}

async function startSync(section, mode) {
  try {
    const res = await fetch("/api/sync/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ section, ...(mode ? { mode } : {}) }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    const msg = data.mode === "update" ? "增量更新已启动（很快结束）"
      : data.mode === "resume" ? `已从第 ${fmtNum(data.page)} 页继续同步`
      : "全量同步已启动，后台进行中（可随时停止）";
    showToast(msg);
  } catch (err) {
    showToast(`启动失败：${err.message}`);
  }
  refreshSyncUI(false);
}

async function stopSync() {
  try {
    await fetch("/api/sync/stop", { method: "POST" });
    showToast("正在停止同步…");
  } catch { /* 忽略 */ }
}

async function refreshSyncUI(notifyDone) {
  let s;
  try {
    const res = await fetch("/api/sync/status");
    s = await res.json();
  } catch { return; }

  // 顶栏徽标（任何 tab 下都显示）
  if (s.running) {
    el["sync-badge"].hidden = false;
    const modeText = s.mode === "update" ? "增量更新"
      : s.mode === "resume" ? "断点续抓" : "全量同步";
    const etaText = s.mode !== "update" && s.eta_seconds != null
      ? ` · 剩余 ${fmtEta(s.eta_seconds)}` : "";
    el["sync-badge-text"].textContent =
      `${modeText} · ${s.section_label || `板块${s.section}`} 第 ${fmtNum(s.page)} 页 · 库内 ${fmtNum(s.db_total)}${etaText}`;
    if (!syncTimer) syncTimer = setInterval(() => refreshSyncUI(true), 3000);
  } else {
    el["sync-badge"].hidden = true;
    if (syncTimer) { clearInterval(syncTimer); syncTimer = null; }
    if (notifyDone && state.prevRunning) {
      showToast(s.message
        ? `同步结束：${s.message}，库内共 ${fmtNum(s.db_total)} 条`
        : `同步结束，本地库共 ${fmtNum(s.db_total)} 条`);
    }
  }

  // 同步管理页内容（仅在该 tab 下渲染，避免多余 DOM 操作）
  if (state.tab === "sync") {
    renderSyncPage(s);
    if (s.running && !syncTimer) {
      syncTimer = setInterval(() => refreshSyncUI(true), 3000);
    }
  }
  state.prevRunning = s.running;
}

function stopSyncPolling() {
  if (syncTimer) { clearInterval(syncTimer); syncTimer = null; }
}

// ---- 日志 ----

async function loadLogs() {
  try {
    const res = await fetch("/api/logs?lines=300");
    const data = await res.json();
    renderLogs(data.lines || []);
  } catch { /* 下个周期重试 */ }
}

function renderLogs(lines) {
  const html = lines.map((line) => {
    const esc = escapeHtml(line);
    if (/\bERROR\b/.test(line)) return `<span class="lv-error">${esc}</span>`;
    if (/\bWARNING\b/.test(line)) return `<span class="lv-warn">${esc}</span>`;
    return esc;
  }).join("\n");
  el["log-box"].innerHTML = html;
  if (el["log-scroll"].checked) {
    el["log-box"].scrollTop = el["log-box"].scrollHeight;
  }
}

function startLogPolling() {
  if (!logTimer) logTimer = setInterval(loadLogs, 5000);
}

function stopLogPolling() {
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
}

// ---- 事件绑定 ----

el.tabs.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (btn) switchTab(btn.dataset.tab);
});

el["search-form"].addEventListener("submit", (e) => {
  e.preventDefault();
  state.q = el.q.value.trim();
  state.page = 1;
  load();
});

el["sync-stop"].addEventListener("click", stopSync);

el["log-refresh"].addEventListener("click", loadLogs);

// 初始化
switchTab("movie");
refreshSyncUI(false); // 驱动顶栏同步徽标
