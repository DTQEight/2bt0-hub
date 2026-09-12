// 2bt0 资源库前端：电影 / 电视剧 / 本地磁力库 / 同步 / 日志

const state = {
  tab: "movie", // movie | tv | local | sync | logs
  page: 1,
  q: "",
  localCategory: "电影", // 本地库默认只看电影，可切电视剧 / 全部
  localView: "items", // items=全部版本 | groups=按片名分组
  localSort: "last", // 海报墙排序：last=最新入库 | score=豆瓣评分 | years=年份 | versions=版本数
  movieId: "", // 非空＝正在看某部影片的全部版本
  movieTitle: "", // 该影片片名（分组接口带回，用于详情未拉取时兜底显示）
  prevRunning: false,
};

const TABS = {
  movie: { source: "bt0", sc: 1, hint: "搜片名，留空浏览最新电影种子…" },
  tv: { source: "bt0", sc: 2, hint: "搜片名，留空浏览最新电视剧种子…" },
  local: { source: "local", sc: 0, hint: "搜片名/原名/别名/演员/导演/hash…" },
  sync: {},
  logs: {},
};

const SECTIONS = { 1: "电影", 2: "电视剧" };

const el = {};
for (const id of ["tabs", "q", "search-form", "status", "list", "pager",
  "list-view", "cat-filter", "view-toggle", "sort-select", "sort-by",
  "movie-head", "sync-view", "sync-grid",
  "schedule-card", "logs-view", "log-box", "log-scroll", "log-refresh",
  "sync-badge", "sync-badge-text", "sync-stop", "toast"]) {
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
  state.movieId = "";
  el.q.value = "";
  for (const btn of el.tabs.querySelectorAll("button")) {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  }
  const isList = ["movie", "tv", "local"].includes(tab);
  el["search-form"].hidden = !isList; // 搜索栏嵌在顶栏，列表页才显示
  el["list-view"].hidden = !isList;
  el["sync-view"].hidden = tab !== "sync";
  el["logs-view"].hidden = tab !== "logs";
  applyCatFilter();
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

// 分类筛选器、排序、视图开关都只在「本地磁力库」页出现
function applyCatFilter() {
  const isLocal = state.tab === "local";
  el["cat-filter"].hidden = !isLocal;
  el["view-toggle"].hidden = !isLocal;
  // 排序只对海报墙有意义（看某片版本时不显示）
  el["sort-select"].hidden =
    !(isLocal && state.localView === "groups" && !state.movieId);
  if (!isLocal) {
    el["movie-head"].replaceChildren();
    return;
  }
  for (const btn of el["cat-filter"].querySelectorAll("button")) {
    btn.classList.toggle("active", btn.dataset.cat === state.localCategory);
  }
  for (const btn of el["view-toggle"].querySelectorAll("button")) {
    // 看某部影片的版本时两个视图都不选中
    btn.classList.toggle("active",
      !state.movieId && btn.dataset.view === state.localView);
  }
}

// ---- 列表加载 ----

let loadToken = 0; // 递增令牌：快速翻页/搜索时丢弃过期响应，避免慢请求覆盖新结果

async function load() {
  const token = ++loadToken;
  const t = TABS[state.tab];
  if (!t.source) return;
  // 本地库「按片名」视图是海报墙，走独立接口（分组结果不是磁力条目）
  const posterWall = state.tab === "local" && state.localView === "groups" && !state.movieId;
  el.list.classList.toggle("poster-grid", posterWall);
  if (posterWall) {
    return loadGroups(token);
  }
  const params = new URLSearchParams({ source: t.source, page: state.page });
  if (t.sc) params.set("sc", t.sc);
  if (state.q) params.set("q", state.q);
  if (state.tab === "local" && state.localCategory) params.set("category", state.localCategory);
  if (state.tab === "local" && state.movieId) params.set("movie_id", state.movieId);

  el.status.className = "status";
  el.status.textContent = "加载中…";
  el.list.replaceChildren();
  el.pager.replaceChildren();
  renderMovieHead(); // 看某片版本时先渲染影片信息卡

  try {
    const res = await fetch(`/api/items?${params}`);
    const data = await res.json();
    if (token !== loadToken) return; // 已被更新的请求取代
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    renderList(data);
  } catch (err) {
    if (token !== loadToken) return;
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
    // 仅在"无筛选且无关键词"时才提示库为空，否则只是当前条件没命中
    const filtered = state.tab === "local" && (state.q || state.localCategory);
    li.textContent = state.tab === "local" && !filtered
      ? "本地库为空。在电影 / 电视剧页浏览会自动入库，也可在「同步」页启动全量同步"
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
    // 只对 magnet: 协议渲染直开链接，防止数据里混入 javascript: 等伪协议
    if (/^magnet:/i.test(item.magnet)) {
      actions.push(`<a class="act" href="${escapeHtml(item.magnet)}">打开</a>`);
    }
    if (hash) actions.push(`<span class="hash" title="info_hash">${escapeHtml(hash.slice(0, 16))}…</span>`);
  } else if (item.detail_url && /^https?:\/\//i.test(item.detail_url)) {
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

// ---- 本地库「按片名」分组视图 ----

async function loadGroups(token) {
  const params = new URLSearchParams({ page: state.page, sort: state.localSort });
  if (state.q) params.set("q", state.q);
  if (state.localCategory) params.set("category", state.localCategory);

  el.status.className = "status";
  el.status.textContent = "加载中…";
  el["movie-head"].replaceChildren();
  el.list.replaceChildren();
  el.pager.replaceChildren();

  try {
    const res = await fetch(`/api/groups?${params}`);
    const data = await res.json();
    if (token !== loadToken) return; // 已被更新的请求取代
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    renderGroups(data);
  } catch (err) {
    if (token !== loadToken) return;
    el.status.className = "status error";
    el.status.textContent = `加载失败：${err.message}`;
  }
}

function renderGroups(data) {
  const groups = data.groups || [];
  const prefix = state.q
    ? `搜索“${state.q}”命中 ${fmtNum(data.total_groups)} 部 · `
    : `共 ${fmtNum(data.total_groups)} 部影片 · `;
  el.status.textContent = `${prefix}第 ${data.page} / ${data.total_pages} 页`;

  if (!groups.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = state.q ? "没有匹配的影片" : "本地库还没有影片数据（影片信息随同步一起入库）";
    el.list.appendChild(li);
    return;
  }
  for (const g of groups) el.list.appendChild(buildGroupCard(g));
  buildPager(data.page, data.total_pages);
}

function buildGroupCard(g) {
  const li = document.createElement("li");
  li.className = "poster-card";
  const title = g.title || g.movie_title || `影片 ${g.movie_id}`;
  // 站点用 0 / @ 表示"暂无评分"，不显示角标
  const score = g.doub_score && !["0", "@"].includes(g.doub_score) ? g.doub_score : "";
  // 海报图床是外链，加载失败只是留空，不影响其它信息
  const poster = g.image && /^https?:\/\//i.test(g.image)
    ? `<img class="pc-img" src="${escapeHtml(g.image)}" alt="${escapeHtml(title)}"
             loading="lazy" referrerpolicy="no-referrer">`
    : `<div class="pc-noimg">${escapeHtml(title)}</div>`;
  const meta = [g.years, `${fmtNum(g.versions)} 个版本`].filter(Boolean).join(" · ");

  li.innerHTML = `
    <div class="pc-poster">
      ${poster}
      ${score ? `<span class="pc-score">${escapeHtml(score)}</span>` : ""}
    </div>
    <div class="pc-title" title="${escapeHtml(title)}">${escapeHtml(title)}</div>
    <div class="pc-meta">${escapeHtml(meta)}</div>`;
  li.addEventListener("click", () => openMovie(g.movie_id, title));
  return li;
}

// 进入某部影片：切到该片的版本列表，并展示影片详情
function openMovie(id, title) {
  state.movieId = id;
  state.movieTitle = title || "";
  state.page = 1;
  state.q = "";
  el.q.value = "";
  applyCatFilter();
  load();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function backToGroups() {
  state.movieId = "";
  state.movieTitle = "";
  state.page = 1;
  state.localView = "groups";
  applyCatFilter();
  load();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// 影片信息卡：详情来自站点 getVideoDetail，由「同步」页的批量任务拉取后缓存在本地
async function renderMovieHead() {
  const head = el["movie-head"];
  if (state.tab !== "local" || !state.movieId) {
    head.replaceChildren();
    return;
  }
  const id = state.movieId;
  head.innerHTML = `<div class="movie-head">
      <div class="mh-poster-wrap"><div class="mh-poster mh-noposter">加载中…</div></div>
      <div class="mh-info">
        <div class="mh-headline">
          <div class="mh-title">${escapeHtml(state.movieTitle || `影片 ${id}`)}</div>
          <button type="button" class="act" data-back="1">← 返回片名列表</button>
        </div>
        <div class="mh-otitle">正在加载影片信息…</div>
      </div>
    </div>`;
  head.querySelector("[data-back]").addEventListener("click", backToGroups);

  let movie = null;
  try {
    const res = await fetch(`/api/movie/${encodeURIComponent(id)}`);
    movie = (await res.json()).movie;
  } catch { /* 取不到就按"无详情"渲染 */ }
  if (state.movieId !== id) return; // 期间已切走，别覆盖新内容

  head.innerHTML = movie ? movieHeadHtml(movie) : movieHeadEmptyHtml();
  head.querySelector("[data-back]").addEventListener("click", backToGroups);
}

// 评价人数格式化：649314 → "64.9万人评价"，999 → "999人评价"
function fmtVotes(v) {
  const n = Number(v);
  if (!n || Number.isNaN(n)) return "";
  return n >= 10000 ? `${(n / 10000).toFixed(1)}万人评价` : `${n}人评价`;
}

// 影片信息卡：布局参考主站 2bt0.com 详情页（海报 + 标题 + 元数据 + 评分胶囊 + 剧情简介）
function movieHeadHtml(m) {
  const title = m.title || state.movieTitle || "（无标题）";
  const years = (m.years || "").trim();
  const otitle = m.otitle
    ? `<div class="mh-otitle">${escapeHtml(m.otitle)}${m.alias ? `　/　又名：${escapeHtml(m.alias)}` : ""}</div>`
    : (m.alias ? `<div class="mh-otitle">又名：${escapeHtml(m.alias)}</div>` : "");

  // 元数据：标签独立成行、值在下，与主站 .meta 一致（"0"/"@" 是站点占位符，不展示）
  const meta = [
    ["导演", m.director],
    ["主演", m.performer],
    ["类型", m.category],
    ["制片国家/地区", m.area],
    ["语言", m.language],
    ["上映日期", m.years],
    ["片长", m.long_time],
    ["集数", m.episodes],
  ]
    .filter(([, v]) => v && !["0", "@"].includes(v))
    .map(([k, v]) => `<div class="mh-field"><strong>${k}</strong><span>${escapeHtml(v)}</span></div>`)
    .join("");

  // 评分胶囊：豆瓣（绿「豆」字 logo）/ IMDb（金底黑字小标签），均带外链
  const ratings = [];
  if (m.doub_score && !["0", "@"].includes(m.doub_score)) {
    const votes = fmtVotes(m.doub_votes);
    ratings.push(`<a class="mh-rating" href="https://movie.douban.com/subject/${encodeURIComponent(m.idcode)}/" target="_blank" rel="noopener noreferrer" title="在豆瓣查看">
      <span class="mh-logo mh-logo-doub">豆</span><span class="mh-rating-score">${escapeHtml(m.doub_score)}</span>${votes ? `<span class="mh-count">${votes}</span>` : ""}</a>`);
  }
  if (m.imdb_score && m.imdb_score !== "0" && m.imdb_id) {
    const votes = fmtVotes(m.imdb_votes);
    ratings.push(`<a class="mh-rating" href="https://www.imdb.com/title/${encodeURIComponent(m.imdb_id)}/" target="_blank" rel="noopener noreferrer" title="在 IMDb 查看">
      <span class="mh-logo mh-logo-imdb">IMDb</span><span class="mh-rating-score">${escapeHtml(m.imdb_score)}</span>${votes ? `<span class="mh-count">${votes}</span>` : ""}</a>`);
  } else if (m.imdb_id) {
    ratings.push(`<a class="mh-rating" href="https://www.imdb.com/title/${encodeURIComponent(m.imdb_id)}/" target="_blank" rel="noopener noreferrer" title="在 IMDb 查看">
      <span class="mh-logo mh-logo-imdb">IMDb</span><span class="mh-count">${escapeHtml(m.imdb_id)}</span></a>`);
  }

  // 海报（站点图床外链，懒加载；无海报时给占位块，保持与主站相同的版心）
  const poster = m.image && /^https?:\/\//i.test(m.image)
    ? `<img class="mh-poster" src="${escapeHtml(m.image)}" loading="lazy" alt="${escapeHtml(title)} 海报" referrerpolicy="no-referrer">`
    : `<div class="mh-poster mh-noposter">暂无海报</div>`;

  const summary = m.abstract
    ? `<div class="mh-summary"><h3>剧情简介</h3><p>${escapeHtml(m.abstract)}</p></div>`
    : "";

  return `
    <div class="movie-head">
      <div class="mh-poster-wrap">${poster}</div>
      <div class="mh-info">
        <div class="mh-headline">
          <div class="mh-title">${escapeHtml(title)}${years ? `<span class="mh-years">(${escapeHtml(years)})</span>` : ""}</div>
          <button type="button" class="act" data-back="1">← 返回片名列表</button>
        </div>
        ${otitle}
        ${meta ? `<div class="mh-meta">${meta}</div>` : ""}
        ${ratings.length ? `<div class="mh-ratings">${ratings.join("")}</div>` : ""}
        ${summary}
      </div>
    </div>`;
}

function movieHeadEmptyHtml() {
  return `
    <div class="movie-head">
      <div class="mh-poster-wrap"><div class="mh-poster mh-noposter">暂无海报</div></div>
      <div class="mh-info">
        <div class="mh-headline">
          <div class="mh-title">${escapeHtml(state.movieTitle || `影片 ${state.movieId}`)}</div>
          <button type="button" class="act" data-back="1">← 返回片名列表</button>
        </div>
        <div class="mh-otitle">详情尚未拉取 —— 可在「同步」页启动「拉取影片详情」，之后即会显示原名／年份／分类／演员／评分与海报</div>
      </div>
    </div>`;
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

  // 页码窗口：总页数少时全部列出；多时显示当前页左右各 RADIUS 页，并始终保留首尾页
  const PLAIN_LIMIT = 15;  // 总页数不超过此值就直接全列，不做省略
  const RADIUS = 4;        // 超出时当前页左右各显示几页
  const win = new Set([1, totalPages]);
  if (totalPages <= PLAIN_LIMIT) {
    for (let p = 1; p <= totalPages; p++) win.add(p);
  } else {
    for (let p = page - RADIUS; p <= page + RADIUS; p++) {
      if (p >= 1 && p <= totalPages) win.add(p);
    }
  }
  const sorted = [...win].sort((a, b) => a - b);
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

  // 页码跳转（2bt0 接口不返回真实总页数，total_pages 恒为当前页+1，故只有本地库限定上界）
  const bounded = state.tab === "local";
  const jump = document.createElement("span");
  jump.className = "pager-jump";
  jump.innerHTML = `<input type="number" min="1"${bounded ? ` max="${totalPages}"` : ""}
    value="${page}" aria-label="跳转到指定页码" /><button type="button">跳转</button>`;
  const input = jump.querySelector("input");
  const go = () => {
    let n = Math.floor(Number(input.value));
    if (!Number.isFinite(n) || n < 1) { input.value = String(page); return; }
    if (bounded) n = Math.min(n, totalPages);
    if (n === page) { input.value = String(page); return; }
    state.page = n;
    load();
    window.scrollTo({ top: 0, behavior: "smooth" });
  };
  jump.querySelector("button").addEventListener("click", go);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); go(); }
  });
  el.pager.appendChild(jump);
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

// 顶栏徽标专用：压缩成「82.3万」，避免文案过长把顶栏挤成两行
function fmtCompact(n) {
  const v = Number(n || 0);
  return v >= 10000 ? `${(v / 10000).toFixed(1)}万` : String(v);
}

// 顶栏徽标专用：ETA 压缩成「1小时40分」
function fmtEtaShort(sec) {
  if (sec == null) return "计算中";
  if (sec < 3600) return `${Math.max(1, Math.round(sec / 60))}分`;
  const h = Math.floor(sec / 3600);
  const m = Math.round((sec % 3600) / 60);
  return m ? `${h}小时${m}分` : `${h}小时`;
}

// 单个板块卡片（含该板块自己的影片详情进度）
function syncCardHtml(sc, s, m) {
  const label = SECTIONS[sc];
  const prog = s.progress && s.progress[String(sc)];
  const isDone = (s.done || []).includes(sc);
  const isRunning = s.running && s.section === sc;
  const detailRunning = !!(m && m.running && m.section === sc);
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
           <div class="progress-num">${s.mode === "update"
             ? `增量更新中 · 第 ${fmtNum(s.page)} 页（连续 10 页无新资源即停）`
             : "正在探测板块总页数…"}</div>`)
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
    <div class="sync-card ${isRunning || detailRunning ? "running" : ""}">
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
      ${detailBlockHtml(sc, m)}
    </div>`;
}

// 板块卡片里的「影片详情」子块：详情按板块分开统计和拉取
function detailBlockHtml(sc, m) {
  if (!m) return "";
  const st = (m.sections || {})[String(sc)] || {};
  const pending = st.pending || 0;
  const fetched = st.fetched || 0;
  const wanted = st.wanted || 0;
  const isRunning = !!m.running && m.section === sc;

  let badge, badgeCls;
  if (isRunning) { badge = "拉取中"; badgeCls = "run"; }
  else if (pending) { badge = `待拉取 ${fmtNum(pending)} 部`; badgeCls = "pause"; }
  else if (wanted) { badge = "已完成"; badgeCls = "done"; }
  else { badge = "无数据"; badgeCls = "none"; }

  const pct = isRunning && m.total > 0
    ? Math.min(100, (m.done / m.total) * 100).toFixed(1) : null;
  const progressHtml = isRunning
    ? `<div class="progress"><div class="progress-bar" style="width:${pct}%"></div></div>
       <div class="progress-num">${pct}% · 已拉取 ${fmtNum(m.done)} / ${fmtNum(m.total)} 部</div>`
    : "";
  const runRows = isRunning
    ? `<div class="sync-row"><span>速度</span><span>${m.speed > 0 ? `${fmtNum(m.speed)} 部/分钟` : "采样中…"}</span></div>
       <div class="sync-row"><span>预计剩余</span><span class="eta">${fmtEta(m.eta_seconds)}</span></div>`
    : "";
  // 上次拉取结果只在本板块跑过时展示，避免两个卡片显示同一句话
  const lastRow = m.message && m.section === sc
    ? `<div class="sync-row"><span>最近一次</span><span>${escapeHtml(m.message)}</span></div>` : "";

  const btn = isRunning
    ? `<button type="button" class="stop" data-detail-stop="1">停止拉取详情</button>`
    : `<button type="button" data-detail="${sc}" ${pending ? "" : "disabled"}>${
        pending ? `拉取${SECTIONS[sc]}详情（${fmtNum(pending)} 部）` : "详情已全部拉取"}</button>`;

  return `
    <div class="sync-detail">
      <div class="sync-detail-head">
        <span>影片详情</span>
        <span class="sbadge ${badgeCls}">${badge}</span>
      </div>
      ${progressHtml}
      <div class="sync-rows">
        ${runRows}
        <div class="sync-row"><span>详情已入库</span><span>${fmtNum(fetched)} / ${fmtNum(wanted)} 部</span></div>
        ${lastRow}
      </div>
      <div class="sync-actions">${btn}</div>
      <div class="stats-hint">片名 / 原名 / 别名 / 年份 / 分类 / 豆瓣与 IMDB 评分 / 地区 / 导演 / 主演 / 简介。站点无批量接口，按影片逐个拉取（约 0.3 秒/部，4 线程并发）；本板块同步跑完会自动跟进。</div>
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

function renderSyncPage(s, m) {
  el["sync-grid"].innerHTML =
    syncCardHtml(1, s, m) + syncCardHtml(2, s, m) + statsCardHtml(s);
  // 绑定按钮事件（innerHTML 重建后需重绑）
  for (const btn of el["sync-grid"].querySelectorAll("[data-full]")) {
    // 必须显式传 full：后端在未指定模式时，对"已全量完成"的板块会自动降级成增量更新
    btn.addEventListener("click", () => startSync(Number(btn.dataset.full), "full"));
  }
  for (const btn of el["sync-grid"].querySelectorAll("[data-update]")) {
    btn.addEventListener("click", () => startSync(Number(btn.dataset.update), "update"));
  }
  for (const btn of el["sync-grid"].querySelectorAll("[data-stop]")) {
    // 卡片上的停止只停种子同步；顶栏停止按钮才会连影片详情任务一起停
    btn.addEventListener("click", stopSectionSync);
  }
  for (const btn of el["sync-grid"].querySelectorAll("[data-detail]")) {
    btn.addEventListener("click", () => startMovieFetch(Number(btn.dataset.detail)));
  }
  for (const btn of el["sync-grid"].querySelectorAll("[data-detail-stop]")) {
    btn.addEventListener("click", stopMovieFetch);
  }
}

async function startMovieFetch(section) {
  try {
    const res = await fetch("/api/movies/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(section ? { section } : {}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    showToast(`已开始拉取${SECTIONS[section] || ""}影片详情（共 ${fmtNum(data.total)} 部）`);
  } catch (err) {
    showToast(`启动失败：${err.message}`);
  }
  refreshSyncUI(false);
}

async function stopMovieFetch() {
  try {
    await fetch("/api/movies/stop", { method: "POST" });
    showToast("正在停止影片详情拉取…");
  } catch { /* 忽略 */ }
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

// 板块卡片上的「停止」：只停种子同步，不影响同时跑着的影片详情任务
async function stopSectionSync() {
  try {
    await fetch("/api/sync/stop", { method: "POST" });
    showToast("正在停止同步…");
  } catch { /* 忽略 */ }
}

async function stopSync() {
  // 顶栏停止按钮同时停两类后台任务（未在跑的调用是空操作）
  try {
    await Promise.all([
      fetch("/api/sync/stop", { method: "POST" }),
      fetch("/api/movies/stop", { method: "POST" }),
    ]);
    showToast("正在停止后台任务…");
  } catch { /* 忽略 */ }
}

async function fetchMovieStatus() {
  try {
    const res = await fetch("/api/movies/status");
    return await res.json();
  } catch { return null; }
}

async function refreshSyncUI(notifyDone) {
  let s;
  try {
    const res = await fetch("/api/sync/status");
    s = await res.json();
  } catch { return; }
  const m = await fetchMovieStatus();
  const busy = s.running || !!(m && m.running);

  // 顶栏徽标（任何 tab 下都显示）
  if (busy) {
    el["sync-badge"].hidden = false;
    if (s.running) {
      const modeText = s.mode === "update" ? "增量更新"
        : s.mode === "resume" ? "断点续抓" : "全量同步";
      const etaText = s.mode !== "update" && s.eta_seconds != null
        ? `·剩${fmtEtaShort(s.eta_seconds)}` : "";
      el["sync-badge-text"].textContent =
        `${modeText}·${s.section_label || `板块${s.section}`} 第${fmtNum(s.page)}页·${fmtCompact(s.db_total)}条${etaText}`;
    } else {
      const etaText = m.eta_seconds != null ? `·剩${fmtEtaShort(m.eta_seconds)}` : "";
      el["sync-badge-text"].textContent =
        `影片详情${m.section_label ? `·${m.section_label}` : ""} ${fmtCompact(m.done)}/${fmtCompact(m.total)}部${etaText}`;
    }
  } else {
    el["sync-badge"].hidden = true;
    if (notifyDone && state.prevRunning) {
      showToast(s.message
        ? `同步结束：${s.message}，库内共 ${fmtNum(s.db_total)} 条`
        : `同步结束，本地库共 ${fmtNum(s.db_total)} 条`);
    }
  }

  // 同步管理页内容（仅在该 tab 下渲染，避免多余 DOM 操作）
  if (state.tab === "sync") renderSyncPage(s, m);

  // 只要有任务在跑就保持轮询，跑完自动停
  if (busy) {
    if (!syncTimer) syncTimer = setInterval(() => refreshSyncUI(true), 3000);
  } else if (syncTimer) {
    clearInterval(syncTimer);
    syncTimer = null;
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
  state.movieId = ""; // 搜索针对整个库，退出单片视图
  state.movieTitle = "";
  applyCatFilter();
  load();
});

el["sync-stop"].addEventListener("click", stopSync);

el["cat-filter"].addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-cat]");
  if (!btn) return;
  state.localCategory = btn.dataset.cat;
  state.page = 1;
  state.movieId = "";
  state.movieTitle = "";
  applyCatFilter();
  load();
});

el["sort-by"].addEventListener("change", () => {
  state.localSort = el["sort-by"].value;
  state.page = 1; // 换排序后回到第一页
  load();
});

el["view-toggle"].addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-view]");
  if (!btn) return;
  state.localView = btn.dataset.view;
  state.movieId = "";
  state.movieTitle = "";
  state.page = 1;
  applyCatFilter();
  load();
});

el["log-refresh"].addEventListener("click", loadLogs);

// 初始化
switchTab("movie");
refreshSyncUI(false); // 驱动顶栏同步徽标
