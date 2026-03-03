from __future__ import annotations
import os
import asyncio
import base64
import html as ihtml
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from nonebot import on_fullmatch
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment
from nonebot.log import logger
from playwright.async_api import async_playwright

# ===== 你可以按需改 =====
PJSK_PROXY = os.getenv("PJSK_PROXY", "http://127.0.0.1:7890")  # Clash HTTP
PJSK_NO_PROXY = os.getenv("PJSK_NO_PROXY") == "1"

REPO_JP = "sekai-master-db-diff"
REPO_CN = "sekai-master-db-cn-diff"

# 镜像源：强烈建议把 jsDelivr 放第一（很多网络下比 github.io 稳）
_MIRRORS = [
    "https://cdn.jsdelivr.net/gh/Sekai-World/{repo}@main/{file}",
    "https://ghproxy.net/https://raw.githubusercontent.com/Sekai-World/{repo}/main/{file}",
    "https://raw.githubusercontent.com/Sekai-World/{repo}/main/{file}",
    "https://sekai-world.github.io/{repo}/{file}",
]

# 可选本地缓存（建议开）
CACHE_DIR = Path(__file__).parent / "cache_db"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _setup_proxy_env() -> None:
    """用环境变量代理 + trust_env，兼容各种 httpx 版本；并且让 127.0.0.1 不走代理。"""
    if PJSK_NO_PROXY:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(k, None)
        return

    proxy = (PJSK_PROXY or "").strip()
    if proxy:
        # 兼容你日志里出现的 127.0.0.1:7890（缺 scheme）情况
        if "://" not in proxy:
            proxy = "http://" + proxy

        # 直接覆盖（不要 setdefault，避免被旧环境变量污染）
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ[k] = proxy

    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")


def _cache_path(repo: str, file: str) -> Path:
    safe = file.replace("/", "_")
    return CACHE_DIR / f"{repo}__{safe}"


def _log_fetch_error(url: str, e: Exception) -> None:
    cause = getattr(e, "__cause__", None)
    extra = ""
    if isinstance(e, httpx.ConnectTimeout):
        extra = " [ConnectTimeout]"
    elif isinstance(e, httpx.ReadTimeout):
        extra = " [ReadTimeout]"
    elif isinstance(e, httpx.ConnectError):
        extra = " [ConnectError]"
    elif isinstance(e, httpx.ProxyError):
        extra = " [ProxyError]"
    elif isinstance(e, httpx.HTTPStatusError):
        extra = f" [HTTPStatusError status={getattr(getattr(e, 'response', None), 'status_code', None)}]"
    elif isinstance(e, httpx.TransportError):
        extra = " [TransportError]"

    logger.warning(f"[peach] fetch failed: {url}{extra} -> {e!r}; cause={cause!r}")


async def _fetch_json_from_url(client: httpx.AsyncClient, url: str) -> Any:
    r = await client.get(url)
    if r.status_code == 404:
        raise FileNotFoundError(url)
    r.raise_for_status()
    return r.json()


async def fetch_json_file(
    client: httpx.AsyncClient,
    repo: str,
    filename: str,
    *,
    use_cache: bool = True,
) -> Tuple[Any, str]:
    cp = _cache_path(repo, filename)
    if use_cache and cp.exists():
        try:
            return json.loads(cp.read_text(encoding="utf-8")), f"cache:{cp}"
        except Exception as e:
            logger.warning(f"[peach] cache read failed: {cp} -> {e!r}")

    last_err: Optional[Exception] = None
    for tpl in _MIRRORS:
        url = tpl.format(repo=repo, file=filename)
        try:
            data = await _fetch_json_from_url(client, url)
            if use_cache:
                try:
                    cp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                except Exception as e:
                    logger.warning(f"[peach] cache write failed: {cp} -> {e!r}")
            return data, url
        except Exception as e:
            last_err = e
            _log_fetch_error(url, e)
            continue

    raise RuntimeError(f"All mirrors failed for {repo}/{filename}. last_err={last_err!r}")


# =========================================================
# 角色模板：把这里当成“角色配置区”，以后复制一份改这几行就能衍生
# =========================================================
TRIGGER_TEXT = "来颗桃"
LOOK_TEXT = "看看你"
ROLE_DISPLAY_NAME = "小爱莉"
ROLE_ABBR = "airi"  # name_list.txt 第三列要匹配的缩写

FIRST_REPLY = (
    "好呀，为你找了份专属于小爱莉的精美写真哟\n"
    "如果你想仔细看看我的高清大图请发“看看你”哟"
)

# =========================================================
# 路径配置（不要改）
# =========================================================
CPJSK_ROOT = Path(r"F:\download\mypjskbot\cpjsk")
NAME_LIST_PATH = CPJSK_ROOT / "name_list.txt"
CARDS_DIR = CPJSK_ROOT / "cards"

STAR_PATH = Path(
    r"F:\download\mypjskbot\pjskbot\src\plugins\pjsk\sekai-viewer\src\assets\rarity_star_afterTraining.png"
)

# =========================================================
# HTML 模板（内嵌，Playwright 渲染）
# 只做了“标题一行”的最小改动：中文大 + 日文小
# =========================================================
CARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root{
    --bg:#f6f7fb;
    --card:#ffffff;
    --border:#e6e8f0;
    --text:#0f172a;
    --muted:#64748b;
    --shadow:0 10px 30px rgba(15,23,42,.08);
    --radius:22px;
  }
  *{box-sizing:border-box;}
  body{margin:0;background:var(--bg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",Arial,sans-serif;color:var(--text);}
  .wrap{width:900px;margin:20px auto;background:var(--card);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden;}
  .head{padding:18px 18px 14px 18px;border-bottom:1px solid var(--border);display:flex;gap:14px;align-items:flex-start;}
  .meta{flex:1;min-width:0;}

  /* ▼ 标题样式：中文大 + 日文小（最小改动） */
  .title{display:flex;gap:10px;align-items:baseline;margin:0 0 8px 0;}
  .t-cn{font-size:26px;font-weight:900;line-height:1.15;}
  .t-jp{font-size:16px;font-weight:800;color:var(--muted);line-height:1.15;}

  .subrow{display:flex;flex-wrap:wrap;gap:10px 14px;align-items:center;}
  .pill{border:1px solid var(--border);border-radius:999px;padding:6px 10px;background:#fff;font-size:13px;color:var(--muted);font-weight:800;}
  .stars{display:flex;gap:4px;align-items:center;}
  .star{width:18px;height:18px;display:block;}
  .section{padding:16px 18px 18px 18px;}
  .grid{display:grid;gap:14px;}
  .grid.two{grid-template-columns:1fr 1fr;}
  .grid.one{grid-template-columns:1fr;}
  .imgbox{border:1px solid var(--border);border-radius:18px;overflow:hidden;background:#fff;}
  .label{padding:10px 12px;border-bottom:1px solid var(--border);background:#fbfcff;font-weight:900;}
  .img{width:100%;display:block;}
  .block{margin-top:14px;border:1px solid var(--border);border-radius:18px;overflow:hidden;background:#fff;}
  .block-title{padding:12px 14px;border-bottom:1px solid var(--border);background:#fbfcff;font-weight:900;}
  .block-body{padding:12px 14px;color:var(--text);line-height:1.65;font-size:15px;white-space:pre-wrap;word-break:break-word;}
  .muted{color:var(--muted);}
</style>
</head>

<body>
  <div class="wrap">
    <div class="head">
      <div class="meta">
        <div class="title">
          <span class="t-cn">$card_title_cn</span>
          $card_title_jp_html
        </div>
        <div class="subrow">
          <div class="pill">活动：$event_name</div>
          $rarity_html
        </div>
      </div>
    </div>

    <div class="section">
      <div class="grid $grid_class">
        $normal_block
        $after_block
      </div>

      <div class="block">
        <div class="block-title">技能效果（通常）</div>
        <div class="block-body">$skill_text</div>
      </div>
    </div>
  </div>
</body>
</html>
"""


# =========================================================
# 小工具：data URI / 安全文本
# =========================================================
def _mime(name: str) -> str:
    n = name.lower()
    if n.endswith(".png"):
        return "image/png"
    if n.endswith(".webp"):
        return "image/webp"
    if n.endswith(".jpg") or n.endswith(".jpeg"):
        return "image/jpeg"
    return "application/octet-stream"


def _file_to_data_uri(p: Optional[Path]) -> Optional[str]:
    if not p or not p.exists():
        return None
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{_mime(p.name)};base64,{b64}"


def _esc(x: Any) -> str:
    return ihtml.escape("" if x is None else str(x))


def _nl2br_text(s: str) -> str:
    return _esc(s).replace("\r\n", "\n")


def _parse_rarity(card: dict) -> int:
    v = str(card.get("cardRarityType") or card.get("rarity") or "")
    m = re.search(r"(\d+)", v)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    return 0


def _as_list(x: Any) -> Optional[List[dict]]:
    return x if isinstance(x, list) else None


# =========================================================
# name_list.txt 读取（按 mtime 缓存）
# =========================================================
_NAME_CACHE_MTIME: float = 0.0
_ABBR_TO_CARD_IDS: Dict[str, List[int]] = {}


def _load_name_list_index() -> None:
    global _NAME_CACHE_MTIME, _ABBR_TO_CARD_IDS

    if not NAME_LIST_PATH.exists():
        _ABBR_TO_CARD_IDS = {}
        _NAME_CACHE_MTIME = 0.0
        return

    mtime = NAME_LIST_PATH.stat().st_mtime
    if _ABBR_TO_CARD_IDS and abs(mtime - _NAME_CACHE_MTIME) < 1e-6:
        return

    mp: Dict[str, List[int]] = {}
    for line in NAME_LIST_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            card_id = int(parts[0])
        except Exception:
            continue
        abbr = (parts[2] or "").strip().lower()
        if not abbr:
            continue
        mp.setdefault(abbr, []).append(card_id)

    for k in mp:
        mp[k].sort()

    _ABBR_TO_CARD_IDS = mp
    _NAME_CACHE_MTIME = mtime
    logger.info(f"[peach] name_list loaded: {NAME_LIST_PATH} (abbr={len(mp)})")


def _pick_card_id_for_abbr(abbr: str) -> Optional[int]:
    _load_name_list_index()
    ids = _ABBR_TO_CARD_IDS.get(abbr.lower(), [])
    if not ids:
        return None

    shuffled = ids[:]
    random.shuffle(shuffled)
    for cid in shuffled:
        if (CARDS_DIR / f"{cid}_normal.png").exists():
            return cid
    return None


# =========================================================
# 技能占位符渲染：把 {{17;d}}/{{17;v}} 替换成具体数字
# 需要 skillEffectDetails.json
# =========================================================
_PLACEHOLDER_RE = re.compile(r"\{\{(\d+);([a-zA-Z])\}\}")


def _fmt_number(x: Any) -> str:
    try:
        v = float(x)
    except Exception:
        return str(x)

    # 类似 5.0 -> 5
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return f"{v:.1f}"


def _extract_duration_sec(detail: dict) -> Optional[float]:
    # 常见字段名兜底
    raw = (
        detail.get("activateEffectDuration")
        or detail.get("duration")
        or detail.get("effectDuration")
        or detail.get("valueDuration")
    )
    if raw is None:
        return None
    try:
        v = float(raw)
    except Exception:
        return None

    # 容错：如果大于 100，很多表是毫秒
    if v > 100:
        v = v / 1000.0
    return v


def _extract_value_percent(detail: dict) -> Optional[float]:
    raw = (
        detail.get("activateEffectValue")
        or detail.get("value")
        or detail.get("effectValue")
        or detail.get("valueRate")
    )
    if raw is None:
        return None
    try:
        v = float(raw)
    except Exception:
        return None

    # 容错：如果是 0.2/1.0 这种比例，转成百分比
    if 0 < v <= 3:
        v = v * 100.0
    return v


def _format_skill_text(template: str, details: Dict[int, dict]) -> str:
    if not template:
        return "（暂时没取到技能文案）"

    def repl(m: re.Match) -> str:
        did = int(m.group(1))
        kind = m.group(2).lower()
        d = details.get(did)
        if not d:
            logger.warning(f"[peach] skill placeholder detail id not found: {did} in '{template}'")
            return m.group(0)

        if kind == "d":
            sec = _extract_duration_sec(d)
            return _fmt_number(sec) if sec is not None else m.group(0)

        if kind == "v":
            val = _extract_value_percent(d)
            return _fmt_number(val) if val is not None else m.group(0)

        # 其它占位符类型先原样保留，并打日志
        logger.warning(f"[peach] unsupported skill placeholder kind='{kind}' id={did} in '{template}'")
        return m.group(0)

    return _PLACEHOLDER_RE.sub(repl, template)


# =========================================================
# 远端 DB 缓存（避免每次都拉）
# =========================================================
@dataclass
class _DBCache:
    ts: float
    cards_jp_by_id: Dict[int, dict]
    cards_cn_by_id: Dict[int, dict]
    event_id_by_card_id: Dict[int, int]
    event_name_by_id: Dict[int, str]
    skill_text_by_id: Dict[int, str]          # skills.json 的描述模板
    skill_detail_by_id: Dict[int, dict]       # skillEffectDetails.json 的数值


_DB: Optional[_DBCache] = None
_DB_LOCK = asyncio.Lock()
_DB_TTL_SEC = 60 * 20  # 20 分钟


async def _get_db_cache() -> _DBCache:
    global _DB

    now = time.time()
    if _DB and (now - _DB.ts) < _DB_TTL_SEC:
        return _DB

    async with _DB_LOCK:
        now = time.time()
        if _DB and (now - _DB.ts) < _DB_TTL_SEC:
            return _DB

        _setup_proxy_env()
        logger.info(
            f"[peach] proxy env: PJSK_NO_PROXY={PJSK_NO_PROXY} "
            f"HTTP_PROXY={os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')} "
            f"NO_PROXY={os.environ.get('NO_PROXY')}"
        )

        headers = {"User-Agent": "pjskbot/1.0", "Accept": "application/json,*/*"}
        timeout = httpx.Timeout(connect=10.0, read=25.0, write=25.0, pool=25.0)

        async with httpx.AsyncClient(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
            trust_env=True,
            verify=True,
        ) as client:
            # cards：JP（基础字段），CN（只用来取中文标题）
            cards_jp_list, cards_jp_src = await fetch_json_file(client, REPO_JP, "cards.json")
            try:
                cards_cn_list, cards_cn_src = await fetch_json_file(client, REPO_CN, "cards.json")
            except Exception as e:
                logger.warning(f"[peach] CN cards.json not available, fallback to JP only: {e!r}")
                cards_cn_list, cards_cn_src = [], "failed"

            cards_jp_by_id: Dict[int, dict] = {}
            for c in (_as_list(cards_jp_list) or []):
                try:
                    cards_jp_by_id[int(c.get("id"))] = c
                except Exception:
                    continue

            cards_cn_by_id: Dict[int, dict] = {}
            for c in (_as_list(cards_cn_list) or []):
                try:
                    cards_cn_by_id[int(c.get("id"))] = c
                except Exception:
                    continue

            # events / eventCards：CN 优先，失败回退 JP
            try:
                event_cards_list, event_cards_src = await fetch_json_file(client, REPO_CN, "eventCards.json")
            except Exception:
                event_cards_list, event_cards_src = await fetch_json_file(client, REPO_JP, "eventCards.json")

            try:
                events_list, events_src = await fetch_json_file(client, REPO_CN, "events.json")
            except Exception:
                events_list, events_src = await fetch_json_file(client, REPO_JP, "events.json")

            event_id_by_card_id: Dict[int, int] = {}
            for ec in (_as_list(event_cards_list) or []):
                try:
                    event_id_by_card_id[int(ec.get("cardId"))] = int(ec.get("eventId"))
                except Exception:
                    continue

            event_name_by_id: Dict[int, str] = {}
            for ev in (_as_list(events_list) or []):
                try:
                    eid = int(ev.get("id"))
                except Exception:
                    continue
                event_name_by_id[eid] = str(ev.get("name") or ev.get("title") or "")

            # skills.json（描述模板） + skillEffectDetails.json（数值）
            # skills：CN 优先，失败回退 JP
            try:
                skills_list, skills_src = await fetch_json_file(client, REPO_CN, "skills.json")
            except Exception:
                skills_list, skills_src = await fetch_json_file(client, REPO_JP, "skills.json")

            skill_text_by_id: Dict[int, str] = {}
            for sk in (_as_list(skills_list) or []):
                sid = sk.get("id")
                if sid is None:
                    continue
                try:
                    sid_int = int(sid)
                except Exception:
                    continue
                text = (
                    sk.get("description")
                    or sk.get("skillDescription")
                    or sk.get("effect")
                    or sk.get("name")
                    or ""
                )
                if isinstance(text, list):
                    text = "\n".join(str(x) for x in text if x is not None)
                skill_text_by_id[sid_int] = str(text)

            # skillEffectDetails：用于替换 {{xx;d}}/{{xx;v}}
            detail_list: List[dict] = []
            detail_src = "missing"

            try:
                detail_list, detail_src = await fetch_json_file(client, REPO_CN, "skillEffectDetails.json")
            except Exception as e_cn:
                logger.warning(f"[peach] skillEffectDetails CN missing/unavailable: {e_cn!r}")
                try:
                    detail_list, detail_src = await fetch_json_file(client, REPO_JP, "skillEffectDetails.json")
                except Exception as e_jp:
                    logger.warning(f"[peach] skillEffectDetails JP missing/unavailable: {e_jp!r}")
                    detail_list, detail_src = [], "missing"

            skill_detail_by_id: Dict[int, dict] = {}
            for d in (_as_list(detail_list) or []):
                did = d.get("id")
                if did is None:
                    continue
                try:
                    skill_detail_by_id[int(did)] = d
                except Exception:
                    continue

        _DB = _DBCache(
            ts=time.time(),
            cards_jp_by_id=cards_jp_by_id,
            cards_cn_by_id=cards_cn_by_id,
            event_id_by_card_id=event_id_by_card_id,
            event_name_by_id=event_name_by_id,
            skill_text_by_id=skill_text_by_id,
            skill_detail_by_id=skill_detail_by_id,
        )

        logger.info(
            f"[peach] db cache built: cards_jp={len(cards_jp_by_id)} cards_cn={len(cards_cn_by_id)} "
            f"events={len(event_name_by_id)} skills={len(skill_text_by_id)} details={len(skill_detail_by_id)} "
            f"(src cards_jp={cards_jp_src} cards_cn={cards_cn_src} events={events_src} eventCards={event_cards_src} "
            f"skills={skills_src} details={detail_src})"
        )
        return _DB


def _card_title_from(card: dict) -> str:
    return str(card.get("prefix") or card.get("title") or card.get("name") or "").strip()


def _card_titles(card_id: int, db: _DBCache) -> Tuple[str, str]:
    """返回 (中文标题, 日文标题)。若中文缺失，则只显示中文=日文，日文为空。"""
    jp = _card_title_from(db.cards_jp_by_id.get(card_id, {}))
    cn = _card_title_from(db.cards_cn_by_id.get(card_id, {}))

    if not cn:
        cn = jp or "（未知卡牌标题）"
        jp = ""
    return cn, jp


def _card_skill_text(card: dict, db: _DBCache) -> str:
    raw = card.get("skillId") or card.get("cardSkillId") or card.get("skillID")
    if raw is None:
        return "（暂时没取到技能文案）"
    try:
        sid = int(raw)
    except Exception:
        return "（暂时没取到技能文案）"

    tmpl = (db.skill_text_by_id.get(sid) or "").strip()
    if not tmpl:
        return "（暂时没取到技能文案）"

    # 把 {{xx;d}}/{{xx;v}} 替换掉
    return _format_skill_text(tmpl, db.skill_detail_by_id)


def _card_event_name(card_id: int, db: _DBCache) -> str:
    eid = db.event_id_by_card_id.get(card_id)
    if not eid:
        return "（未知/非活动卡）"
    return db.event_name_by_id.get(eid, f"（活动 {eid}）")


def _rarity_html(rarity: int) -> str:
    if rarity <= 0:
        return ""
    star_uri = _file_to_data_uri(STAR_PATH)
    if not star_uri:
        return ""
    stars = "".join([f'<img class="star" src="{star_uri}" />' for _ in range(rarity)])
    return f'<div class="pill"><span class="muted">稀有度：</span><span class="stars">{stars}</span></div>'


def _img_block(label: str, data_uri: Optional[str]) -> str:
    if not data_uri:
        return ""
    return (
        '<div class="imgbox">'
        f'<div class="label">{_esc(label)}</div>'
        f'<img class="img" src="{data_uri}" />'
        "</div>"
    )


async def _render_card_png(
    card_id: int,
    *,
    title_cn: str,
    title_jp: str,
    event_name: str,
    rarity: int,
    skill_text: str,
    normal_path: Path,
    after_path: Optional[Path],
) -> bytes:
    normal_uri = _file_to_data_uri(normal_path)
    after_uri = _file_to_data_uri(after_path) if after_path else None

    normal_block = _img_block("花前", normal_uri)
    after_block = _img_block("花后", after_uri)

    grid_class = "two" if after_block else "one"

    # 日文标题可选：空就不渲染 span
    title_jp_html = f'<span class="t-jp">{_esc(title_jp)}</span>' if title_jp else ""

    html = CARD_HTML
    html = html.replace("$card_title_cn", _esc(title_cn))
    html = html.replace("$card_title_jp_html", title_jp_html)
    html = html.replace("$event_name", _esc(event_name))
    html = html.replace("$rarity_html", _rarity_html(rarity))
    html = html.replace("$grid_class", grid_class)
    html = html.replace("$normal_block", normal_block)
    html = html.replace("$after_block", after_block)
    html = html.replace("$skill_text", _nl2br_text(skill_text))

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        try:
            page = await browser.new_page(viewport={"width": 900, "height": 10}, device_scale_factor=2)
            await page.set_content(html, wait_until="load")
            await page.wait_for_function(
                "Array.from(document.images).every(img => img.complete)",
                timeout=20000,
            )
            await page.wait_for_timeout(60)
            return await page.screenshot(full_page=True, type="png")
        finally:
            await browser.close()


# =========================================================
# 会话记忆：用于 “看看你”
# =========================================================
@dataclass
class _LastPick:
    card_id: int
    normal_path: Path
    after_path: Optional[Path]
    ts: float


_LAST: Dict[Tuple[str, int], _LastPick] = {}
_LAST_TTL = 60 * 30  # 30 分钟内有效


def _ctx_key(event: MessageEvent) -> Tuple[str, int]:
    uid = str(event.get_user_id())
    gid = int(getattr(event, "group_id", 0) or 0)
    return uid, gid


def _cleanup_last() -> None:
    now = time.time()
    dead = [k for k, v in _LAST.items() if now - v.ts > _LAST_TTL]
    for k in dead:
        _LAST.pop(k, None)


# =========================================================
# NoneBot 匹配器
# =========================================================
peach = on_fullmatch(TRIGGER_TEXT, priority=20, block=True)
look = on_fullmatch(LOOK_TEXT, priority=20, block=True)


@peach.handle()
async def _(event: MessageEvent):
    _cleanup_last()

    if not NAME_LIST_PATH.exists():
        await peach.finish(f"未找到 name_list.txt：{NAME_LIST_PATH}")

    if not CARDS_DIR.exists():
        await peach.finish(f"未找到 cards 目录：{CARDS_DIR}")

    picked = _pick_card_id_for_abbr(ROLE_ABBR)
    if not picked:
        await peach.finish(f"在 name_list.txt 里找不到缩写 {ROLE_ABBR}，或者本地没有对应 normal 卡面")

    normal_path = CARDS_DIR / f"{picked}_normal.png"
    after_path = CARDS_DIR / f"{picked}_after.png"
    if not after_path.exists():
        after_path = None

    await peach.send(FIRST_REPLY)

    db = await _get_db_cache()
    card_jp = db.cards_jp_by_id.get(picked, {})

    title_cn, title_jp = _card_titles(picked, db)
    event_name = _card_event_name(picked, db)
    rarity = _parse_rarity(card_jp)
    skill_text = _card_skill_text(card_jp, db)

    try:
        png = await _render_card_png(
            picked,
            title_cn=title_cn,
            title_jp=title_jp,
            event_name=event_name,
            rarity=rarity,
            skill_text=skill_text,
            normal_path=normal_path,
            after_path=after_path,
        )
    except Exception as e:
        logger.exception(f"[peach] render failed: {e!r}")
        await peach.finish("渲染失败了（Playwright/图片加载异常），你把控制台报错贴我我再继续修。")

    _LAST[_ctx_key(event)] = _LastPick(
        card_id=picked,
        normal_path=normal_path,
        after_path=after_path,
        ts=time.time(),
    )

    await peach.finish(MessageSegment.image(png))


@look.handle()
async def _(event: MessageEvent):
    _cleanup_last()
    key = _ctx_key(event)
    last = _LAST.get(key)

    if not last:
        await look.finish("你还没有“来颗桃”过哦～先发“来颗桃”再让我给你看高清大图吧！")

    try:
        b1 = last.normal_path.read_bytes()
        await look.send(MessageSegment.image(b1))
    except Exception:
        await look.finish("花前图片不见了…你是不是移动/删除了 cards 目录里的文件？")

    if last.after_path and last.after_path.exists():
        try:
            b2 = last.after_path.read_bytes()
            await look.send(MessageSegment.image(b2))
        except Exception:
            pass
