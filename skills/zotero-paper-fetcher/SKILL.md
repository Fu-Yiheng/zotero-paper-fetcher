---
name: zotero-paper-fetcher
description: 按研究主题找论文、下载 PDF、导入 Zotero 指定分类。用户只给研究范围（如「桥梁断面气动导纳」）和大致篇数，agent 写检索词在 OpenAlex 检索、读标题摘要筛出几十篇、与 Zotero 库查重，再经 Elsevier API、Wiley TDM API、Springer、开放获取自动下载并导入；ASCE、IEEE 等对脚本弹人机验证的站点，分批在用户自己的浏览器打开，由用户点 Zotero Connector 保存。当用户说「下载 XX 方向的论文」「找几十篇 XX 的文献放进 Zotero」「批量下论文」「把这些 DOI 下下来」时使用。需要在校园网或机构 IP 内运行。
---

# Zotero Paper Fetcher：按主题批量下载论文到 Zotero

分工：**检索词、筛选、判断由 agent 做**；下载、校验、导入由脚本 `scripts/paperfetch.py` 做；
**人机验证和受保护站点的保存由用户自己点**。

## 红线

- **不绕过、不代点任何人机验证 / 验证码**。脚本遇到就把那篇转成 `manual`，交给用户的浏览器。
  不要修改脚本去伪装浏览器 User-Agent，不要用自动化浏览器（Playwright、CDP、浏览器扩展）去打开 `BOT_WALLED` 里的站点。
- 出版社许可禁止系统性批量下载，违规时整个学校的 IP 段可能被封。单次上限 80 篇（`--max`），
  不要为绕开上限连续分批跑，用户确需更多时先提醒这个风险。
- 不用 Sci-Hub、LibGen 之类的来源。
- Elsevier / Wiley 的 key 只存在配置文件里，不回显、不写进运行记录、不提交到 git。
  按两家的 TDM 许可，只能用于非商业科研、本人使用。

## 环境

| 项 | 说明 |
|---|---|
| 运行脚本 | `uv run --script <本 skill 目录>/scripts/paperfetch.py <子命令> ...`（依赖由 uv 自动装；Windows 下加 `PYTHONIOENCODING=utf-8`） |
| PDF 校验 | 需要 `pdfinfo`、`pdftotext`（Poppler 或 TeX Live 自带的 xpdf） |
| Zotero | Zotero 7 正在运行；设置 → 高级 →「允许此计算机上的其他应用程序与 Zotero 通讯」已勾选 |
| 配置 | `~/.config/academic-agent/paperfetch.json`：`zotero_collection`（固定的目标分类，名字或完整路径「父分类/子分类」）、`elsevier_api_key`、`elsevier_insttoken`（可空）、`wiley_tdm_token`、`openalex_api_key`（可空）、`prefer_published`（默认 true） |
| 去向 | **Zotero 开着** → 导入 `zotero_collection`；**没开** → 存进用户**这次亲自指定**的文件夹（`fetch --dir`），命名「第一作者 等 - 年份 - 标题.pdf」。下载目录没有默认值，每次都要问用户，不写进配置、不沿用上次的。之后打开 Zotero 重跑 `fetch`，已下载的直接导入，不重新下载 |
| 暂存 PDF | `~/.cache/academic-agent/paperfetch/<run 名>/`，导入 Zotero 后可删 |
| 运行记录 | 项目有约定照约定，否则 `paperfetch-runs/<YYYY-MM-DD>_<英文短名>/`：candidates、selection、results、fetch.log |
| Codex | 沙箱需允许联网，并能访问 `127.0.0.1:23119`（Zotero） |

## 下载渠道

| 出版社 | 渠道 | 条件 |
|---|---|---|
| Elsevier | 官方 Article Retrieval API，先查订阅状态再取 PDF | 配置 `elsevier_api_key`，在校园网内 |
| Wiley | 官方 TDM API | 配置 `wiley_tdm_token` |
| Springer | `link.springer.com/content/pdf/<DOI>.pdf` | 校园网 |
| 任意 | OpenAlex 记录的开放获取 PDF、Crossref 登记的 PDF 链接 | 站点不弹人机验证 |
| ASCE、IEEE、T&F、SAGE、MDPI，以及没配 key 时的 Elsevier / Wiley | 转 `manual`，由用户点 Zotero Connector | 用户在场 |

**优先正式版**：自动渠道只拿到作者稿（Accepted / Submitted Version）时先不导入，存作备用，这篇转 `manual`，
让用户在浏览器里存出版社的正式版。手动轮结束后仍缺的，征得用户同意再 `fetch --allow-am` 用备用作者稿补上
（Zotero 附件标题写 `Accepted Version`，文件夹模式文件名末尾注明）。出版社 API 明确说本校未订阅时，直接用作者稿。
配置 `"prefer_published": false` 则一律直接接受作者稿。

## 流程

### 1. 明确范围
从用户的话里确定：主题、篇数（默认 30）、年份范围、要不要综述。
先运行 `targets` 看 Zotero 开没开（没开会直接报「Zotero 没在运行」）：
- **开着**：目标分类取配置；用户这次另指定时写进 selection.json 的 `collection`。分类要由用户事先在 Zotero 里建好（脚本建不了分类）
- **没开**：问用户这次下载到哪个文件夹，`fetch` 时用 `--dir` 传入。**每次都问**，即使上次给过也不沿用

缺的只问一次，和上面的范围问题合在一条消息里问完。

### 2. 检索
写 4–8 条英文检索词：同义词、上位与下位概念、关键方法名、常见缩写各覆盖一条，不要只换词序。
```
uv run --script .../paperfetch.py search --run <run> --query "..." --query "..." [--from-year 2005] [--per-query 100]
```
找到 2–3 篇核心文献后可以滚雪球：`--cites W123`（引用它的）、`--refs W123`（它引用的），W 号在候选表里。
OpenAlex 无 key 时每天约 100 次请求，每条检索词或每次滚雪球各算 1 次，输出会显示剩余额度。

### 3. 筛选（agent 的核心工作）
分段读完 `<run>/candidates.txt`（按命中次数排序，每篇有年份、被引、期刊、DOI、标题、摘要前 320 字）。
- 只收和主题直接相关的；关键词碰巧撞上的不收
- 兼顾高被引的经典文献、近 5 年的进展和综述；同一组作者的系列文章挑最有代表性的
- 没有 DOI 的条目尽量不收（无法查重，也走不了出版社渠道）

写 `<run>/selection.json`：
```json
{"topic": "主题原话", "collection": "", "tags": [],
 "papers": [{"idx": 12, "doi": "10.1016/...", "why": "一句话入选理由"}]}
```
`idx` 是候选表里的编号；候选表外的论文直接写 `doi`。`collection` 留空则用配置里的固定分类。

### 4. 给用户确认
用表格列出清单（序号、年份、期刊、标题、入选理由），说明总数和各出版社的大致比例，**等用户确认后再下载**。
用户事先说了「直接下」就跳过这一步。

### 5. 查重（Zotero 开着时）
```
uv run --script .../paperfetch.py check --run <run>
```
库里已有且带 PDF 的跳过。Zotero 的 Connector 接口不能修改已有条目，所以库里已有的论文不会被移进新分类；
报告里列出来，请用户自己拖进去。

### 6. 下载导入
```
uv run --script .../paperfetch.py fetch --run <run>                  # Zotero 开着；先加 --dry-run 看计划
uv run --script .../paperfetch.py fetch --run <run> --dir "<用户给的文件夹>"   # Zotero 没开
```
脚本自己判断去向：Zotero 开着就导入分类，没开就存 `--dir` 指定的文件夹（日志第一行会写明）。
Zotero 没开又没给 `--dir` 时脚本会停下，这时去问用户，不要自己挑一个文件夹。没开 Zotero 时没法查重，报告里提醒用户。
30 篇约 5–10 分钟（每个站点的请求间隔几秒到十秒），放后台跑。中断后重跑会接着做：已导入的跳过，暂存目录里已校验的 PDF 直接导入。
每篇的 PDF 都要校验：至少 2 页，前两页能找到标题（词重合 ≥ 0.6）或 DOI。
没通过的文件改名为 `*.rejected.pdf` 留在暂存目录，不导入。

### 7. 核对
- `check --run <run>`：每篇是否在目标分类里（`C`）、带的是正式版（`PDF`）还是只有作者稿（`作者稿`）、库里有没有重复条目（`⚠`）
- 读 `fetch.log`；`import_failed` 的看错误信息
- `*.rejected.pdf` 抽查一两个，确认确实是错文件或预览页，而不是校验规则误判

### 8. 手动补漏（`manual` 状态的论文）
```
uv run --script .../paperfetch.py open --run <run> --page 1      # 默认每批 8 个标签页
```
告诉用户：
1. 在 Zotero 左栏选中目标分类（Connector 会存进当前选中的分类）
2. 在每个打开的标签页点浏览器工具栏的 Zotero Connector 图标（或按 Ctrl+Shift+S），等提示保存了 PDF
3. 遇到人机验证就自己点掉；页面显示无权限的跳过
4. 这一批点完后说一声

用户回复后运行 `check`，状态会变成 `imported_manual`；再开下一批（`--page 2`）。
全部批次做完后，还缺正式版、但有备用作者稿的，问用户要不要用作者稿补上（`fetch --allow-am`）。
Zotero 没开时，请用户在页面上点「下载 PDF」存进下载目录即可，这种情况 `check` 无法核对，报告里如实写明。

### 9. 报告
`status --run <run> --md` 生成状态表，在此基础上写报告：主题和检索词、入选篇数、自动导入 / 手动导入 / 本校无权限 / 库里已有的数量、
仍缺 PDF 的清单和原因、所有非正式版（作者稿）附件、需要用户自己处理的事（拖入分类、补 key 等）。

## 状态

| 状态 | 含义 |
|---|---|
| `imported` | 脚本已下载、校验并导入目标分类 |
| `imported_manual` | 用户用 Connector 保存后，`check` 确认已带 PDF |
| `downloaded` | Zotero 没开，PDF 已存进用户指定的文件夹；打开 Zotero 后重跑 `fetch` 即导入 |
| `in_library` / `in_library_no_pdf` | 库里已有（带 / 不带 PDF），没有重复导入 |
| `manual` | 需要用户在浏览器里保存（包括只找到作者稿的；有备用作者稿时记录里有 `alt`） |
| `no_access` | 出版社 API 明确表示本校未订阅，也没有开放获取版本 |
| `import_failed` | 下载成功但写入 Zotero 失败；PDF 在暂存目录 |

## 常见问题

- **Elsevier 全部是 manual**：没配 key；或日志里出现「这个 API key 不允许取 PDF 格式」，说明这个 key 没有开通 PDF 格式，只能走手动。
- **`no_access` 很多**：确认是否在校园网内（Springer 文章页会显示 "Access provided by <学校>"）。
- **Zotero 本地 API 返回 403**：没勾选「允许其他应用程序通讯」。
- **找不到分类 / 用户要换固定分类**：请用户在 Zotero 左栏点选那个分类，再运行 `targets`，它会读出当前选中分类的完整路径，
  写进配置的 `zotero_collection`（有同名分类时写完整路径，如 `桥梁风工程/待读`）。`targets --all` 列出全部分类，库大时输出很长。
- **库里的条目只挂着作者稿，想换正式版**：Connector 接口不能给已有条目加附件。请用户先在 Zotero 里删掉那条（移到回收站），
  再在打开的页面点 Connector 存正式版；或者手动下载 PDF 拖到该条目上，再删掉作者稿附件。
- **导入时 Zotero 左栏会跳到目标分类**：这是 Connector 接口移动条目时的正常行为。
