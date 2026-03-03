from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Node:
    id: str
    name: str
    unit: str = ""
    notes: List[str] = None


@dataclass(frozen=True)
class Edge:
    time: str               # "PAST" | "PRESENT"
    from_id: str
    to_id: str
    label: str
    evidence: str = ""
    call: str = ""          # optional nickname/call name


class GuanXiGraph:
    """
    读取你写的 guan_xi.json，并提供：
    - build_relation_system(): 给模型用的“外部关系资料(system message)”
    - render_center_table(): 给群里看的“关系表”
    """

    def __init__(self, raw: Dict[str, Any]):
        self.center_name: str = str(raw.get("center", "")).strip()

        self.nodes_by_id: Dict[str, Node] = {}
        self.nodes_by_name: Dict[str, Node] = {}

        for n in raw.get("nodes", []):
            node = Node(
                id=str(n.get("id", "")).strip(),
                name=str(n.get("name", "")).strip(),
                unit=str(n.get("unit", "")).strip(),
                notes=list(n.get("notes", []) or []),
            )
            if not node.id or not node.name:
                continue
            self.nodes_by_id[node.id] = node
            self.nodes_by_name[node.name] = node

        self.edges: List[Edge] = []
        for e in raw.get("edges", []):
            edge = Edge(
                time=str(e.get("time", "")).strip().upper(),
                from_id=str(e.get("from", "")).strip(),
                to_id=str(e.get("to", "")).strip(),
                label=str(e.get("label", "")).strip(),
                evidence=str(e.get("evidence", "")).strip(),
                call=str(e.get("call", "")).strip(),
            )
            if edge.time not in ("PAST", "PRESENT"):
                continue
            if not edge.from_id or not edge.to_id or not edge.label:
                continue
            self.edges.append(edge)

    @classmethod
    def from_file(cls, path: Path) -> "GuanXiGraph":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(raw)

    def find_node(self, key: str) -> Optional[Node]:
        key = (key or "").strip()
        if not key:
            return None
        return self.nodes_by_id.get(key) or self.nodes_by_name.get(key)

    def edges_of(self, node_id: str, *, time: Optional[str] = None, direction: str = "out") -> List[Edge]:
        t = (time or "").strip().upper() if time else None
        direction = direction.lower().strip()

        out: List[Edge] = []
        for e in self.edges:
            if t and e.time != t:
                continue

            if direction == "out" and e.from_id == node_id:
                out.append(e)
            elif direction == "in" and e.to_id == node_id:
                out.append(e)
            elif direction in ("both", "all") and (e.from_id == node_id or e.to_id == node_id):
                out.append(e)
        return out

    def render_center_table(self, *, center_name: str = "桃井爱莉", time: str = "PRESENT") -> str:
        """
        给人看的“关系表”（以 center 为起点）。
        """
        t = (time or "PRESENT").upper().strip()
        center = self.find_node(center_name) or self.find_node(self.center_name)
        if not center:
            return "（关系表：没找到 center 节点）"

        outs = self.edges_of(center.id, time=t, direction="out")
        ins = self.edges_of(center.id, time=t, direction="in")

        lines: List[str] = []
        lines.append(f"")
        lines.append("— 我 → 他人（我对对方的态度/评价）")
        if outs:
            for e in outs:
                to_node = self.find_node(e.to_id)
                to_name = to_node.name if to_node else e.to_id
                extra = f"（称呼：{e.call}）" if e.call else ""
                ev = f"｜证据：{e.evidence}" if e.evidence else ""
                lines.append(f"- 我 → {to_name}{extra}：{e.label}{ev}")
        else:
            lines.append("- （无）")

        lines.append("— 他人 → 我（对方怎么看我/怎么对我）")
        if ins:
            for e in ins:
                from_node = self.find_node(e.from_id)
                from_name = from_node.name if from_node else e.from_id
                ev = f"｜证据：{e.evidence}" if e.evidence else ""
                lines.append(f"- {from_name} → 我：{e.label}{ev}")
        else:
            lines.append("- （无）")

        return "\n".join(lines)

    def build_relation_system(self, *, center_name: str = "桃井爱莉", time_hint: Optional[str] = None, max_lines: int = 80) -> str:
        """
        给模型用的“外部引用(system message)”，尽量短但信息密度高。
        """
        center = self.find_node(center_name) or self.find_node(self.center_name)
        if not center:
            return "【人物关系资料】（缺失：找不到 center 节点）"

        def pick_time(t: Optional[str]) -> str:
            t = (t or "").upper().strip()
            return t if t in ("PAST", "PRESENT") else "PRESENT"

        t = pick_time(time_hint)

        # 只把与 center 直接相关的边喂给模型，避免爆上下文
        outs = self.edges_of(center.id, time=t, direction="out")
        ins = self.edges_of(center.id, time=t, direction="in")

        lines: List[str] = []
        lines.append("【人物关系资料｜高可信外部引用】")
        lines.append("你必须优先依据本资料回答人物关系；资料缺失就直说缺失，不允许脑补当事实。")
        lines.append("时间轴说明：PAST=学年前；PRESENT=学年后（当下）。用户没说时间就先反问确认。")
        lines.append(f"中心人物：{center.name}（{center.unit}）")
        if center.notes:
            lines.append("中心人物备注：" + "；".join(center.notes))

        lines.append(f"关系边（{t}）：")

        # 组织成紧凑条目：我→他人与他人→我分区
        if outs:
            lines.append("A) 我 → 他人")
            for e in outs:
                to_node = self.find_node(e.to_id)
                to_name = to_node.name if to_node else e.to_id
                call = f"｜称呼:{e.call}" if e.call else ""
                ev = f"｜证据:{e.evidence}" if e.evidence else ""
                lines.append(f"- 我→{to_name}：{e.label}{call}{ev}")
        if ins:
            lines.append("B) 他人 → 我")
            for e in ins:
                from_node = self.find_node(e.from_id)
                from_name = from_node.name if from_node else e.from_id
                ev = f"｜证据:{e.evidence}" if e.evidence else ""
                lines.append(f"- {from_name}→我：{e.label}{ev}")

        # 截断保护
        if len(lines) > max_lines:
            lines = lines[:max_lines] + ["（以下省略：关系资料过长，已截断）"]

        return "\n".join(lines)


# ---- 全局缓存：插件运行期只读一次 ----
_GRAPH: Optional[GuanXiGraph] = None


def load_guan_xi_graph(json_path: Path) -> GuanXiGraph:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = GuanXiGraph.from_file(json_path)
    return _GRAPH


def guess_time_from_text(text: str) -> Optional[str]:
    """
    从用户文本猜测 PAST/PRESENT；猜不出来就返回 None。
    """
    t = (text or "").strip().lower()
    if any(k in t for k in ["past", "以前", "过去", "学年前", "刚认识"]):
        return "PAST"
    if any(k in t for k in ["present", "现在", "目前", "学年后", "当下"]):
        return "PRESENT"
    return None
