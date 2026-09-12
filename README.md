# 2bt0 资源库

抓取 [2bt0.com](https://www.2bt0.com) 的磁力资源并落库到本地 SQLite，提供 Web 界面浏览、检索、追新与影片资料。

该站是 Vue 单页应用，直接抓页面只能拿到空壳，因此本项目直连其后端 JSON API（`/prod/api/v1/*`），匿名访问、无需登录。

当前库内约 **82 万条**磁力（电影 2.2 万页、电视剧 1.9 万页）。

## 功能

- **双板块抓取**：电影、电视剧分开抓取、分开浏览（站点热门榜 3/4/5 不抓）
- **三种同步模式**：断点续抓、增量更新（秒级追新）、全量重抓
- **三个列表 Tab 都读本地库**：电影 / 电视剧 Tab 是各自板块的片名海报墙（每页 24 部，可按最新入库/评分最高/评分人数/上映时间/版本数排序）；「本地磁力库」是全部板块的全部磁力版本列表，可切电影 / 电视剧 / 全部；搜索均匹配片名、原名、别名、演员、导演、种子名与 info_hash
- **筛选条**：分类参考主站影片库（影视类型 / 制片地区 / 上映年份 / 资源画质 / 影视标签 五组标签 + 排序方式 + 高级筛选：评分人数、豆瓣评分区间、仅看 IMDb），站点那项「仅显示网盘资源」不做；可选项由本地库实际数据聚合，点了一定有结果
- **影片详情卡**：点开某部影片后置顶展示，布局参考主站详情页（250×375 海报 + 片名年份 + 原名/别名 + 导演/主演/类型/地区/语言/片长 + 豆瓣与 IMDb 评分胶囊外链 + 剧情简介限高滚动）
- **影片资料**：片名、原名、别名、年份、类型、地区、语言、片长、豆瓣/IMDB 评分、导演、主演、剧情简介
- **每日定时增量**：到点自动对电影、电视剧依次追新，每个板块跑完自动跟进该板块的新片详情（海报/评分），开关与时间可在网页修改
- **自动备份**：每日增量跑完用 `VACUUM INTO` 生成紧凑快照，保留最近 7 份
- **终端风格日志页**：5 秒自动刷新、自动滚底、ERROR/WARNING 着色
- **深色主题**：顶部 5 个 Tab（电影 / 电视剧 / 本地磁力库 / 日志 / 同步），列表数据全部来自本地库，不再在线浏览站点列表
- **手机适配**：≤720px 顶栏自动分两行、Tab 条横向滑动、同步徽标独占一行；海报墙固定两列；按钮与输入框按触控尺寸放大，输入框字号 ≥16px 避免 iOS 聚焦时整页缩放

## 快速开始

### NAS 部署（拉取已构建镜像，无需本地构建）

```bash
# 1. 把 docker-compose.nas.yml 放到 NAS 上任意目录
# 2. cd 到该目录执行：
docker compose -f docker-compose.nas.yml up -d
# 3. 浏览器打开 http://<NAS的IP>:8000
```

镜像公开，无需 `docker login`；支持 `linux/amd64` 与 `linux/arm64`。

### 本地构建

```bash
docker compose up -d --build
# 打开 http://127.0.0.1:8001
```

### 首次使用

1. 进「同步」页，点电影的「开始全量同步」，等抓完（约 3~8 小时，视站点速度）；抓完会自动接着拉该板块的影片详情（海报/评分）
2. 电影跑完再点电视剧（约 8 小时），同样会自动跟进详情
3. 之后每天定时增量会自动追新并跟进新片详情，也可手动点「增量更新」

## 目录结构

```
app/
  main.py           FastAPI 入口与全部 API 路由
  db.py             SQLite 数据层（建表、去重写入、查询、备份）
  sync.py           全量同步 / 增量更新 / 断点续抓
  movies.py         影片详情批量拉取（4 线程后台任务）
  scheduler.py      每日定时增量 + 数据库自动备份
  sources/
    base.py         数据源抽象（Item / PageResult / Source）
    bt0.py          2bt0 数据源（getTList / getVideoDetail 等接口）
    local.py        本地库数据源（查自己）
  static/           前端（原生 JS，无框架）
  Dockerfile
```

## 数据存储

数据库位于 `DATA_DIR/db/magnets.db`，SQLite 开启 WAL 模式（读写并发）。

### `magnets` — 种子表

以 `info_hash`（种子 info 字典的 SHA-1）为唯一键去重。重复抓到时只刷新 `last_seen_at`，并补齐原来为空的字段，不覆盖已有内容。

| 列 | 说明 |
|---|---|
| `info_hash` | 种子指纹（唯一键） |
| `magnet` | 磁力链接 |
| `title` | 资源完整发布名（含画质/字幕/压制组） |
| `size` | 文件大小 |
| `published_at` | 发布日期（站点的「9小时前」等相对时间已换算成真实日期） |
| `category` | 所属板块（电影 / 电视剧） |
| `detail_url` | 站点详情页地址 |
| `source` | 数据来源 |
| `torrent_name` | 种子文件名（按需解析 `.torrent` 时才有） |
| `trackers` | Tracker 列表（JSON） |
| `first_seen_at` | 首次入库时间 |
| `last_seen_at` | 最近一次见到的时间 |
| `movie_id` | 关联影片表（`movies.idcode`） |
| `movie_title` | 片名 |

### `movies` — 影片表

影片级元数据按 `idcode` 唯一。一部影片平均对应十余个种子版本，长文本（简介、主演）只存一份。

`idcode`（即豆瓣 subject id，可拼 `movie.douban.com/subject/{idcode}/`）、`title`、`otitle`、`alias`、`years`、`category`（类型）、`area`、`language`、`episodes`、`long_time`、`doub_score`、`doub_votes`（评价人数）、`imdb_id`、`imdb_score`、`imdb_votes`、`image`（海报 URL）、`director`、`performer`、`abstract`、`tags`（影视标签，逗号分隔）、`definition`（画质，逗号分隔）、`detail_ver`（详情版本）、`fetched_at`

`tags` / `definition` 是筛选条用的两个字段（详情接口里就有，之前没存）。加进来时把 `detail_ver` 一并加上：版本低于当前的旧记录会计入「待拉取」，重拉一次补齐，否则老片永远筛不出标签和画质。

### 其他表

- `sync_state` — 同步进度（`bt0:1` = 电影已抓页码；`bt0:1:done` = 已完成全量）
- `settings` — 网页可改的配置（定时任务开关与小时）

## 同步模式

| 模式 | 触发方式 | 行为 |
|---|---|---|
| 断点续抓 | 容器启动检测到断点会自动开始 | 从上次完成页 +1 开始，直到抓完 |
| 增量更新 | 同步页「增量更新」按钮 / 每日定时 | 只扫前 10 页，连续 10 页无新资源即停（约 20 秒） |
| 全量重抓 | 同步页「开始全量同步」按钮 | 从第 1 页抓到末尾，用于首次建库或重建字段 |

说明：

- 增量更新要求该板块已完成全量同步，否则后端返回 409
- 4 线程并发抓取；同板块一页 20 条，一批写入
- 站点 API 的 `total` 字段不可信（恒为 400），页数由「连续空页 + 多级探测」判定
- 顶部呼吸灯与「同步」页实时显示板块、页码、库内条数、速度与 ETA
- 影片详情按板块并入电影/电视剧卡片：各自显示「已入库 / 待拉取」与独立进度条、速度、ETA 和「拉取详情」按钮；板块同步（全量/续抓/增量）跑完自动跟进本板块详情，详情跑完也会显示在顶栏呼吸灯上
- 详情字段加了 `tags` / `definition` 之后，之前入库的影片算「待拉取」，需要在同步页点一次「拉取详情」重拉补齐（筛选条要靠这两个字段筛标签和画质）
- 同步在服务端后台运行，关掉浏览器不受影响；重建容器也不丢进度

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATA_DIR` | `/data` | 数据库、日志、备份的存放目录 |
| `TZ` | 容器默认（UTC） | 时区，定时任务按此触发，务必设为 `Asia/Shanghai` |
| `SYNC_HOUR` | `3` | 每日增量默认触发小时（0-23）；网页改过之后以数据库为准 |

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/items` | 抓取/浏览条目（`source`、`sc`、`q`、`page`、`category`、`movie_id`） |
| GET | `/api/groups` | 本地库按片名分组（海报墙）：`q`、`category`、`sort`、`page`；筛选 `ftype`/`farea`/`fyears`/`fquality`/`ftag`/`votes_min`/`score_min`/`score_max`/`imdb_only` |
| GET | `/api/filters` | 筛选条可选项（`category` 可选）：只返回本地库确实有数据的标签 |
| GET | `/api/movie/{idcode}` | 影片详情 |
| POST | `/api/sync/start` | 启动同步（`section`、`mode`、`start_page`、`workers`） |
| GET | `/api/sync/status` | 同步状态（进度、速度、ETA） |
| POST | `/api/sync/stop` | 停止同步 |
| POST | `/api/movies/start` | 启动影片详情批量拉取（`section` 可选，不传则拉全部板块） |
| GET | `/api/movies/status` | 影片拉取状态 |
| POST | `/api/movies/stop` | 停止影片拉取 |
| GET / POST | `/api/schedule` | 读取 / 修改每日定时增量配置 |
| GET | `/api/logs` | 读取日志尾部（`lines`） |

接口文档：`/api/docs`

## 说明

本项目仅做资源索引与元数据聚合，不存储、不转发任何文件内容。请遵守当地法律法规，仅将抓取结果用于个人学习与研究。
