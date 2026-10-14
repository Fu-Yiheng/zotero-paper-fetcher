# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.27"]
# ///
"""paperfetch — 按主题检索论文，经合法渠道下载 PDF，导入 Zotero 指定分类。

  search   OpenAlex 检索若干关键词（可加引用滚雪球），合并去重 -> <run>/candidates.jsonl + candidates.txt
  targets  列出 Zotero 的分类（需 Zotero 运行）
  check    selection.json 与 Zotero 库查重；手动保存之后也用它核对 -> <run>/check.json
  fetch    下载并导入：Elsevier API / Wiley TDM / Springer 直链 / 开放获取 -> <run>/results.json
  open     把需要手动保存的论文分批在默认浏览器打开（用户点 Zotero Connector 保存）
  status   汇总一次运行；--md 另存 status.md

原则：User-Agent 如实报名，不伪装浏览器，不碰任何验证码 / 人机验证。
对非浏览器请求弹人机验证的站点不发请求，直接转 manual，交给用户自己的浏览器 + Zotero Connector。
"""
from __future__ import annotations

import argparse
import glob
import json
import random
import re
import shutil
import subprocess
import sys
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

UA = "paperfetch/0.1 (Academic Agent personal literature tool; python-httpx)"
CONFIG = Path.home() / ".config" / "academic-agent" / "paperfetch.json"
STAGE_ROOT = Path.home() / ".cache" / "academic-agent" / "paperfetch"
ZOTERO = "http://127.0.0.1:23119"
OPENALEX = "https://api.openalex.org"
CROSSREF = "https://api.crossref.org"
MAX_PER_RUN = 80  # 单次上限，防止误操作变成批量爬取

# 对非浏览器请求一律返回人机验证的站点：不发请求，直接转 manual
BOT_WALLED = {
    "www.sciencedirect.com", "sciencedirect.com", "linkinghub.elsevier.com",
    "onlinelibrary.wiley.com", "ascelibrary.org", "ieeexplore.ieee.org",
    "xplorestaging.ieee.org", "www.tandfonline.com", "journals.sagepub.com", "www.worldscientific.com",
    "pubs.acs.org", "www.icevirtuallibrary.com", "asmedigitalcollection.asme.org", "www.mdpi.com",
}
# 同一主机两次请求的最小间隔（秒），实际再加 0–30% 随机抖动
MIN_INTERVAL = {
    "api.openalex.org": 0.2, "api.crossref.org": 0.5, "api.elsevier.com": 3,
    "api.wiley.com": 10, "link.springer.com": 8,
}
DEFAULT_INTERVAL = 6

PREFIX = {
    "10.1016": "elsevier", "10.1006": "elsevier", "10.1053": "elsevier",
    "10.1002": "wiley", "10.1111": "wiley",
    "10.1007": "springer", "10.1023": "springer", "10.1186": "springer", "10.1140": "springer",
    "10.1109": "ieee", "10.1061": "asce", "10.1080": "tandf", "10.1177": "sage", "10.1142": "worldsci",
}
VERSION_TITLE = {"publishedVersion": "Full Text PDF", "acceptedVersion": "Accepted Version",
                 "submittedVersion": "Submitted Version"}
VERSION_RANK = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}
AM_TITLES = {"Accepted Version", "Submitted Version"}  # 只挂着这类附件的条目不算有正式版
TYPE_MAP = {"journal-article": "journalArticle", "proceedings-article": "conferencePaper",
            "book-chapter": "bookSection", "book": "book", "monograph": "book", "edited-book": "book",
            "posted-content": "preprint", "report": "report", "dissertation": "thesis"}
OA_SELECT = ("id,doi,display_name,publication_year,type,cited_by_count,primary_location,"
             "best_oa_location,locations,authorships,abstract_inverted_index,language")
DONE = ("imported", "imported_manual")


# ------------------------------------------------------------------ 小工具

def now_sql() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def norm_doi(s: str | None) -> str:
    s = (s or "").strip().lower()
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", s)


def norm_title(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)[:150]


def first(v):
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def strip_tags(s: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def load_json(p: Path, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save_json(p: Path, obj) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def load_cfg() -> dict:
    return load_json(CONFIG, {})


def publisher_of(doi: str) -> str:
    return PREFIX.get(doi.split("/", 1)[0], "other") if doi else "other"


def pretty_name(cr: dict | None, p: dict, version: str = "publishedVersion") -> str:
    """「第一作者 等 - 年份 - 标题.pdf」，与 Zotero Attanger 的常用命名一致；作者稿在末尾注明。"""
    au = (cr or {}).get("author") or []
    if au:
        name = au[0].get("family") or au[0].get("name") or ""
        many = len(au) > 1
    else:
        names = [x for x in (p.get("authors") or "").replace(" et al.", "").split("; ") if x]
        name = names[0].split()[-1] if names else ""
        many = len(names) > 1 or "et al." in (p.get("authors") or "")
    parts = [name + (" 等" if many and name else ""), str(p.get("year") or ""), p.get("title") or p.get("doi", "")]
    s = re.sub(r'[\\/:*?"<>|\s]+', " ", " - ".join(x for x in parts if x)).strip()
    tail = f" ({VERSION_TITLE[version]})" if version in VERSION_TITLE and version != "publishedVersion" else ""
    return s[:150].rstrip(" .") + tail + ".pdf"


def paper_key(p: dict) -> str:
    return p.get("doi") or p.get("openalex") or norm_title(p.get("title"))[:80]


class Log:
    def __init__(self, path: Path | None = None):
        self.f = open(path, "a", encoding="utf-8") if path else None

    def __call__(self, *parts):
        s = " ".join(str(x) for x in parts)
        print(s, flush=True)
        if self.f:
            self.f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {s}\n")
            self.f.flush()


# ------------------------------------------------------------------ 网络

class Blocked(Exception):
    """站点要求人机验证（或在已知名单里）。不重试、不绕过。"""


class Net:
    def __init__(self):
        self.c = httpx.Client(headers={"User-Agent": UA}, follow_redirects=True,
                              timeout=httpx.Timeout(90, connect=20))
        self.last: dict[str, float] = {}
        self.blocked: set[str] = set()
        self.flags: dict[str, str] = {}

    def get(self, url: str, **kw) -> httpx.Response:
        host = urlparse(url).hostname or ""
        gap = MIN_INTERVAL.get(host, DEFAULT_INTERVAL)
        dt = time.time() - self.last.get(host, 0)
        if dt < gap:
            time.sleep(gap - dt + random.uniform(0, gap * 0.3))
        try:
            return self.c.get(url, **kw)
        finally:
            self.last[host] = time.time()


def is_challenge(r: httpx.Response) -> bool:
    if r.status_code in (418, 429):
        return True
    if r.status_code in (403, 503):
        ct = r.headers.get("content-type", "")
        if "html" in ct or "text" in ct:
            t = r.text[:30000].lower()
            return any(k in t for k in ("challenge-platform", "just a moment", "cf-chl", "captcha",
                                        "are you a robot", "verify you are human", "<title>access denied"))
    return False


def get_pdf(net: Net, url: str, headers: dict | None = None):
    """返回 (pdf_bytes, 最终 URL)；不是 PDF 时返回 (None, 原因)；遇到人机验证抛 Blocked。"""
    host = urlparse(url).hostname or ""
    if host in BOT_WALLED or host in net.blocked:
        raise Blocked(host)
    r = net.get(url, headers=headers or {})
    final = urlparse(str(r.url)).hostname or ""
    if is_challenge(r):
        net.blocked.update({host, final})
        raise Blocked(final)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    if not r.content.startswith(b"%PDF"):
        return None, f"不是 PDF（{r.headers.get('content-type', '?')[:40]}）"
    return r.content, str(r.url)


def crossref(net: Net, doi: str) -> dict | None:
    try:
        r = net.get(f"{CROSSREF}/works/{quote(doi)}")
    except httpx.HTTPError:
        return None
    return r.json().get("message") if r.status_code == 200 else None


# ------------------------------------------------------------------ OpenAlex

def oa_params(cfg: dict, **kw) -> dict:
    if cfg.get("openalex_api_key"):
        kw["api_key"] = cfg["openalex_api_key"]
    return kw


def oa_abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    pos = [(i, w) for w, idxs in inv.items() for i in idxs]
    return " ".join(w for _, w in sorted(pos))


def oa_record(w: dict) -> dict:
    prim = w.get("primary_location") or {}
    src = prim.get("source") or {}
    locs, seen = [], set()
    for loc in [w.get("best_oa_location") or {}] + (w.get("locations") or []):
        u = loc.get("pdf_url")
        if not u or u in seen:
            continue
        seen.add(u)
        locs.append({"url": u, "version": loc.get("version") or "", "is_oa": bool(loc.get("is_oa")),
                     "source": (loc.get("source") or {}).get("display_name") or "",
                     "host": urlparse(u).hostname or ""})
    auth = [(a.get("author") or {}).get("display_name", "") for a in (w.get("authorships") or [])]
    return {
        "openalex": (w.get("id") or "").rsplit("/", 1)[-1],
        "doi": norm_doi(w.get("doi")),
        "title": w.get("display_name") or "",
        "year": w.get("publication_year"),
        "type": w.get("type"),
        "venue": src.get("display_name") or "",
        "cited_by": w.get("cited_by_count") or 0,
        "authors": "; ".join(auth[:3]) + (" et al." if len(auth) > 3 else ""),
        "abstract": oa_abstract(w.get("abstract_inverted_index")),
        "landing": prim.get("landing_page_url") or "",
        "oa": locs,
    }


def load_cands(run: Path) -> list[dict]:
    p = run / "candidates.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def write_cands(run: Path, cands: list[dict]) -> None:
    cands = sorted(cands, key=lambda c: c["idx"])
    (run / "candidates.jsonl").write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in cands) + "\n", encoding="utf-8")
    lines = []
    for c in sorted(cands, key=lambda c: (-len(c["hits"]), c["best_rank"], -c["cited_by"])):
        oa = "OA" if c["oa"] else "--"
        lines.append(f"[{c['idx']}] {c['year']} | {c['cited_by']} cites | {oa} | {c['venue'][:48]} | "
                     f"{c['doi'] or c['openalex']} | hits {len(c['hits'])}")
        lines.append(f"    {c['title']}")
        if c["abstract"]:
            lines.append(f"    {c['abstract'][:320]}")
    (run / "candidates.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def cmd_search(a) -> None:
    run = Path(a.run)
    run.mkdir(parents=True, exist_ok=True)
    cfg, net = load_cfg(), Net()
    cands = load_cands(run)
    by = {paper_key(c): c for c in cands}
    nxt = max((c["idx"] for c in cands), default=0) + 1
    base = []
    if a.from_year:
        base.append(f"from_publication_date:{a.from_year}-01-01")
    if a.to_year:
        base.append(f"to_publication_date:{a.to_year}-12-31")
    if a.types:
        base.append("type:" + a.types.replace(",", "|"))
    if a.lang:
        base.append(f"language:{a.lang}")
    jobs = [(f"q:{q}", {"search": q}, base) for q in a.query]
    jobs += [(f"cites:{w}", {}, base + [f"cites:{w}"]) for w in a.cites]
    jobs += [(f"refs:{w}", {}, base + [f"cited_by:{w}"]) for w in a.refs]
    if not jobs:
        raise SystemExit("至少给一个 --query / --cites / --refs")
    for label, extra, flt in jobs:
        params = oa_params(cfg, per_page=a.per_query, select=OA_SELECT, **extra)
        if flt:
            params["filter"] = ",".join(flt)
        r = net.get(f"{OPENALEX}/works", params=params)
        if r.status_code != 200:
            print(f"!! {label}: HTTP {r.status_code} {r.text[:200]}")
            continue
        res, new = r.json().get("results", []), 0
        for rank, w in enumerate(res, 1):
            rec = oa_record(w)
            k = paper_key(rec)
            if k in by:
                c = by[k]
                if label not in c["hits"]:
                    c["hits"].append(label)
                c["best_rank"] = min(c["best_rank"], rank)
            else:
                rec.update(idx=nxt, hits=[label], best_rank=rank)
                by[k] = rec
                nxt += 1
                new += 1
        print(f"{label}: {len(res)} 条，新增 {new}（OpenAlex 今日剩余额度 "
              f"{r.headers.get('X-RateLimit-Remaining', '?')}）")
    write_cands(run, list(by.values()))
    print(f"候选共 {len(by)} 篇 -> {run / 'candidates.txt'}")


# ------------------------------------------------------------------ Zotero

class Zotero:
    def __init__(self):
        self.c = httpx.Client(base_url=ZOTERO, headers={"User-Agent": UA}, timeout=120)

    def running(self) -> bool:
        try:
            return self.c.get("/connector/ping", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False

    def require(self) -> "Zotero":
        if not self.running():
            raise SystemExit("Zotero 没在运行（127.0.0.1:23119）。先打开 Zotero。")
        return self

    def targets(self) -> dict:
        r = self.c.post("/connector/getSelectedCollection", json={})
        r.raise_for_status()
        return r.json()

    def resolve_target(self, name: str) -> dict:
        """按 ID（C123）、完整路径（父分类/子分类）或名字找分类。"""
        stack: list[str] = []
        for t in (ts := self.targets().get("targets", [])):
            lvl = t.get("level", 0)
            stack = stack[:lvl] + [t["name"]]
            t["path"] = "/".join(stack[1:])  # 去掉文库名
        want = re.sub(r"\s*/\s*", "/", name.strip().strip("/"))
        if re.fullmatch(r"[LC]\d+", want):
            hit = [t for t in ts if t["id"] == want]
        elif "/" in want:
            hit = [t for t in ts if t["path"] == want]
        else:
            hit = [t for t in ts if t["name"] == want]
        if not hit:
            raise SystemExit(f"Zotero 里找不到分类「{name}」。先在 Zotero 里建好，或用 targets 查看准确名字。")
        if len(hit) > 1:
            raise SystemExit(f"有 {len(hit)} 个同名分类「{name}」：{[t['path'] for t in hit]}，改用完整路径")
        return hit[0]

    def save(self, item: dict, pdf: bytes, pdf_url: str, att_title: str, target: dict, tags: list[str]) -> str:
        """建条目、挂 PDF、移到目标分类。前两步失败抛异常；第三步失败只返回警告（条目已存在，不能重做）。"""
        sid = uuid.uuid4().hex
        item = dict(item, id=f"i{sid[:10]}")
        r = self.c.post("/connector/saveItems", json={"sessionID": sid, "uri": item.get("url", ""), "items": [item]})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"saveItems HTTP {r.status_code}: {r.text[:200]}")
        meta = {"id": f"a{sid[:10]}", "parentItemID": item["id"], "title": att_title,
                "url": pdf_url, "sessionID": sid}
        r = self.c.post("/connector/saveAttachment", content=pdf,
                        headers={"Content-Type": "application/pdf", "X-Metadata": json.dumps(meta)})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"saveAttachment HTTP {r.status_code}: {r.text[:200]}")
        # tags 必须是逗号分隔的字符串：Zotero 对它调用 .trim()，传数组会 500
        r = self.c.post("/connector/updateSession",
                        json={"sessionID": sid, "target": target["id"], "tags": ", ".join(tags)})
        if r.status_code not in (200, 201):
            return f"条目和 PDF 已保存，但移到目标分类失败（updateSession HTTP {r.status_code}），留在保存时 Zotero 左栏选中的分类"
        return ""

    def api_all(self, path: str, params: dict | None = None) -> list[dict]:
        out, start = [], 0
        while True:
            r = self.c.get(f"/api/users/0/{path}", params=dict(params or {}, limit=100, start=start, format="json"))
            if r.status_code != 200:
                raise SystemExit(f"Zotero 本地 API {path} 返回 HTTP {r.status_code}。确认 Zotero 设置 → 高级 → "
                                 "「允许此计算机上的其他应用程序与 Zotero 通讯」已勾选。")
            batch = r.json()
            out += batch
            start += 100
            if not batch or start >= int(r.headers.get("Total-Results", 0)):
                return out

    def library_index(self):
        items = self.api_all("items/top")
        atts = self.api_all("items", {"itemType": "attachment"})
        colls = self.api_all("collections")
        pdf_parents, vor_parents = set(), set()
        for x in atts:
            d = x["data"]
            if d.get("contentType") == "application/pdf" and d.get("parentItem"):
                pdf_parents.add(d["parentItem"])
                if d.get("title") not in AM_TITLES:
                    vor_parents.add(d["parentItem"])
        by_doi: dict[str, list] = {}
        by_title: dict[str, list] = {}
        for it in items:
            d = it["data"]
            if d.get("itemType") in ("attachment", "note", "annotation"):
                continue
            rec = {"key": d["key"], "title": d.get("title", ""), "has_pdf": d["key"] in pdf_parents,
                   "has_vor": d["key"] in vor_parents, "collections": d.get("collections", [])}
            m = re.search(r"^DOI:\s*(\S+)", d.get("extra", ""), re.M | re.I)
            doi = norm_doi(d.get("DOI") or (m.group(1) if m else ""))
            if doi:
                by_doi.setdefault(doi, []).append(rec)
            nt = norm_title(d.get("title"))
            if nt:
                by_title.setdefault(nt, []).append(rec)
        return by_doi, by_title, colls


def library_check(z: Zotero, papers: list[dict], coll_path: str | None) -> dict:
    """coll_path 是 resolve_target 给出的完整路径（父分类/子分类）。"""
    by_doi, by_title, colls = z.library_index()
    info = {c["data"]["key"]: (c["data"]["name"], c["data"].get("parentCollection") or None) for c in colls}

    def path_of(key):
        parts = []
        while key and key in info:
            name, key = info[key]
            parts.append(name)
        return "/".join(reversed(parts))

    ckeys = {k for k in info if coll_path and path_of(k) == coll_path}
    out = {}
    for p in papers:
        hits = by_doi.get(p["doi"], []) if p.get("doi") else []
        if not hits and p.get("title"):
            hits = by_title.get(norm_title(p["title"]), [])
        out[paper_key(p)] = {"in_library": bool(hits), "has_pdf": any(h["has_pdf"] for h in hits),
                             "has_vor": any(h["has_vor"] for h in hits),
                             "in_collection": any(set(h["collections"]) & ckeys for h in hits),
                             "keys": [h["key"] for h in hits]}
    return out


def zotero_item(cr: dict | None, p: dict, tags: list[str]) -> dict:
    cr = cr or {}
    ztype = TYPE_MAP.get(cr.get("type"), "journalArticle")
    doi = p.get("doi", "")
    creators = []
    for au in cr.get("author") or []:
        if au.get("family"):
            creators.append({"firstName": au.get("given", ""), "lastName": au["family"], "creatorType": "author"})
        elif au.get("name"):
            creators.append({"lastName": au["name"], "firstName": "", "creatorType": "author", "fieldMode": 1})
    if not creators and p.get("authors"):
        for nm in p["authors"].replace(" et al.", "").split("; "):
            *given, family = nm.split()
            creators.append({"firstName": " ".join(given), "lastName": family, "creatorType": "author"})
    parts = ((cr.get("issued") or cr.get("published") or {}).get("date-parts") or [[None]])[0]
    date = "-".join(str(x) if i == 0 else f"{x:02d}" for i, x in enumerate(parts) if x) if parts and parts[0] \
        else str(p.get("year") or "")
    abstract = strip_tags(cr.get("abstract")) or p.get("abstract", "")
    abstract = re.sub(r"^abstract\s*[:.]?\s*", "", abstract, flags=re.I)
    container = strip_tags(first(cr.get("container-title"))) or p.get("venue", "")
    pages = cr.get("page") or cr.get("article-number") or ""
    item = {
        "itemType": ztype,
        "title": strip_tags(first(cr.get("title"))) or p.get("title", ""),
        "creators": creators,
        "date": date,
        "url": f"https://doi.org/{doi}" if doi else p.get("landing", ""),
        "abstractNote": abstract,
        "language": cr.get("language", ""),
        "libraryCatalog": "Crossref" if cr else "OpenAlex",
        "accessDate": now_sql(),
        "tags": [{"tag": t} for t in tags],
        "attachments": [],
        "notes": [],
    }
    if ztype == "journalArticle":
        item.update(publicationTitle=container, journalAbbreviation=first(cr.get("short-container-title")),
                    volume=cr.get("volume", ""), issue=cr.get("issue", ""), pages=pages,
                    ISSN=first(cr.get("ISSN")), DOI=doi)
    elif ztype == "conferencePaper":
        item.update(proceedingsTitle=container, conferenceName=(cr.get("event") or {}).get("name", ""),
                    publisher=cr.get("publisher", ""), pages=pages, ISBN=first(cr.get("ISBN")), DOI=doi)
    elif ztype == "bookSection":
        item.update(bookTitle=container, publisher=cr.get("publisher", ""), pages=pages,
                    ISBN=first(cr.get("ISBN")), extra=f"DOI: {doi}" if doi else "")
    elif ztype == "book":
        item.update(publisher=cr.get("publisher", ""), ISBN=first(cr.get("ISBN")), extra=f"DOI: {doi}" if doi else "")
    elif ztype == "preprint":
        item.update(repository=cr.get("publisher", ""), DOI=doi)
    elif doi:
        item["extra"] = f"DOI: {doi}"
    return {k: v for k, v in item.items() if v not in ("", None)}


# ------------------------------------------------------------------ 下载渠道

def ch_direct(net: Net, url: str, version: str) -> dict:
    pdf, info = get_pdf(net, url)
    return {"pdf": pdf, "src": info if pdf else url, "version": version, "note": None if pdf else info}


def ch_elsevier(net: Net, cfg: dict, doi: str) -> dict:
    key = cfg.get("elsevier_api_key")
    if not key:
        return {"note": "未配置 elsevier_api_key"}
    if net.flags.get("elsevier"):
        return {"note": net.flags["elsevier"]}
    h = {"X-ELS-APIKey": key}
    if cfg.get("elsevier_insttoken"):
        h["X-ELS-Insttoken"] = cfg["elsevier_insttoken"]
    r = net.get(f"https://api.elsevier.com/content/article/entitlement/doi/{doi}",
                headers=dict(h, Accept="application/json"))
    if r.status_code in (401, 403):
        net.flags["elsevier"] = f"API key 被拒（HTTP {r.status_code}）"
        return {"note": net.flags["elsevier"]}
    try:
        ent = r.json()["entitlement-response"]["document-entitlement"]["entitled"]
    except Exception:
        ent = None
    if ent is False:
        return {"note": "Elsevier 显示本校未订阅", "no_access": True}
    url = f"https://api.elsevier.com/content/article/doi/{doi}?httpAccept=application/pdf"
    r = net.get(url, headers=dict(h, Accept="application/pdf"))
    if r.status_code == 200 and r.content.startswith(b"%PDF"):
        return {"pdf": r.content, "src": url, "version": "publishedVersion"}
    if "restricted" in r.text[:500].lower():
        net.flags["elsevier"] = "这个 API key 不允许取 PDF 格式"
        return {"note": net.flags["elsevier"]}
    return {"note": f"Elsevier API HTTP {r.status_code} {r.text[:120]}"}


def ch_wiley(net: Net, cfg: dict, doi: str) -> dict:
    tok = cfg.get("wiley_tdm_token")
    if not tok:
        return {"note": "未配置 wiley_tdm_token"}
    if net.flags.get("wiley"):
        return {"note": net.flags["wiley"]}
    url = f"https://api.wiley.com/onlinelibrary/tdm/v1/articles/{quote(doi, safe='')}"
    r = net.get(url, headers={"Wiley-TDM-Client-Token": tok})
    if r.status_code == 200 and r.content.startswith(b"%PDF"):
        return {"pdf": r.content, "src": url, "version": "publishedVersion"}
    if r.status_code == 401:
        net.flags["wiley"] = "Wiley TDM token 无效"
        return {"note": net.flags["wiley"]}
    if r.status_code == 403:
        return {"note": "Wiley 显示本校未订阅", "no_access": True}
    return {"note": f"Wiley TDM HTTP {r.status_code}"}


def oa_locations(net: Net, cfg: dict, p: dict) -> list[dict]:
    if "oa" in p:
        return p["oa"]
    if not p.get("doi"):
        return []
    try:
        r = net.get(f"{OPENALEX}/works/doi:{p['doi']}", params=oa_params(cfg, select=OA_SELECT))
        return oa_record(r.json())["oa"] if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return []


def validate(path: Path, title: str, doi: str) -> dict:
    def tool(name):
        p = shutil.which(name)
        if p:
            return p
        hits = sorted(glob.glob(rf"C:\texlive\*\bin\win*\{name}.exe"))  # TeX Live 自带 xpdf 工具
        return hits[-1] if hits else None

    def words(s):
        return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) >= 3}

    info: dict = {"pages": None, "title_score": None, "doi_found": False}
    if pi := tool("pdfinfo"):
        out = subprocess.run([pi, str(path)], capture_output=True).stdout.decode("utf-8", "replace")
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        info["pages"] = int(m.group(1)) if m else None
    text = ""
    if pt := tool("pdftotext"):
        text = subprocess.run([pt, "-f", "1", "-l", "2", "-enc", "UTF-8", str(path), "-"],
                              capture_output=True).stdout.decode("utf-8", "replace")
    tw = words(title or "")
    if tw:
        info["title_score"] = round(len(tw & words(text)) / len(tw), 2)
    info["doi_found"] = bool(doi) and doi in text.lower()
    if (info["pages"] or 0) < 2:
        info["why"] = f"只有 {info['pages']} 页"
    elif (info["title_score"] or 0) < 0.6 and not info["doi_found"]:
        info["why"] = f"前两页找不到标题（重合 {info['title_score']}）"
    info["ok"] = "why" not in info
    return info


def acquire(net: Net, cfg: dict, p: dict, cr: dict | None, rec: dict, pdf_path: Path, allow_am: bool) -> bool:
    """按渠道顺序找 PDF。优先正式版：作者稿只留作备用，除非 allow_am 或出版社 API 明确说本校没订阅。"""
    doi, pub = p.get("doi", ""), publisher_of(p.get("doi", ""))
    title = p.get("title") or strip_tags(first((cr or {}).get("title")))
    chans = []
    if pub == "elsevier":
        chans.append(("elsevier-api", lambda: ch_elsevier(net, cfg, doi)))
    if pub == "wiley":
        chans.append(("wiley-tdm", lambda: ch_wiley(net, cfg, doi)))
    if pub == "springer":
        chans.append(("springer", lambda: ch_direct(net, f"https://link.springer.com/content/pdf/{doi}.pdf",
                                                    "publishedVersion")))
    urls = set()
    for loc in sorted(oa_locations(net, cfg, p), key=lambda x: VERSION_RANK.get(x["version"], 3)):
        if loc["url"] not in urls:
            urls.add(loc["url"])
            chans.append((f"oa:{loc['host']}", lambda loc=loc: ch_direct(net, loc["url"], loc["version"] or "publishedVersion")))
    for link in (cr or {}).get("link") or []:
        u, host = link.get("URL", ""), urlparse(link.get("URL", "")).hostname or ""
        if link.get("content-type") in ("application/pdf", "unspecified") and u not in urls \
                and host not in ("api.elsevier.com", "api.wiley.com", "link.springer.com"):
            urls.add(u)
            chans.append((f"crossref:{host}", lambda u=u: ch_direct(net, u, "publishedVersion")))
    no_access = False
    alt = None  # 第一个通过校验的作者稿
    am_path = pdf_path.with_name(f"{pdf_path.stem}.am.pdf")
    for name, fn in chans:
        try:
            res = fn()
        except Blocked as e:
            rec["tries"].append(f"{name}: 需人机验证（{e}），不自动处理")
            continue
        except httpx.HTTPError as e:
            rec["tries"].append(f"{name}: 网络错误 {type(e).__name__}")
            continue
        no_access |= bool(res.get("no_access"))
        if not res.get("pdf"):
            rec["tries"].append(f"{name}: {res.get('note')}")
            continue
        published = res["version"] == "publishedVersion"
        if not published and alt:
            continue  # 已经有一份作者稿备用了
        dst = pdf_path if published else am_path
        dst.write_bytes(res["pdf"])
        v = validate(dst, title, doi)
        if not v["ok"]:
            rec["tries"].append(f"{name}: 校验未通过（{v['why']}）")
            if pub == "elsevier" and name == "elsevier-api" and v["pages"] == 1:
                no_access = True  # 未订阅时 Elsevier 只给首页预览
            dst.replace(pdf_path.with_name(f"{pdf_path.stem}.{name.split(':')[0]}.rejected.pdf"))
            continue
        got = {"channel": name, "pdf_src": res["src"], "version": res["version"],
               "pages": v["pages"], "title_score": v["title_score"]}
        if not published:
            alt = dict(got, path=str(am_path))
            rec["tries"].append(f"{name}: 找到作者稿（{res['version']}），先留作备用")
            continue
        rec.update(got, pdf_ok=True, pdf_path=str(pdf_path))
        rec["tries"].append(f"{name}: OK {v['pages']} 页")
        return True
    if alt and (allow_am or no_access):
        shutil.copyfile(alt["path"], pdf_path)
        rec.update({k: v for k, v in alt.items() if k != "path"}, pdf_ok=True, pdf_path=str(pdf_path))
        rec["tries"].append("拿不到正式版，改用作者稿" + ("（本校未订阅）" if no_access else "（--allow-am）"))
        return True
    rec["status"] = "no_access" if no_access else "manual"
    if alt:
        rec["alt"] = alt
    return False


# ------------------------------------------------------------------ 命令

def load_selection(run: Path):
    sel = load_json(run / "selection.json", None)
    if not sel:
        raise SystemExit(f"缺少 {run / 'selection.json'}")
    cands = load_cands(run)
    by_idx = {c["idx"]: c for c in cands}
    by_doi = {c["doi"]: c for c in cands if c["doi"]}
    papers = []
    for p in sel["papers"]:
        c = by_idx.get(p.get("idx")) or {}
        doi = norm_doi(p.get("doi")) or c.get("doi", "")
        if c and p.get("doi") and c.get("doi") and c["doi"] != doi:
            print(f"!! idx {p['idx']} 的 DOI 和候选表不一致：{doi} vs {c['doi']}，以 DOI 为准")
            c = {}
        c = c or by_doi.get(doi, {})
        papers.append({**c, **{k: v for k, v in p.items() if v not in (None, "")}, "doi": doi})
    return sel, papers


def cmd_targets(a) -> None:
    d = Zotero().require().targets()
    ts = d.get("targets", [])
    stack: list[str] = []
    for t in ts:
        stack = stack[:t.get("level", 0)] + [t["name"]]
        t["path"] = "/".join(stack[1:])
    sel = next((t for t in ts if t["id"] == f"C{d.get('id')}"), None)
    if sel:
        same = sum(t["name"] == sel["name"] for t in ts)
        print(f"Zotero 当前选中：{sel['path']}（{sel['id']}；同名分类 {same} 个，"
              f"配置里写 {'名字' if same == 1 else '完整路径'} 即可）")
    else:
        print(f"Zotero 当前选中：{d.get('name')}（不是分类）")
    print(f"配置的固定分类：{load_cfg().get('zotero_collection') or '（未设置）'}；共 {len(ts) - 1} 个分类")
    if a.all:
        for t in ts:
            print(f"{'  ' * t.get('level', 0)}{t['id']}  {t['name']}")


def cmd_check(a) -> None:
    run = Path(a.run)
    sel, papers = load_selection(run)
    collection = a.collection or sel.get("collection") or load_cfg().get("zotero_collection")
    z = Zotero().require()
    coll_path = z.resolve_target(collection)["path"] if collection else None
    lib = library_check(z, papers, coll_path)
    collection = coll_path
    results = load_json(run / "results.json", {})
    for p in papers:
        k, L = paper_key(p), lib[paper_key(p)]
        r = results.get(k)
        if r and L["has_pdf"] and r.get("status") == "import_failed":
            r["status"] = "imported"  # 条目其实已建好（多半是最后一步移分类失败）
        elif r and L["has_vor"] and r.get("status") in ("manual", "no_access"):
            r["status"] = "imported_manual"
        if r:
            r.update(in_collection=L["in_collection"], zotero_keys=L["keys"])
    if results:
        save_json(run / "results.json", results)
    save_json(run / "check.json", lib)
    n_in = sum(v["in_library"] for v in lib.values())
    n_pdf = sum(v["has_pdf"] for v in lib.values())
    n_col = sum(v["in_collection"] for v in lib.values())
    print(f"{len(papers)} 篇：库中已有 {n_in}（其中有 PDF {n_pdf}），在分类「{collection}」里 {n_col}")
    for i, p in enumerate(papers, 1):
        L = lib[paper_key(p)]
        flag = ("PDF" if L["has_vor"] else "作者稿" if L["has_pdf"] else "无PDF") if L["in_library"] else "-"
        r = results.get(paper_key(p), {})
        dup = f"  ⚠ 库里有 {len(L['keys'])} 条重复" if len(L["keys"]) > 1 else ""
        print(f"{i:>3}. [{flag:^5}] {'C' if L['in_collection'] else ' '} {r.get('status', ''):<16} "
              f"{p.get('year') or r.get('year') or ''} {(p.get('title') or r.get('title') or p.get('doi', ''))[:70]}{dup}")


def cmd_fetch(a) -> None:
    run = Path(a.run)
    sel, papers = load_selection(run)
    if a.only:
        want = {norm_doi(x) for x in a.only.split(",")}
        papers = [p for p in papers if p.get("doi") in want]
    if len(papers) > a.max:
        raise SystemExit(f"{len(papers)} 篇超过单次上限 {a.max}。分批跑，或确认后加 --max。")
    cfg, net, log = load_cfg(), Net(), Log(run / "fetch.log")
    stage = STAGE_ROOT / run.name
    stage.mkdir(parents=True, exist_ok=True)
    results = load_json(run / "results.json", {})
    collection = a.collection or sel.get("collection") or cfg.get("zotero_collection")
    tags = sel.get("tags", [])
    z = target = out_dir = None
    lib: dict = {}
    if not a.no_zotero and Zotero().running():  # Zotero 开着：导入固定分类
        z = Zotero()
        if not collection:
            raise SystemExit("需要 Zotero 分类：配置 zotero_collection，或 --collection")
        target = z.resolve_target(collection)
        lib = library_check(z, papers, target["path"])
        dest = f"Zotero「{target['path']}」"
    else:  # Zotero 没开：存到用户这次指定的文件夹（每次都由用户指定，不读配置、不沿用上次的）
        if not a.dir and not a.dry_run:
            raise SystemExit("Zotero 没在运行，PDF 只能存到文件夹。请问用户这次下载到哪个文件夹"
                             "（每次都要问，不要沿用上次的），再加 --dir <文件夹> 重跑。")
        out_dir = Path(a.dir).expanduser().resolve() if a.dir else None
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)
        dest = f"文件夹 {out_dir}（Zotero 未运行）" if out_dir else "Zotero 未运行，尚未指定文件夹（仅预览）"
    allow_am = a.allow_am or not cfg.get("prefer_published", True)
    keys = {k: bool(cfg.get(k)) for k in ("elsevier_api_key", "wiley_tdm_token")}
    log(f"== fetch {len(papers)} 篇 -> {dest}；已配置 {keys}；{'接受作者稿' if allow_am else '只要正式版，作者稿留作备用'}")
    counts: dict[str, int] = {}
    for i, p in enumerate(papers, 1):
        k = paper_key(p)
        prev = results.get(k, {})
        head = f"[{i}/{len(papers)}] {p.get('title', '')[:60]}"
        saved = Path(prev["saved_to"]) if prev.get("saved_to") else None
        # 存过、而且就存在这次指定的文件夹里才算完成；换了文件夹就从暂存目录再复制一份
        done = prev.get("status") in DONE or bool(
            out_dir and prev.get("status") == "downloaded" and saved and saved.exists()
            and saved.parent.resolve() == out_dir)
        if done and not a.retry:
            counts[prev["status"]] = counts.get(prev["status"], 0) + 1
            continue
        L = lib.get(k, {})
        cr = crossref(net, p["doi"]) if p.get("doi") and not L.get("in_library") and not a.dry_run else None
        if cr:  # 只给了 DOI 的论文，用 Crossref 补题录
            p.setdefault("title", strip_tags(first(cr.get("title"))))
            p.setdefault("venue", strip_tags(first(cr.get("container-title"))))
            p.setdefault("year", (((cr.get("issued") or {}).get("date-parts") or [[None]])[0] or [None])[0])
            head = f"[{i}/{len(papers)}] {p.get('title', '')[:60]}"
        rec = {"doi": p.get("doi", ""), "title": p.get("title", ""), "year": p.get("year"),
               "venue": p.get("venue", ""), "publisher": publisher_of(p.get("doi", "")), "why": p.get("why", ""),
               "landing": f"https://doi.org/{p['doi']}" if p.get("doi") else p.get("landing", ""),
               "tries": [], "time": now_sql()}
        if L.get("in_library"):
            rec.update(status="in_library" if L["has_pdf"] else "in_library_no_pdf",
                       zotero_keys=L["keys"], in_collection=L["in_collection"])
        elif a.dry_run:
            rec["status"] = "planned"
        else:
            pdf_path = stage / f"{safe_name(k)}.pdf"
            alt = prev.get("alt") or {}
            if prev.get("pdf_ok") and pdf_path.exists():
                rec.update({x: prev[x] for x in ("channel", "pdf_src", "version", "pdf_ok", "pages", "title_score",
                                                  "pdf_path") if x in prev})
                rec["tries"] = prev.get("tries", []) + ["沿用暂存目录里的 PDF"]
                ok = True
            elif allow_am and alt and Path(alt["path"]).exists():
                shutil.copyfile(alt["path"], pdf_path)
                rec.update({k: v for k, v in alt.items() if k != "path"}, pdf_ok=True, pdf_path=str(pdf_path))
                rec["tries"] = prev.get("tries", []) + ["改用暂存的作者稿（--allow-am）"]
                ok = True
            else:
                ok = acquire(net, cfg, p, cr, rec, pdf_path, allow_am)
            if ok and z:
                try:
                    warn = z.save(zotero_item(cr, p, tags), pdf_path.read_bytes(), rec["pdf_src"],
                                  VERSION_TITLE.get(rec["version"], "Full Text PDF"), target, tags)
                    rec["status"] = "imported"
                    if warn:
                        rec["warning"] = warn
                        rec["tries"].append(warn)
                except Exception as e:  # noqa: BLE001
                    rec.update(status="import_failed", error=str(e)[:300])
            elif ok:
                dst = out_dir / pretty_name(cr, p, rec.get("version", "publishedVersion"))
                if not dst.exists():
                    shutil.copyfile(pdf_path, dst)
                rec.update(status="downloaded", saved_to=str(dst))
        if not a.dry_run:  # 预览不改运行记录，否则会冲掉已导入的状态
            results[k] = rec
            save_json(run / "results.json", results)
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
        log(f"{head}  -> {rec['status']}  {rec.get('channel', '')}  {'; '.join(rec['tries'][-2:])}")
    log(f"== 完成：{counts}")
    if net.blocked:
        log(f"   需人机验证、已转手动的站点：{sorted(net.blocked)}")
    for k, v in net.flags.items():
        log(f"   {k}: {v}")


def cmd_open(a) -> None:
    run = Path(a.run)
    results = load_json(run / "results.json", {})
    pending = [r for r in results.values() if r.get("status") == "manual"]
    if not pending:
        print("没有待手动保存的论文。")
        return
    chunk = pending[(a.page - 1) * a.batch: a.page * a.batch]
    pages = (len(pending) + a.batch - 1) // a.batch
    print(f"待手动保存 {len(pending)} 篇，第 {a.page}/{pages} 批，打开 {len(chunk)} 个标签页：")
    for r in chunk:
        print(f"  - {r.get('year', '')} {r['title'][:80]}  {r['landing']}")
        if not a.list_only:
            webbrowser.open_new_tab(r["landing"])  # 用户自己的默认浏览器
            time.sleep(1.5)


def cmd_status(a) -> None:
    run = Path(a.run)
    results = load_json(run / "results.json", {})
    counts: dict[str, int] = {}
    for r in results.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"{len(results)} 篇：{counts}")
    rows = ["| # | 年 | 出版社 | 标题 | 状态 | 渠道 / 说明 |", "|---|---|---|---|---|---|"]
    for i, r in enumerate(results.values(), 1):
        note = r.get("channel") or (r.get("tries") or [""])[-1]
        if r.get("version") and r["version"] != "publishedVersion":
            note += f"（{r['version']}）"
        rows.append(f"| {i} | {r.get('year', '')} | {r.get('publisher', '')} | {r['title'][:80]} | "
                    f"{r['status']} | {note} |")
    text = "\n".join(rows)
    print(text)
    if a.md:
        (run / "status.md").write_text(text + "\n", encoding="utf-8")


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search")
    s.add_argument("--run", required=True)
    s.add_argument("--query", action="append", default=[])
    s.add_argument("--cites", action="append", default=[], help="引用了这篇（OpenAlex W 号）的文献")
    s.add_argument("--refs", action="append", default=[], help="这篇（W 号）的参考文献")
    s.add_argument("--per-query", type=int, default=100)
    s.add_argument("--from-year", type=int)
    s.add_argument("--to-year", type=int)
    s.add_argument("--types", default="article,review")
    s.add_argument("--lang", default="en")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("targets")
    s.add_argument("--all", action="store_true", help="列出全部分类")
    s.set_defaults(fn=cmd_targets)

    s = sub.add_parser("check")
    s.add_argument("--run", required=True)
    s.add_argument("--collection")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("fetch")
    s.add_argument("--run", required=True)
    s.add_argument("--collection")
    s.add_argument("--only", help="逗号分隔的 DOI，只处理这些")
    s.add_argument("--no-zotero", action="store_true", help="Zotero 开着也只存文件夹")
    s.add_argument("--dir", help="Zotero 没开时这次的下载目录（每次由用户指定，没有默认值）")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--retry", action="store_true", help="已导入的也重做")
    s.add_argument("--allow-am", action="store_true", help="拿不到正式版时导入作者稿（Accepted/Submitted Version）")
    s.add_argument("--max", type=int, default=MAX_PER_RUN)
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("open")
    s.add_argument("--run", required=True)
    s.add_argument("--batch", type=int, default=8)
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--list-only", action="store_true")
    s.set_defaults(fn=cmd_open)

    s = sub.add_parser("status")
    s.add_argument("--run", required=True)
    s.add_argument("--md", action="store_true")
    s.set_defaults(fn=cmd_status)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
