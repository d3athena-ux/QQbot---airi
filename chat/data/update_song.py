# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.request import Request, urlopen


MUSICS_URL = "https://sekai-world.github.io/sekai-master-db-diff/musics.json"
MUSIC_VOCALS_URL = "https://sekai-world.github.io/sekai-master-db-diff/musicVocals.json"

# 保险：如果 musicVocals 识别失败，用 musics.seq 的规律兜底
# 经验规律：seq 的第 2 位（从左数）= 3 通常是 MMJ（例如 2300101）
FALLBACK_USE_SEQ = True

# MMJ 四人：Minori/Haruka/Airi/Shizuku 在多数 master db 里对应的 game_characterId
# 如果你未来想改成“自动从 gameCharacterUnits.json 解析”，也可以再加一层 fetch。
MMJ_GAME_CHARACTER_IDS: Set[int] = {5, 6, 7, 8}


@dataclass(frozen=True)
class SongRow:
    sekai_id: int
    title_name: str
    title_cn_name: str


def http_get_json(url: str, timeout: int = 60) -> Any:
    req = Request(url, headers={"User-Agent": "pjskbot-song-updater/2.0"})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8"))


def load_json_list(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, list):
        raise ValueError(f"{path} is not a JSON list.")
    return obj


def save_json_list(path: Path, items: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def reindex(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for i, it in enumerate(items, start=1):
        out.append(
            {
                "id": i,
                "sekai_id": int(it["sekai_id"]),
                "title_name": str(it["title_name"]),
                "title_cn_name": str(it["title_cn_name"]),
            }
        )
    return out


def build_existing_maps(existing: List[Dict[str, Any]]) -> tuple[Dict[int, Dict[str, Any]], Dict[str, str]]:
    """
    - by_id: sekai_id -> entry
    - cn_by_title: title_name -> title_cn_name (用于新歌尽量复用已有译名)
    """
    by_id: Dict[int, Dict[str, Any]] = {}
    cn_by_title: Dict[str, str] = {}
    for it in existing:
        try:
            sid = int(it.get("sekai_id"))
            title = str(it.get("title_name", "")).strip()
            cn = str(it.get("title_cn_name", "")).strip()
        except Exception:
            continue
        if sid:
            by_id[sid] = {"sekai_id": sid, "title_name": title, "title_cn_name": cn}
        if title and cn:
            cn_by_title[title] = cn
    return by_id, cn_by_title


def collect_mmj_music_ids_from_vocals(music_vocals: List[Dict[str, Any]]) -> Set[int]:
    mmj_ids: Set[int] = set()
    for v in music_vocals:
        if not isinstance(v, dict):
            continue
        music_id = v.get("musicId")
        chars = v.get("characters")
        if music_id is None or not isinstance(chars, list):
            continue

        hit = False
        for c in chars:
            if not isinstance(c, dict):
                continue
            if c.get("characterType") != "game_character":
                continue
            cid = c.get("characterId")
            try:
                cid_i = int(cid)
            except Exception:
                continue
            if cid_i in MMJ_GAME_CHARACTER_IDS:
                hit = True
                break

        if hit:
            try:
                mmj_ids.add(int(music_id))
            except Exception:
                pass

    return mmj_ids


def collect_mmj_music_ids_from_seq(musics: List[Dict[str, Any]]) -> Set[int]:
    """
    兜底规则：seq 转字符串，第二位为 '3' 视为 MMJ。
    例：2300101 -> '2' '3' ...
    """
    ids: Set[int] = set()
    for m in musics:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        seq = m.get("seq")
        if mid is None or seq is None:
            continue
        try:
            seq_s = str(int(seq))
            mid_i = int(mid)
        except Exception:
            continue
        if len(seq_s) >= 2 and seq_s[1] == "3":
            ids.add(mid_i)
    return ids


def build_mmj_song_rows(
    musics: List[Dict[str, Any]],
    mmj_music_ids: Set[int],
    existing_by_id: Dict[int, Dict[str, Any]],
    cn_by_title: Dict[str, str],
) -> List[SongRow]:
    id_to_music: Dict[int, Dict[str, Any]] = {}
    for m in musics:
        if isinstance(m, dict) and "id" in m:
            try:
                id_to_music[int(m["id"])] = m
            except Exception:
                continue

    rows: List[SongRow] = []
    for mid in sorted(mmj_music_ids):
        m = id_to_music.get(mid)
        if not m:
            continue
        title = str(m.get("title", "")).strip()
        if not title:
            continue

        # 译名优先级：
        # 1) 旧 json 同 sekai_id 的译名
        # 2) 旧 json 同 title 的译名（防止标题不变但 id 新增）
        # 3) 没有就先用原名占位
        cn = ""
        old = existing_by_id.get(mid)
        if old and str(old.get("title_cn_name", "")).strip():
            cn = str(old["title_cn_name"]).strip()
        elif title in cn_by_title:
            cn = cn_by_title[title]
        else:
            cn = title

        rows.append(SongRow(sekai_id=mid, title_name=title, title_cn_name=cn))

    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Update pjsk MMJ song_list.json by Sekai-World master DB")
    parser.add_argument(
        "--out",
        type=str,
        default="song_list.json",
        help="Output JSON path (default: ./song_list.json in script folder)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write file, only print summary",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = script_dir / out_path

    existing = load_json_list(out_path)
    existing_by_id, cn_by_title = build_existing_maps(existing)

    # 1) 下载 musics.json / musicVocals.json
    try:
        musics = http_get_json(MUSICS_URL)
        if not isinstance(musics, list):
            raise ValueError("musics.json is not a list")
    except Exception as e:
        print(f"[ERROR] Failed to fetch musics.json: {e}", file=sys.stderr)
        return 2

    mmj_music_ids: Set[int] = set()
    vocals_ok = False
    try:
        music_vocals = http_get_json(MUSIC_VOCALS_URL)
        if not isinstance(music_vocals, list):
            raise ValueError("musicVocals.json is not a list")
        mmj_music_ids = collect_mmj_music_ids_from_vocals(music_vocals)
        vocals_ok = True
    except Exception as e:
        print(f"[WARN] Failed to fetch/parse musicVocals.json: {e}", file=sys.stderr)

    # 2) 如果 vocals 识别不到，兜底用 seq 规律
    if (not mmj_music_ids) and FALLBACK_USE_SEQ:
        mmj_music_ids = collect_mmj_music_ids_from_seq(musics)
        if not vocals_ok:
            print("[WARN] Using seq fallback to detect MMJ songs.", file=sys.stderr)

    if not mmj_music_ids:
        print("[ERROR] MMJ music id set is empty. Abort to avoid overwriting your JSON with empty list.", file=sys.stderr)
        return 3

    # 3) 生成新列表（只输出 MMJ 的歌；并保留/复用旧译名）
    rows = build_mmj_song_rows(musics, mmj_music_ids, existing_by_id, cn_by_title)
    new_items = reindex([r.__dict__ for r in rows])

    # 统计变化
    old_ids = {int(it.get("sekai_id")) for it in existing if isinstance(it, dict) and it.get("sekai_id") is not None}
    new_ids = {r.sekai_id for r in rows}
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)

    print(f"[INFO] MMJ songs detected: {len(new_items)}")
    if added:
        print(f"[INFO] Added: {added[:50]}{' ...' if len(added) > 50 else ''}")
    if removed:
        print(f"[INFO] Removed (no longer detected as MMJ): {removed[:50]}{' ...' if len(removed) > 50 else ''}")

    if args.dry_run:
        print("[DRY-RUN] Not writing file.")
        return 0

    save_json_list(out_path, new_items)
    print(f"[OK] Wrote {len(new_items)} songs -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
