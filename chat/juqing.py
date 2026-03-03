from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter


# -----------------------------
# 数据结构
# -----------------------------

@dataclass(frozen=True)
class StoryPiece:
    id: str
    kind: str                  # "mainline" | "event" | "anecdote"
    event_no: Optional[int]    # 活动期号（仅记录，不在提示里主动曝光）
    title: str
    text: str
    keywords: List[str]


class JuQingDB:
    def __init__(self, raw: Dict[str, Any]):
        self.center = str(raw.get("center", "")).strip() or "桃井爱莉"
        self.pieces: List[StoryPiece] = []

        for p in raw.get("pieces", []):
            sp = StoryPiece(
                id=str(p.get("id", "")).strip(),
                kind=str(p.get("kind", "")).strip(),
                event_no=p.get("event_no", None),
                title=str(p.get("title", "")).strip(),
                text=str(p.get("text", "")).strip(),
                keywords=list(p.get("keywords", []) or []),
            )
            if not sp.id or not sp.title or not sp.text:
                continue
            self.pieces.append(sp)

        # 建索引 + 预计算向量
        self._index_by_id: Dict[str, int] = {sp.id: i for i, sp in enumerate(self.pieces)}
        self._vecs: List[Counter[str]] = []
        self._norms: List[float] = []

        for sp in self.pieces:
            vec = _build_vector(_fingerprint(sp))
            self._vecs.append(vec)
            self._norms.append(_vec_norm(vec))

        # 默认起点：主线开头
        self.default_id = raw.get("default_id") or (self.pieces[0].id if self.pieces else "")

    def get(self, piece_id: str) -> Optional[StoryPiece]:
        idx = self._index_by_id.get(piece_id)
        if idx is None:
            return None
        return self.pieces[idx]

    def index(self, piece_id: str) -> Optional[int]:
        return self._index_by_id.get(piece_id)

    def pick_start(self, query: str, threshold: float = 0.085) -> StoryPiece:
        """
        向量召回：返回最相似的片段；如果相似度不足，则回到 default_id（主线开头）。
        """
        if not self.pieces:
            raise RuntimeError("JuQingDB is empty")

        q = (query or "").strip()
        if not q:
            return self.get(self.default_id) or self.pieces[0]

        q_vec = _build_vector(q)
        q_norm = _vec_norm(q_vec)
        if q_norm <= 0:
            return self.get(self.default_id) or self.pieces[0]

        best_i = 0
        best_s = -1.0
        for i, sp in enumerate(self.pieces):
            s = _cosine(q_vec, q_norm, self._vecs[i], self._norms[i])
            if s > best_s:
                best_s = s
                best_i = i

        # 低于阈值就默认主线开头（满足“没有提示词就从主线开始”）
        if best_s < threshold:
            return self.get(self.default_id) or self.pieces[0]
        return self.pieces[best_i]

    def build_context(self, start_id: str, max_chars: int = 2800) -> str:
        """
        从 start_id 开始按顺序拼接后续片段，直到达到 max_chars。
        注意：不把 event_no 暴露在文本里（只记录不主动提）。
        """
        if not self.pieces:
            return ""

        start_idx = self.index(start_id)
        if start_idx is None:
            start_idx = self.index(self.default_id) or 0

        buf: List[str] = []
        total = 0
        for i in range(start_idx, len(self.pieces)):
            sp = self.pieces[i]
            chunk = f"\n{sp.text}\n"
            if total + len(chunk) > max_chars and buf:
                break
            buf.append(chunk)
            total += len(chunk)
            if total >= max_chars:
                break

        return "\n".join(buf).strip()


# -----------------------------
# 对外 API
# -----------------------------

_JUQING_DB: Optional[JuQingDB] = None


def load_juqing_db(path: Path) -> JuQingDB:
    global _JUQING_DB
    raw = json.loads(path.read_text(encoding="utf-8"))
    _JUQING_DB = JuQingDB(raw)
    return _JUQING_DB


def guess_story_recall_intent(text: str) -> bool:
    """
    识别“问经历/问往事/问发生过什么”的意图。
    你也可以在主程序里只在 normal 模式启用（你已经这么做了）。
    """
    t = (text or "").strip()
    if not t:
        return False

    triggers = [
        "经历过什么", "发生过什么", "发生了什么", "讲讲你的故事", "说说你的故事", "说说过去",
        "你以前怎么", "你以前发生", "你的过去", "你都经历", "你都遇到", "主线是什么", "主线讲了什么",
        "你们怎么认识", "MORE MORE JUMP怎么来的", "你为什么退役", "你为什么引退",
        "世界是什么", "SEKAI是什么", "屋顶", "事务所", "综艺", "退役", "引退", "QT", "Cheerful", "LUMINA", "巨蛋"
    ]
    return any(k in t for k in triggers)


def pick_story_start(db: JuQingDB, user_text: str) -> StoryPiece:
    return db.pick_start(user_text)


def build_recall_context(db: JuQingDB, start_id: str, max_chars: int = 2800) -> str:
    return db.build_context(start_id=start_id, max_chars=max_chars)


def build_recall_system() -> str:
    """
    用 system message 约束输出：10~18行、第一人称、连贯、不主动报期号、不胡编。
    """
    return (
        "【经历回忆回答规则】\n"
        "你是“桃井爱莉”，必须第一人称叙事与对白推进。\n"
        "用户在问“你发生过什么/你经历过什么”之类的问题时：\n"
        "1) 只允许依据用户提供的【可引用剧情资料】回答；资料没覆盖就坦白“这段我没法确定”。\n"
        "2) 回复必须连贯自然，像在回忆；不要分点列条，不要加标题。\n"
        "3) 输出严格控制在 10~18 行（用换行分行），每行是自然句子。\n"
        "4) 活动期号/编号只做内部记录：除非用户主动问“第几期/多少期”，否则不要主动说编号。\n"
    )


def clamp_to_lines(text: str, min_lines: int = 10, max_lines: int = 18) -> str:
    """
    简单后处理：最多 max_lines 行；少于 min_lines 不强行补，避免编造。
    """
    if not text:
        return text
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    # 少于 min_lines：不补水（防止模型瞎编），直接返回
    return "\n".join(lines).strip()


# -----------------------------
# 向量：字符 n-gram + cosine（零依赖）
# -----------------------------

_CLEAN_RE = re.compile(r"[\s\r\n\t]+")


def _fingerprint(sp: StoryPiece) -> str:
    # 用 title/text/keywords 拼一个检索用的“指纹”
    kw = " ".join(sp.keywords or [])
    return f"{sp.title}\n{sp.text}\n{kw}"


def _normalize(text: str) -> str:
    t = (text or "").lower()
    t = _CLEAN_RE.sub(" ", t)
    # 保留中日韩字符、数字、字母；其余统一空格
    t = re.sub(r"[^0-9a-z\u4e00-\u9fff\u3040-\u30ff\u3400-\u4dbf ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _ngrams(t: str, n: int) -> List[str]:
    if len(t) < n:
        return []
    return [t[i:i+n] for i in range(len(t) - n + 1)]


def _build_vector(text: str) -> Counter[str]:
    t = _normalize(text)
    if not t:
        return Counter()

    # 以“字符2/3-gram”为主，兼顾少量单字（提升中文检索）
    grams: List[str] = []
    raw = t.replace(" ", "")
    grams.extend(_ngrams(raw, 2))
    grams.extend(_ngrams(raw, 3))
    grams.extend(list(raw))  # 单字

    return Counter(grams)


def _vec_norm(v: Counter[str]) -> float:
    return math.sqrt(sum(c * c for c in v.values())) if v else 0.0


def _cosine(v1: Counter[str], n1: float, v2: Counter[str], n2: float) -> float:
    if n1 <= 0 or n2 <= 0:
        return 0.0
    # 点积：遍历更短的那个
    if len(v1) > len(v2):
        v1, v2 = v2, v1
        n1, n2 = n2, n1
    dot = 0.0
    for k, c in v1.items():
        dot += c * v2.get(k, 0)
    return dot / (n1 * n2)
