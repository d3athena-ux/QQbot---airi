import re
import time
from typing import List, Optional, Tuple
from urllib.parse import quote_plus

import httpx
from nonebot import get_plugin_config, on_regex
from pydantic import BaseModel

# =========================================================
# ▼▼▼ ふりがな（平假名注音，可选） ▼▼▼
# =========================================================
# 需要：pip install pykakasi
try:
    from pykakasi import kakasi as _kakasi_mod

    _kks = _kakasi_mod()
    _kks.setMode("J", "H")  # Kanji -> Hiragana
    _kks.setMode("K", "H")  # Katakana -> Hiragana
    _kks.setMode("H", "H")  # Hiragana -> Hiragana
    _conv = _kks.getConverter()

    def to_hiragana(s: str) -> str:
        return _conv.do(s).strip()

except Exception:
    def to_hiragana(s: str) -> str:
        return ""

# =========================================================
# ▼▼▼ MyMemory 配置 (MyMemory Config) ▼▼▼
# =========================================================

class Config(BaseModel):
    mymemory_base: str = "https://api.mymemory.translated.net"
    mymemory_email: Optional[str] = None  # .env.prod: MYMEMORY_EMAIL=xxx@qq.com
    timeout: float = 12.0

    # 同群冷却（秒），防止刷爆免费额度
    group_cooldown_sec: float = 1.0

    # 日语结果是否显示平假名注音（装了 pykakasi 才生效）
    jp_show_furigana: bool = True

config = get_plugin_config(Config)

# =========================================================
# ▼▼▼ 工具：语言检测 / 去重截断 / MyMemory 请求 ▼▼▼
# =========================================================

# 日语：平假名/片假名
_JA_KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
# 英文：包含拉丁字母就倾向英文（允许混入数字标点）
_EN_HAS_LETTER_RE = re.compile(r"[A-Za-z]")
# 中文/中日韩统一汉字范围（这里只用来判断“像中文”）
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

def detect_lang(text: str) -> str:
    """
    简单自动检测：返回 MyMemory 语言码：
    - ja / en / zh-CN
    """
    s = (text or "").strip()
    if not s:
        return "zh-CN"
    if _JA_KANA_RE.search(s):
        return "ja"
    if _EN_HAS_LETTER_RE.search(s):
        return "en"
    if _CJK_RE.search(s):
        return "zh-CN"
    # 兜底：当中文处理
    return "zh-CN"

def pick_best(candidates: List[str], limit: int) -> List[str]:
    seen = set()
    out = []
    for x in candidates:
        x = (x or "").strip()
        if not x or x in seen:
            continue
        seen.add(x)
        out.append(x)
        if len(out) >= limit:
            break
    return out

async def mymemory_translate(text: str, source_lang: str, target_lang: str) -> Tuple[str, List[str]]:
    """
    GET /get?q=...&langpair=en|zh-CN&de=邮箱(可选)
    返回: (primary, alternatives)
    """
    q = quote_plus(text)
    langpair = f"{source_lang}|{target_lang}"
    url = f"{config.mymemory_base.rstrip('/')}/get?q={q}&langpair={langpair}"
    if config.mymemory_email:
        url += f"&de={quote_plus(config.mymemory_email)}"

    async with httpx.AsyncClient(timeout=config.timeout, trust_env=False) as client:
        r = await client.get(url)
        r.raise_for_status()
        j = r.json()

    primary = (j.get("responseData") or {}).get("translatedText") or ""
    matches = j.get("matches") or []
    alts = []
    for m in matches:
        t = (m or {}).get("translation")
        if t:
            alts.append(t)
    return primary, alts

# =========================================================
# ▼▼▼ 中文“近义说法”（免费兜底：小词库 + 句子兜底） ▼▼▼
# =========================================================

# 你可以自己慢慢扩充这个表（越用越好用）
_CN_SYNONYMS = {
    "你好": ["您好", "嗨", "哈喽"],
    "谢谢": ["多谢", "感谢", "谢啦"],
    "再见": ["拜拜", "回头见", "下次见"],
    "喜欢": ["爱", "很中意", "很喜欢"],
    "讨厌": ["不喜欢", "反感", "厌烦"],
    "开心": ["高兴", "愉快", "乐呵"],
    "难过": ["伤心", "低落", "郁闷"],
    "厉害": ["强", "牛", "很优秀"],
    "好看": ["漂亮", "美", "耐看"],
}

def chinese_meaning_or_synonyms(text: str, limit: int = 3) -> List[str]:
    """
    中文输入时：
    - 单词/短语：尽量给近义说法
    - 长句：不硬造近义词，直接给“就是它本身/大意”
    """
    s = (text or "").strip()
    if not s:
        return ["没内容"]

    # 完全命中词库
    if s in _CN_SYNONYMS:
        return pick_best(_CN_SYNONYMS[s], limit=limit)

    # 句子/长文本：给大意式兜底（别乱改意思）
    if len(s) >= 8 or any(ch in s for ch in "，。！？；：,.!?;:"):
        # 你说“可以不翻译”，这里就不翻译，给简短说明
        return pick_best([s], limit=1)

    # 短词但不在词库：直接返回本身（不瞎编）
    return pick_best([s], limit=1)

# =========================================================
# ▼▼▼ 简单防刷：同群间隔限制 ▼▼▼
# =========================================================

_last_group_ts = {}

def allow_group(event) -> bool:
    gid = getattr(event, "group_id", None)
    if gid is None:
        return True
    now = time.time()
    last = _last_group_ts.get(int(gid), 0.0)
    if now - last < float(config.group_cooldown_sec):
        return False
    _last_group_ts[int(gid)] = now
    return True

# =========================================================
# ▼▼▼ 核心：翻译到目标语言（包含中文兜底） ▼▼▼
# =========================================================

async def translate_to(text: str, target_lang: str) -> List[str]:
    """
    target_lang: 'zh-CN' / 'en' / 'ja'
    返回候选列表（上层再按 1~3 / 1~2 截断）
    """
    src = detect_lang(text)

    # 目标是中文，且本来就是中文：给近义说法/不翻译
    if target_lang == "zh-CN" and src == "zh-CN":
        return chinese_meaning_or_synonyms(text, limit=3)

    # 目标是英语/日语，但本来就是同语言：不翻译（直接回原句）
    if target_lang in ("en", "ja") and src == target_lang:
        return pick_best([text], limit=1)

    primary, alts = await mymemory_translate(text, src, target_lang)
    return pick_best([primary] + alts, limit=5)

def format_output(best: List[str], target_lang: str, max_n: int) -> str:
    """
    - 中文：用 " / " 合并
    - 英语：用 " / " 合并
    - 日语：如启用注音，单条用两行；多条用空行分隔
    """
    best = pick_best(best, limit=max_n)

    if not best:
        return "没翻出来"

    if target_lang == "ja" and config.jp_show_furigana:
        blocks = []
        for jp in best:
            hira = to_hiragana(jp)
            if hira and hira != jp:
                blocks.append(f"{jp}\n{hira}")
            else:
                blocks.append(jp)
        return "\n\n".join(blocks)

    return " / ".join(best)

# =========================================================
# ▼▼▼ 功能 1：xx 是什么意思（自动检测中/英/日 -> 简中）▼▼▼
# =========================================================

meaning_matcher = on_regex(r"^\s*(?P<term>.+?)\s*是什么意思\s*$", priority=5, block=True)

@meaning_matcher.handle()
async def _meaning(event):
    if not allow_group(event):
        await meaning_matcher.finish("慢一点~")

    full = event.get_plaintext().strip()
    term = re.sub(r"\s*是什么意思\s*$", "", full).strip()

    try:
        cands = await translate_to(term, "zh-CN")
    except Exception:
        await meaning_matcher.finish("翻译失败（网络/额度限制）")

    # 硬性：1~3 条
    await meaning_matcher.finish(format_output(cands, "zh-CN", max_n=3))

# =========================================================
# ▼▼▼ 功能 2：xx 中文/英语/日语翻译（支持“怎么翻译/翻译成/怎么说”）▼▼▼
# =========================================================

_LANG_MAP = {
    "中文": "zh-CN",
    "英语": "en",
    "日语": "ja",
}

async def _handle_target(matcher, event, text: str, lang_cn: str):
    if not allow_group(event):
        await matcher.finish("慢一点~")

    target = _LANG_MAP.get(lang_cn)
    if not target:
        await matcher.finish("只支持：中文/英语/日语")

    try:
        cands = await translate_to(text, target)
    except Exception:
        await matcher.finish("翻译失败（网络/额度限制）")

    # 条数规则：中文最多3；英语/日语最多2
    max_n = 3 if target == "zh-CN" else 2
    await matcher.finish(format_output(cands, target, max_n=max_n))

# ① “xx 中文翻译 / xx 日语翻译 / xx 英语怎么翻译”
to_lang_1 = on_regex(
    r"^\s*(?P<text>.+)\s*(?P<lang>中文|英语|日语)\s*(?:怎么)?翻译\s*[?？!！。．.]*\s*$",
    priority=5,
    block=True,
)

@to_lang_1.handle()
async def _(event):
    full = event.get_plaintext().strip()
    m = re.match(r"^\s*(?P<text>.+)\s*(?P<lang>中文|英语|日语)\s*(?:怎么)?翻译\s*[?？!！。．.]*\s*$", full)
    if not m:
        return
    await _handle_target(to_lang_1, event, m.group("text").strip(), m.group("lang"))

# ② “xx 怎么翻译成中文/英语/日语”
to_lang_2 = on_regex(
    r"^\s*(?P<text>.+)\s*怎么翻译成\s*(?P<lang>中文|英语|日语)\s*[?？!！。．.]*\s*$",
    priority=5,
    block=True,
)

@to_lang_2.handle()
async def _(event):
    full = event.get_plaintext().strip()
    m = re.match(r"^\s*(?P<text>.+)\s*怎么翻译成\s*(?P<lang>中文|英语|日语)\s*[?？!！。．.]*\s*$", full)
    if not m:
        return
    await _handle_target(to_lang_2, event, m.group("text").strip(), m.group("lang"))

# ③ “xx 翻译成中文/英语/日语”
to_lang_3 = on_regex(
    r"^\s*(?P<text>.+)\s*翻译成\s*(?P<lang>中文|英语|日语)\s*[?？!！。．.]*\s*$",
    priority=5,
    block=True,
)

@to_lang_3.handle()
async def _(event):
    full = event.get_plaintext().strip()
    m = re.match(r"^\s*(?P<text>.+)\s*翻译成\s*(?P<lang>中文|英语|日语)\s*[?？!！。．.]*\s*$", full)
    if not m:
        return
    await _handle_target(to_lang_3, event, m.group("text").strip(), m.group("lang"))

# ④ “xx 中文/英语/日语怎么说”
to_lang_4 = on_regex(
    r"^\s*(?P<text>.+)\s*(?P<lang>中文|英语|日语)\s*怎么说\s*[?？!！。．.]*\s*$",
    priority=5,
    block=True,
)

@to_lang_4.handle()
async def _(event):
    full = event.get_plaintext().strip()
    m = re.match(r"^\s*(?P<text>.+)\s*(?P<lang>中文|英语|日语)\s*怎么说\s*[?？!！。．.]*\s*$", full)
    if not m:
        return
    await _handle_target(to_lang_4, event, m.group("text").strip(), m.group("lang"))
