# Zotero Paper Fetcher

按研究主题找论文、下载 PDF、放进 Zotero 的 agent skill。**Claude Code 和 Codex 通用**（同一份 `SKILL.md`）。

An agent skill for Claude Code and Codex: give it a research topic, it searches OpenAlex, screens the
results, and downloads the PDFs through legitimate channels (publisher TDM APIs, Springer, open access)
into a Zotero collection. Sites that show bot checks are opened in your own browser for a one-click save.
Must be run inside your institution's network.

## 用起来是什么样

> 找 30 篇近十年桥梁断面气动导纳的论文

agent 会：

1. 写 4–8 条英文检索词，在 OpenAlex 检索，读标题和摘要筛出相关的，列成表给你确认
2. 和 Zotero 库查重，库里已有的不重复导入
3. 能自动下的自动下：Elsevier 官方 API、Wiley TDM API、Springer、开放获取；每个 PDF 都校验页数和标题，然后连同题录导入 Zotero 的固定分类
4. Zotero 没开时改存到指定文件夹，命名「第一作者 等 - 年份 - 标题.pdf」；之后打开 Zotero 重跑，已下载的直接导入
5. 剩下对脚本弹人机验证的站点（ASCE、IEEE 等），每批 8 篇在你的浏览器里打开，你点一下 Zotero Connector 保存
6. 核对每一篇的状态，写报告

## 不做的事

- 不绕过、不代点验证码和人机验证，不伪装浏览器，不用 Sci-Hub 一类的来源
- 单次最多 80 篇。出版社许可禁止系统性批量下载，违规时整个学校的 IP 段都可能被封

## 各出版社的情况

在一所欧洲大学的校园网内实测（2026-10）：

| 出版社 | 脚本能否直接下 | 本 skill 的做法 |
|---|---|---|
| Elsevier | 网页对脚本弹人机验证 | 官方 API（需个人 API key） |
| Wiley | 网页对脚本弹人机验证 | 官方 TDM API（需 TDM token） |
| Springer | 可以 | 直接下载 |
| 开放获取期刊、机构库 | 大多可以 | 直接下载。只找到作者稿时先留作备用，优先让你存正式版，最后才用作者稿补缺 |
| ASCE、IEEE、T&F、SAGE、World Scientific、MDPI | 弹人机验证 | 在你的浏览器打开，你点 Zotero Connector |

## 安装

需要：[uv](https://docs.astral.sh/uv/)、`pdfinfo`/`pdftotext`（Poppler，或 TeX Live 自带）、Zotero 7（可选）。

```powershell
# Windows
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
```
```bash
# macOS / Linux
./install.sh
```

装进 `~/.claude/skills/zotero-paper-fetcher` 和 `~/.codex/skills/zotero-paper-fetcher`，在对话里用 `/zotero-paper-fetcher` 调用，或直接说「找 30 篇 XX 方向的论文」。加 `-Link` / `--link` 改为目录链接，改仓库即生效。

## 配置

`~/.config/academic-agent/paperfetch.json`（安装脚本会生成模板）：

| 键 | 说明 |
|---|---|
| `zotero_collection` | Zotero 开着时导入的固定分类，需事先建好；重名时写完整路径「父分类/子分类」 |
| `prefer_published` | 默认 `true`：只自动导入正式版，作者稿留作备用 |
| `download_dir` | Zotero 没开时的下载目录 |
| `elsevier_api_key` | 在 [dev.elsevier.com](https://dev.elsevier.com/) 用机构邮箱注册后创建 |
| `elsevier_insttoken` | 一般留空；学校另行提供时才填 |
| `wiley_tdm_token` | 登录 Wiley Online Library，在 Text and Data Mining 页面点 "Get a TDM token"，接受许可 |
| `openalex_api_key` | 可空。无 key 时每天约 100 次检索请求 |

Elsevier 和 Wiley 的 key 按各自的 TDM 许可只能用于非商业科研、本人使用。不要把配置文件提交到任何仓库。

Zotero 需要在 设置 → 高级 里勾选「允许此计算机上的其他应用程序与 Zotero 通讯」。

**Codex**：沙箱需要允许联网，并能访问 `127.0.0.1:23119`（Zotero 的本地接口）。

## 单独使用脚本

```bash
S=~/.claude/skills/zotero-paper-fetcher/scripts/paperfetch.py
uv run --script $S search --run runs/demo --query "aerodynamic admittance bridge deck" --from-year 2015
# 编辑 runs/demo/selection.json，格式见 SKILL.md
uv run --script $S check  --run runs/demo
uv run --script $S fetch  --run runs/demo
uv run --script $S open   --run runs/demo --page 1
uv run --script $S status --run runs/demo --md
```

## 已知限制

- Zotero 的 Connector 接口不能修改已有条目，也不能新建分类：库里已有的论文不会被移进新分类，分类要事先建好
- Elsevier API 能否返回 PDF 取决于 key 的权限；不行时 Elsevier 论文走手动
- 只在 Windows 11 + Zotero 7.0 上实测过

## License

MIT
