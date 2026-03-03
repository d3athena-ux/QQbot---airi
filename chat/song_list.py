from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class Song:
    """歌曲数据结构"""
    id: int
    sekai_id: int
    title_name: str
    title_cn_name: str


class SongList:
    """
    歌单管理器
    提供：
    - 随机推荐歌曲
    - 按关键词搜索
    - 按ID查询
    """

    def __init__(self, songs: List[Song]):
        self.songs = songs
        self.songs_by_id = {s.id: s for s in songs}
        self.songs_by_sekai_id = {s.sekai_id: s for s in songs}

    @classmethod
    def from_file(cls, path: Path) -> "SongList":
        """从 JSON 文件加载歌单"""
        raw = json.loads(path.read_text(encoding="utf-8"))
        songs = []
        for item in raw:
            song = Song(
                id=int(item.get("id", 0)),
                sekai_id=int(item.get("sekai_id", 0)),
                title_name=str(item.get("title_name", "")).strip(),
                title_cn_name=str(item.get("title_cn_name", "")).strip(),
            )
            if song.id > 0 and song.title_name:
                songs.append(song)
        return cls(songs)

    def get_random(self) -> Optional[Song]:
        """随机获取一首歌"""
        if not self.songs:
            return None
        return random.choice(self.songs)

    def get_by_id(self, song_id: int) -> Optional[Song]:
        """按ID查询歌曲"""
        return self.songs_by_id.get(song_id)

    def get_by_sekai_id(self, sekai_id: int) -> Optional[Song]:
        """按游戏内ID查询歌曲"""
        return self.songs_by_sekai_id.get(sekai_id)

    def fuzzy_search(self, keyword: str) -> List[Song]:
        """
        模糊搜索（支持日文/中文，跳跃匹配）
        例如：歌曲名"1234"，搜索"14"也能匹配
        """
        keyword = keyword.lower().strip()
        if not keyword:
            return []

        results = []
        for song in self.songs:
            title_lower = song.title_name.lower()
            title_cn_lower = song.title_cn_name.lower()

            # 完整匹配
            if keyword in title_lower or keyword in title_cn_lower:
                results.append(song)
                continue

            # 跳跃匹配（关键字的字符按顺序出现即可）
            if self._is_subsequence(keyword, title_lower) or \
                    self._is_subsequence(keyword, title_cn_lower):
                results.append(song)

        return results

    @staticmethod
    def _is_subsequence(sub: str, text: str) -> bool:
        """检查 sub 是否是 text 的子序列（跳跃匹配）"""
        it = iter(text)
        return all(c in it for c in sub)

    def format_song(self, song: Song) -> str:
        """格式化歌曲信息 - 优先日文，括号中文"""
        return f"🎵 {song.title_name}（{song.title_cn_name}）"

    def format_song_list(self, songs: List[Song]) -> str:
        """格式化歌曲列表"""
        if not songs:
            return "没有找到匹配的歌曲"

        lines = ["找到以下歌曲：\n"]
        for song in songs:
            lines.append(f"[{song.sekai_id}] {song.title_name}（{song.title_cn_name}）")
        lines.append("\n💡 请发送：播放 [序号] 来播放")
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self.songs)


# ---- 全局缓存 ----
_SONG_LIST: Optional[SongList] = None


def load_song_list(json_path: Path) -> SongList:
    """加载歌单（全局缓存）"""
    global _SONG_LIST
    if _SONG_LIST is None:
        _SONG_LIST = SongList.from_file(json_path)
    return _SONG_LIST


def guess_song_intent(text: str) -> Optional[str]:
    """
    从用户文本判断歌曲相关意图
    返回：'random' | 'play' | 'play_last' | None
    """
    t = text.lower().strip()

    # 播放上次推荐的歌
    if any(k in t for k in ["播放推荐", "播放你推荐", "播放刚才", "播放刚刚"]):
        return "play_last"

    # 播放指定歌曲
    if any(k in t for k in ["播放", "试听", "听听", "放一下"]):
        return "play"

    # 随机推荐
    if any(k in t for k in ["推荐", "来首歌", "听歌", "随机", "什么歌", "有什么歌", "歌单", "唱过什么", "你们团"]):
        return "random"

    return None
