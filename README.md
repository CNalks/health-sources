# health-sources

[![Secret scan](https://github.com/CNalks/health-sources/actions/workflows/gitleaks.yml/badge.svg)](https://github.com/CNalks/health-sources/actions/workflows/gitleaks.yml)
[![Fetch blocked sources](https://github.com/CNalks/health-sources/actions/workflows/fetch-blocked.yml/badge.svg)](https://github.com/CNalks/health-sources/actions/workflows/fetch-blocked.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![License](https://img.shields.io/badge/code-Apache--2.0-green)

> Shared collection layer for public-health **articles** and **news / official notices** (WHO DON, ECDC, MMWR,
> Eurosurveillance, EID, PLOS NTDs, China CDC Weekly, US CDC travel notices, Africa CDC, PAHO alerts).
> Fetch → normalise to one schema → SQLite → JSONL snapshots → incremental push to downstream products.
> No time series, no LLMs, no mock data.

公卫文章与新闻/通报的**共享采集层**。每天从固定来源抓取最新条目，规范成统一字段，
写入 SQLite 并导出 JSONL 快照，供下游产品各取所需：

- **公卫前沿**（CNalks/CDC-Weekly-Digest）三阶段：从 `kind=article` 里挑文章再走自己的出稿流程；
- **Global Health Signals**（CNalks/ai4health-global）：从 `kind=news` 里取疫情事件/官方通报。

当前阶段只做到「采集并写文件试跑」。公卫前沿（火山云）改读本库的方案已备好（CDC-Weekly-Digest PR #51），等批准后接入。

**发现靠各刊 RSS，补全靠 Europe PMC 按 DOI 回查。** 新文章只从各刊/各机构自己的 RSS（或官方接口）发现；
Europe PMC 不再作为独立来源广撒网，只在「补全」步骤里按 DOI 逐篇回查，给缺摘要、缺许可的文章补上摘要、许可、PMCID 和关键词。
这样同一篇文章不会因为既出现在期刊 RSS、又出现在 Europe PMC 检索结果里而算两条。

## 定位与边界

- 只收两类：`article`（期刊文章）和 `news`（新闻/通报）。
- **不收**时间序列数据（病例数、阳性率等），那些继续留在 GHS 自己的管线里。
- 字典只有「统一字段 + 关键词」：不做病种词表、国家词表、ICD 编码。
- 不调大模型：关键词优先用原文自带的期刊关键词 / RSS category，没有就从标题按规则提取。

## 来源（`sources.yaml`）

| id | 来源 | kind | 方式 | license | figures_allowed |
|---|---|---|---|---|---|
| `mmwr` | MMWR | article | RSS（只流式读最新 30 条，解析跳转得到原文 URL 和 DOI） | Public Domain | true |
| `eurosurveillance` | Eurosurveillance | article | RSS | CC BY 4.0 | true |
| `who_don` | WHO Disease Outbreak News | news | JSON API，按 `PublicationDateAndTime` 倒序 | CC BY-NC-SA 3.0 IGO | false |
| `eid` | Emerging Infectious Diseases（提前在线） | article | RSS | Public Domain | true |
| `ecdc` | ECDC：CDTR / 风险评估 / 疫情更新 / 新闻 | news | 4 个 RSS 子 feed | 注明出处即可转载 | true |
| `plos_ntds` | PLOS NTDs | article | Atom | CC BY 4.0 | true |
| `cdc_weekly` | China CDC Weekly（当期） | article | RSS 1.0/RDF，条目自带 `<prism:doi>`；feed 无摘要，靠补全 | CC BY-NC 4.0 | false |
| `cdc_thn` | US CDC Travel Health Notices | news | RSS | Public Domain | true |
| `africa_cdc` | Africa CDC News | news | RSS（GHS 侧再按疫情关键词筛） | 署名、非商用 | false |
| `paho` | PAHO Epidemiological Alerts and Updates | news | 列表页 HTML；服务器被拦，由 GitHub Actions 代抓后推入收件箱（见下） | © PAHO/WHO，署名 | false |

`license`、`figures_allowed` 在 `sources.yaml` 里按来源设定。来源也可以设 `figures_allowed: per_record`，
这时补全步骤会按 Europe PMC 给出的单篇许可重算（只有 CC0 / CC BY / CC BY-SA 为 true）；目前没有来源用这个选项。

CDC Weekly 只读订阅官网 RSS，**不抓文章页面**（官网会弹验证码）；公卫前沿自己的采集流程（火山云 cdc-ingest）与本项目无关、互不影响。

已停用：`europepmc`（v0.1 按检索式广泛抓取，与逐刊订阅重复）。它列在 `retired_sources` 里，每次运行开始时删除库里它的旧记录，不再导出。

## 统一字段

每条记录（JSONL 一行）恰好包含以下字段，由 `health_sources/schema.py` 校验：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | string | 16 位十六进制。有 DOI 时为 `sha1("doi:"+小写DOI)`，否则为 `sha1("url:"+规范化URL)` 的前 16 位 |
| `source` | string | 来源 id，见上表 |
| `kind` | `article` \| `news` | |
| `title` | string | 去掉 HTML 标签后的原文标题 |
| `lang` | string | ISO 639-1，目前都是 `en` |
| `published_at` | string \| null | ISO 8601，带时区（统一为 UTC `+00:00`） |
| `url` | string | 规范化 URL：小写域名、去掉 `utm_*`/`TRACK`/`s_cid` 等跟踪参数和锚点 |
| `doi` | string \| null | 小写 DOI。优先用条目自带的（`<prism:doi>`、`<dc:identifier>`、DOI 形式的 id），否则由 URL 推出：MMWR（含提前发布 `mm7540e1`、增刊 `su7501a6`）、EID、Eurosurveillance、PLOS；推不出就为 null，也就不参加补全 |
| `summary` | string \| null | 纯文本摘要，最长约 1200 字符；feed 没有摘要时为 null |
| `keywords` | string[] | 优先原文关键词 / RSS category（最多 6 个）；没有则从标题提取 3–5 个短语（标题太短时可能更少） |
| `license` | string \| null | 许可说明 |
| `figures_allowed` | bool | 下游能否配原文图表 |
| `fetched_at` | string | 最近一次成功抓到这条记录的时间（UTC） |
| `status` | `ok` \| `stale` | `stale` 表示该来源最近一次抓取失败，这条是沿用的旧数据 |
| `pmcid` | string \| null | Europe PMC / PMC 的 PMCID（如 `PMC13674758`），由补全步骤填写 |
| `enriched_at` | string \| null | 最近一次补全写入字段的时间（UTC）；从没补过为 null |
| `enrich_source` | string \| null | 补全数据来自哪里，目前只有 `europepmc`；与 `enriched_at` 同时为空或同时有值 |
| `journal` | string \| null | 刊名（v0.3 起；CDC Weekly 取 `<prism:publicationName>`，回补时取 Crossref / PLOS 的刊名） |
| `volume` / `issue` / `pages` | string \| null | 卷、期、页码（CDC Weekly 取 PRISM 字段；Crossref / PLOS 回补给出的也会填） |
| `article_type` | string \| null | 栏目 / 文章类型。只有来源给了才填（目前只有 PLOS 回补）；CDC Weekly 的 feed 不带栏目，留空 |
| `authors` | string[] \| null | 作者。CDC Weekly 的 feed 里作者为空，留空；Crossref / PLOS 回补时填写 |
| `issue_order` | int \| null | 在本期 feed 里的顺序（0 起），CDC Weekly 按期组织时用 |
| `feed_url` | string \| null | 这条记录最近一次来自哪个 feed / 接口 |

取不到的字段一律为 null，不推测、不拼造。CDC Weekly 的 `title`、`url` 刻意保留与公卫前沿 `scripts/fetch-ccdc.mjs`
完全相同的清洗规则（见 `health_sources/adapters/ccdc.py`），下游改读本库后生成的 `data/inbox` 与直接读 RSS 一致。

### 关键词规则

1. 原文自带：RSS/Atom 的 `<category>`、ECDC 子 feed 栏目名（如 `Risk assessment`）、补全时 Europe PMC 的 `keywordList`。
   每个来源可用 `ignore_categories` 去掉无意义的通用分类（如 MMWR 的 `MMWR`）。
2. 否则从标题提取：用停用词表（含月份、"United States" 等）和标点把标题切成短语，
   按精简版 RAKE 打分取前 3–5 个，丢掉年份、日期区间等数字。代码在 `health_sources/keywords.py`。

## 补全：按 DOI 回查 Europe PMC

代码在 `health_sources/enrich.py`，配置在 `sources.yaml` 的 `enrich:`。每次 `run` 在采集完、导出前执行一次：

- **谁会被回查**：`kind=article`、有 DOI、`summary` 或 `license` 为空，且发布（没有发布时间则按首次见到）在 30 天内的记录。
- **怎么查**：`GET https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:"<doi>"&resultType=core`，
  一篇一个请求。同一 DOI 常有 MED、PMC 两条结果（PMC 那条有时不带 doi 字段），合并取摘要、许可、PMCID、关键词。
- **只填空字段**：已有的摘要、许可、PMCID 不覆盖，`source` 不改。关键词例外：原来只是从标题自动提取的，才换成原文关键词。
  补全写过的字段记在库里的 `enriched_fields`，之后 feed 再抓到同一篇也不会把它们改回去。
- **配图**：只有来源设了 `figures_allowed: per_record` 时，才按单篇许可重算 `figures_allowed`（CC0 / CC BY / CC BY-SA 才允许）。
- **查不到 / 查到了但还没摘要**：在 `enrich_attempts` 表记一次尝试，20 小时后（即下一次每日运行）再试，
  发布后 30 天内反复尝试——Europe PMC 收录常比原刊晚 10 天以上。
- **限流**：每次最多查 `max_per_run`（60）篇，请求间隔 `delay_seconds`（1 秒），整个步骤最多 `max_seconds`（300 秒）；
  从没试过的优先，其次按发布时间从新到旧。
- **失败隔离**：单篇请求出错只记一次 `error` 尝试；整个步骤出错只写日志，采集和导出照常完成。
- 结果写进 `status.json` 的 `last_enrich`（本次统计）和 `enrich_attempts`（累计），每个来源的 `with_summary` / `summary_from_enrich` 也在里面。

## 往期回补（`backfill`）

代码在 `health_sources/backfill.py`，每个来源的往期入口写在 `sources.yaml` 的 `backfill:`。只用官方 feed / API，不抓 HTML 网页：

| 来源 | 往期入口 | 说明 |
|---|---|---|
| `cdc_weekly` | `rss/{年}_{期}.xml` | 每期一个请求，间隔 6 秒（官网有验证码风控）；期号元数据对不上的条目丢弃 |
| `mmwr` | 全量 RSS | 2000+ 条、不按时间排序：流式按 100 条一块解析，只留 `--since` 之后的，再逐条解析下载跳转 |
| `eurosurveillance` | Crossref `journals/1560-7917/works` | 官网按期 RSS 返回空、目录页是 HTML，所以走 DOI 注册机构的 API |
| `eid` | Crossref `journals/1080-6059/works` | 官网只有提前在线 feed；Crossref 对 EID 只给到「年-月」，`published_at` 改用 DOI 注册时间（约等于上线日期）；明显错误的未来日期跳过 |
| `who_don` | 官方 JSON 接口 `$skip` 分页 | 50 条一页，翻到早于 `--since` 为止 |
| `ecdc` | taxonomy feed `?page=N` | 4 个子 feed 各自翻页；服务器返回重复页时停止并记 note。实测（2026-10-25）ECDC 忽略分页参数，往期只能拿到 feed 当前给出的那些 |
| `plos_ntds` | PLOS 搜索 API（`doc_type:full` + 日期过滤） | 100 条一页 |

- 每页 / 每期解析完立即写库（按 id 去重合并），不把整段历史读进内存；
- 一个来源失败不影响其他来源；不改 `source_runs`（每日采集的健康状态），结果写在 `backfill/<来源>.json`；
- 回补完自动重新导出。往期文章的摘要可再用 `enrich --window-days 120 --max-per-run 1000 --max-seconds 1500` 补。

```bash
python -m health_sources --data-dir ./data backfill --only cdc_weekly --since 2026-01-01 --year 2026 --issues 28-43
python -m health_sources --data-dir ./data backfill --only mmwr eurosurveillance who_don eid ecdc plos_ntds --since 2026-08-01
```

## 存储与输出

数据目录（服务器上是 `/var/lib/health-sources/`）：

```
health_sources.sqlite     # 全量记录，按 id 去重/更新；source_runs 表记每个来源最近一次抓取结果
latest.jsonl              # 最近 30 天（按 published_at）的全部记录，每次运行原子替换
by-source/<来源>.jsonl    # 每个来源近 400 天的全部记录（按期组织的下游读这里，如公卫前沿取 CDC Weekly 卷期篇目）
backfill/<来源>.json      # 最近一次往期回补的结果
exports/YYYY-MM-DD.jsonl  # 每日快照（北京时间日期），保留 90 天
status.json               # 每个来源的条数、最新日期、stale 数、最近一次成功/失败与错误信息、by-source 文件大小
```

更新规则：同一 id 再次抓到时更新内容，但已有的非空字段不会被空值覆盖，补全写过的字段也不会被 feed 覆盖。
旧库（v0.1）启动时自动补列；没有 DOI、但现在能从 URL 推出 DOI 的旧记录会补上 DOI 并换成按 DOI 计算的 id（已有同 DOI 记录则合并）。

### 失败隔离

每个来源单独 try/except。某个来源失败时：它在库里的旧记录全部标为 `status=stale` 并照常导出，
`status.json` 里记录错误；其他来源不受影响。下次抓取成功后自动恢复为 `ok`。
ECDC 的 4 个子 feed 也是各自独立，坏一个只记 warning。

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 抓全部来源，写到 ./data
.venv/bin/python -m health_sources --data-dir ./data run
# 只抓某几个来源
.venv/bin/python -m health_sources --data-dir ./data run --only who_don ecdc
# 只从 SQLite 重新导出
.venv/bin/python -m health_sources --data-dir ./data export
# 只跑补全（按 DOI 回查 Europe PMC），然后重新导出
.venv/bin/python -m health_sources --data-dir ./data enrich
# 采集但跳过补全
.venv/bin/python -m health_sources --data-dir ./data run --no-enrich
# 校验 latest.jsonl 每条记录
.venv/bin/python -m health_sources --data-dir ./data validate
```

依赖只有 `feedparser`、`requests`、`PyYAML`。

### 测试（离线，用 `tests/fixtures/` 里的本地样本）

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

## 被拦来源（GitHub Actions 代抓）

paho.org 拦截云服务器出口 IP（阿里云 403），GitHub 托管 runner 可以访问。`.github/workflows/fetch-blocked.yml`
每天 10:30（北京时间）运行，也可手动触发：

1. `python -m health_sources collect --only paho --out out/paho.jsonl` —— 用本库 `paho` 适配器直连抓取并按统一字段校验，
   不建数据库；
2. 顺路下载 US CDC NNDSS 周报 CSV（**不进本库**：时间序列不走 hs），只转交给 GHS 管线的原始缓存；
3. 用一把**受限 SSH 密钥**推给服务器：服务器端 `authorized_keys` 为 `restrict,command="/usr/local/bin/hs-intake"`，
   接收脚本只认 `paho` / `nndss` 两个目标，校验格式后原子替换（见 `deploy/intake/`）。

服务器上的日常采集（11:00）发现 `paho` 配了 `inbox` 且文件存在，就读收件箱而不联网；收件箱超过 72 小时未更新
→ 该来源标 `stale`，不造数据。主机、端口、密钥、known_hosts 都在仓库 secrets 里，日志只打印条数。

## 定时一览（北京时间）

| 时间 | 在哪 | 任务 |
|---|---|---|
| 每天 10:30 | Actions `fetch-blocked.yml` | PAHO → hs 收件箱；NNDSS CSV → GHS 原始缓存 |
| 每天 11:00 | 服务器 `health-sources.timer` | hs 采集全部来源 |
| 采集成功后 + 11:45/13:00 兜底 | 服务器 `hs-push@cdc` | 推公卫前沿 |
| 每天 11:30 | 服务器 `ghs-events-refresh` | GHS 事件页数据 + 新鲜度 |
| 周一、周四 12:00 | 服务器 `ghs-pipeline-refresh` | GHS 时间序列 |
| 每天 13:40 | Actions `ghs-site.yml` | GHS 建站安装（数据/代码有变才建）+ 新鲜度监控（过期/失败 → 邮件给仓库所有者） |
| 周日 11:30 | 服务器 `hs-reconcile@cdc` | 与 CloudBase 对账 |

`ghs-site.yml` 用只读 deploy key 拉 GHS 私有仓库，只在定时/手动触发时运行（fork 的 PR 拿不到 secrets），构建日志不公开、
不上传产物和缓存；离开 runner 的只有本来就公开的网站文件。

## 安全

- 仓库公开。密钥、订阅方配置（`deploy/subscribers/*.toml`）、`.env*`、`*.key` 等由 `.gitignore` 屏蔽，只放 `*.example`。
- 每次 push / PR 跑 gitleaks 全历史扫描（`.github/workflows/gitleaks.yml`）；本地可 `pre-commit install` 启用同样的检查。
- 服务器地址、密钥只在服务器和 GitHub secrets 里。

## 许可

- 代码：Apache-2.0（见 `LICENSE`）。
- 采集到的条目：版权归各来源，按 `sources.yaml` 里每个来源的 `license` 字段使用；本库只保存标题、日期、链接、摘要等元数据。

## 部署（阿里云北京）

- 代码：`/opt/health-sources`（venv 在 `.venv`，不改系统 Python）
- 数据：`/var/lib/health-sources/`，属主为系统用户 `health-sources`
- 定时：`deploy/health-sources.timer`，每天 11:00（北京时间，2026-10-25 起，原 10:30；公卫前沿火山云 11:17 读取）；`Persistent=true`，错过会补跑
- 资源：`MemoryMax=150M`、`MemorySwapMax=0`、`CPUQuota=50%`、`Nice=10`、IO idle；`ProtectSystem=strict`，只允许写数据目录
- 安装：代码放到 `/opt/health-sources` 后以 root 运行 `deploy/install.sh`
- 下游只读访问（2026-10-25 起，为公卫前沿火山云采集准备，尚未接入定时任务）：系统账号 `hs-reader`，
  `authorized_keys` 为 `restrict,from="<downstream-ip>",command="/usr/local/bin/hs-export-read"`，
  强制命令只输出 `status.json` + `by-source/*.jsonl` 的 tar（源码在 CNalks/CDC-Weekly-Digest `ops/aliyun/hs-export-read`）。
  撤销：`userdel -r hs-reader && rm /usr/local/bin/hs-export-read`

```bash
systemctl list-timers health-sources.timer     # 下次运行时间
systemctl start health-sources.service         # 手动跑一次
journalctl -u health-sources.service -n 50     # 日志（含峰值内存）
cat /var/lib/health-sources/status.json
```

## 下游推送（push，v0.4）

单向、增量、异步：本库只往外推，订阅方只收不回写。通用模块 `health_sources/push.py`，订阅方配置
`/etc/health-sources/subscribers/<name>.toml`（样例 `deploy/subscribers/cdc.toml.example`），密钥文件 `<name>.secret`
（root 0600，systemd `LoadCredential` 交给进程，不进仓库、不进环境变量、不打印）。

- **增量**：按 `records.updated_at + id` 复合游标，只推内容有变化的记录；`fetched_at`、`status` 不推（每天都变）。
  每条带 `hs_updated_at`、`sort_key`（= 导出排序键 `COALESCE(published_at, first_seen_at)`）、`content_hash`（规范化内容 sha256）。
- **游标存在订阅方**：每次先问订阅方确认到哪（`cursor`），每批确认后才推进；失败就停，下次从订阅方游标续传。
- **删除**：`tombstones` 表。停用来源（`purge_source`，reason=retired）、URL id 合并到 DOI id（merged）、换 id（rekeyed，
  新 id 的 `updated_at` 同时更新）都会留标记，按 `(deleted_at, id)` 游标推送；订阅方做软删除。删过又回来的条目标记作废。
- **对账**：每周一次（`--reconcile`）发窗口内全部 `id + content_hash`，订阅方回缺失/不同的，这里补推（不动游标）；
  订阅方把清单里没有、窗口内的条目标删除。
- **通道**：线上用 `https-hmac`（CloudBase HTTP 路由 `/hs-ingest`，HMAC-SHA256 签 `时间戳.nonce.请求体`，时间差 ≤300 秒、nonce 一次性）；`tencent-scf-invoke`（CAM 子用户直接 Invoke）保留未用。
- **触发**：`health-sources.service` 成功后 `OnSuccess=hs-push@cdc.service`（`deploy/health-sources.service.d/push-cdc.conf`），
  另有 `hs-push@.timer` 11:45、13:00 兜底；`hs-reconcile@.timer` 周日 11:30 对账。推送进程只读 SQLite（`mode=ro`）。
- 预演（不联网、不需要密钥）：`python -m health_sources --data-dir ./data push --subscriber cdc --dry-run`

以上单元**不随代码自动安装**；安装前 `health-sources.service` 完全不变。

## 新增来源

1. 在 `sources.yaml` 加一项（`id`、`adapter`、`kind`、`url`、`license`、`figures_allowed`）。
2. 普通 RSS/Atom 直接用 `adapter: rss`；特殊接口在 `health_sources/adapters/` 加一个模块，实现
   `fetch(cfg, http, fetched_at)`（联网）和 `parse(payload, cfg, fetched_at)`（纯解析，便于测试），
   并在 `adapters/__init__.py` 注册。
3. 在 `tests/fixtures/` 放一份裁剪过的样本，补测试。

## 规范化标签（v0.5，给 GHS）

可选字段（可空列表，不破坏旧消费者）：

| 字段 | 含义 |
|---|---|
| `disease_tags` | 公卫前沿病种词表 **code**（如 `ebola`、`influenza`），不在本库另维护词表 |
| `country_iso3` | ISO 3166-1 alpha-3 |

词表路径（`HS_VOCAB_DISEASES`，或 `/opt/cdc-weekly-digest/data/vocab/diseases.json` 等候选）。找不到词表时 `disease_tags` 为 `[]`，采集照常。

## 新增新闻源（2026-10）

| id | 说明 | 许可（sources.yaml） |
|---|---|---|
| `ukhsa` | UKHSA GOV.UK Atom | Open Government Licence v3.0 |
| `cdc_han` | US CDC HAN RSS | Public Domain |
| `hk_chp` | 港 CHP 英文公报 RSS | CHP Important Notices |
| `who_afro` | WHO AFRO emergencies RSS | CC BY-NC-SA 3.0 IGO |
| `phac_news` | 加拿大卫生新闻 Atom | Crown；逐条核对开放许可 |
| `tw_cdc` | 台湾 CDC 英文新闻 | 站点条款；**Actions→inbox**（阿里云不可达） |

数值/时间序列仍不进本库。

