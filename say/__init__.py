import base64
import io
import re
import time
import wave
from pathlib import Path
from typing import Optional, List, Tuple

import anyio
from nonebot import get_plugin_config, logger, on_regex
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment, Bot
from pydantic import BaseModel
from gradio_client import Client, handle_file


# =========================================================
# ▼▼▼ 配置 (Config) ▼▼▼
# =========================================================

class Config(BaseModel):
    # GPT-SoVITS WebUI 地址
    say_api_base: str = "http://127.0.0.1:9872/"

    # 参考音频与参考文本
    gptsovits_ref_wav: Optional[str] = None
    gptsovits_ref_text: Optional[str] = None

    # 冷却（防刷）
    say_cooldown_sec: float = 2.0
    say_max_chars: int = 120

    # 合成参数
    say_top_k: int = 5
    say_top_p: float = 1.0
    say_temperature: float = 1.0
    say_batch_size: int = 20
    say_speed_factor: float = 1.0
    say_fragment_interval: float = 0.3
    say_super_sampling: bool = False
    say_sample_steps: int = 32
    say_text_split_method: str = "凑50字一切"

    # 语音前后静音（秒）
    say_pad_head_sec: float = 0.3
    say_pad_tail_sec: float = 0.3


config = get_plugin_config(Config)

# =========================================================
# ▼▼▼ 小工具：语言识别/切段/冷却 ▼▼▼
# =========================================================

_JA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")


def _guess_prompt_lang(prompt_text: str) -> str:
    s = (prompt_text or "").strip()
    if not s:
        return "中文"
    if _JA_RE.search(s):
        return "日文"
    if re.search(r"[\u4e00-\u9fff]", s):
        return "中文"
    return "英文"


def _lang_to_gradio(target: str) -> str:
    lang_map = {"zh": "中文", "en": "英文", "ja": "日文"}
    return lang_map.get(target, "中文")


def _chunk_text(text: str, max_chars: int) -> List[str]:
    s = (text or "").strip()
    if not s:
        return []

    end_parts = re.split(r"([。！？.!?])", s)
    end_sents = []
    for i in range(0, len(end_parts), 2):
        seg = end_parts[i]
        punc = end_parts[i + 1] if i + 1 < len(end_parts) else ""
        piece = (seg + punc).strip()
        if piece:
            end_sents.append(piece)

    if not end_sents:
        end_sents = [s]

    def split_by_commas(sentence: str) -> List[str]:
        parts = re.split(r"([，、；;：:])", sentence)
        out, cur = [], ""
        for i in range(0, len(parts), 2):
            seg = parts[i]
            punc = parts[i + 1] if i + 1 < len(parts) else ""
            piece = (seg + punc)
            if not piece.strip():
                continue
            if len(cur) + len(piece) <= max_chars:
                cur += piece
            else:
                if cur.strip():
                    out.append(cur.strip())
                cur = piece
        if cur.strip():
            out.append(cur.strip())
        return out

    chunks: List[str] = []
    cur = ""

    def flush():
        nonlocal cur
        if cur.strip():
            chunks.append(cur.strip())
        cur = ""

    for sent in end_sents:
        sent = sent.strip()
        if not sent:
            continue

        pieces = [sent]
        if len(sent) > max_chars:
            pieces = split_by_commas(sent)

        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            if not cur:
                cur = piece
            elif len(cur) + 1 + len(piece) <= max_chars:
                cur = cur + " " + piece
            else:
                flush()
                cur = piece

    flush()

    final_out: List[str] = []
    for c in chunks:
        c = c.strip()
        while len(c) > max_chars:
            final_out.append(c[:max_chars].strip())
            c = c[max_chars:]
        if c.strip():
            final_out.append(c.strip())

    return final_out


_last_ts = 0.0


def _allow(cooldown: float) -> bool:
    global _last_ts
    now = time.time()
    if now - _last_ts < cooldown:
        return False
    _last_ts = now
    return True


def _normalize_tts_text(text: str, target_lang: str) -> str:
    s = (text or "").strip()
    if not s:
        return s

    if target_lang == "zh":
        s = s.replace(",", "。").replace("，", "。")
    elif target_lang == "en":
        s = re.sub(r",\s*", ". ", s)

    return s


# =========================================================
# ▼▼▼ WAV 前后加静音（秒）▼▼▼
# =========================================================

def _pad_wav_bytes(audio_bytes: bytes, head_sec: float, tail_sec: float) -> bytes:
    if not (audio_bytes[:4] == b"RIFF" and b"WAVE" in audio_bytes[:16]):
        return audio_bytes

    head_sec = max(0.0, float(head_sec))
    tail_sec = max(0.0, float(tail_sec))
    if head_sec == 0.0 and tail_sec == 0.0:
        return audio_bytes

    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            nchannels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())

        head_frames = int(head_sec * framerate)
        tail_frames = int(tail_sec * framerate)
        silence_unit = b"\x00" * (sampwidth * nchannels)
        head = silence_unit * head_frames
        tail = silence_unit * tail_frames

        out = io.BytesIO()
        with wave.open(out, "wb") as wo:
            wo.setnchannels(nchannels)
            wo.setsampwidth(sampwidth)
            wo.setframerate(framerate)
            wo.writeframes(head + frames + tail)

        return out.getvalue()
    except Exception:
        logger.exception("[say] pad wav failed, return original audio")
        return audio_bytes


# =========================================================
# ▼▼▼ TTS 客户端：调用 Gradio /inference ▼▼▼
# =========================================================

def _normalize_split_method(x: str) -> str:
    s = (x or "").strip()
    if s == "按 标点符号切":
        return "按标点符号切"
    return s


class GPTSoVITSTTS:
    def __init__(self, base_url: str, api_name: str = "/inference"):
        self.base_url = (base_url or "").strip()
        if not self.base_url:
            raise RuntimeError("SAY_API_BASE 为空")
        if not self.base_url.endswith("/"):
            self.base_url += "/"

        self.api_name = api_name if api_name.startswith("/") else "/" + api_name
        self.client = Client(self.base_url)

        logger.info(f"[say] Loaded as API: {self.base_url}")
        logger.info(f"[say] Using api_name={self.api_name}")

    def _get_ref_audio(self) -> str:
        ref = (config.gptsovits_ref_wav or "").strip()
        if not ref:
            raise RuntimeError("你还没在 .env.prod 里配置 GPTSOVITS_REF_WAV")
        p = Path(ref)
        if not p.exists():
            raise RuntimeError(f"找不到参考音频：{ref}")
        return str(p)

    def _get_prompt_text_lang(self) -> Tuple[str, str]:
        prompt_text = (config.gptsovits_ref_text or "").strip()
        prompt_lang = _guess_prompt_lang(prompt_text) if prompt_text else "中文"
        return prompt_text, prompt_lang

    async def synth(self, text: str, target_lang: str) -> bytes:
        ref_audio_path = self._get_ref_audio()
        ref_audio = handle_file(ref_audio_path)
        prompt_text, prompt_lang = self._get_prompt_text_lang()
        text_lang = _lang_to_gradio(target_lang)

        split_method = _normalize_split_method(config.say_text_split_method)

        inputs = [
            text, text_lang, ref_audio, [],
            prompt_text, prompt_lang,
            int(config.say_top_k),
            float(config.say_top_p),
            float(config.say_temperature),
            split_method,
            int(config.say_batch_size),
            float(config.say_speed_factor),
            False, True,
            float(config.say_fragment_interval),
            -1, True, True, 1.35,
            str(int(config.say_sample_steps)),
            bool(config.say_super_sampling),
        ]

        def _predict_sync():
            return self.client.predict(*inputs, api_name=self.api_name)

        audio_path, _seed = await anyio.to_thread.run_sync(_predict_sync)

        p = Path(audio_path)
        if not p.exists():
            raise RuntimeError(f"TTS 输出文件不存在：{audio_path}")

        audio_bytes = p.read_bytes()
        audio_bytes = _pad_wav_bytes(
            audio_bytes,
            head_sec=config.say_pad_head_sec,
            tail_sec=config.say_pad_tail_sec,
        )
        return audio_bytes


_tts: Optional[GPTSoVITSTTS] = None


def _get_tts() -> GPTSoVITSTTS:
    global _tts
    if _tts is None:
        _tts = GPTSoVITSTTS(config.say_api_base, api_name="/inference")
    return _tts


# =========================================================
# ▼▼▼ 发送语音：base64 record ▼▼▼
# =========================================================

async def _send_audio(bot: Bot, event: MessageEvent, audio_bytes: bytes):
    b64 = base64.b64encode(audio_bytes).decode()
    await bot.send(event, MessageSegment.record(f"base64://{b64}"))


# =========================================================
# ▼▼▼ 统一指令处理：cnsay / jpsay / ensay ▼▼▼
# =========================================================

# 语言映射表
LANG_MAP = {
    "cnsay": "zh",
    "jpsay": "ja",
    "ensay": "en"
}

# 统一匹配所有 say 命令
_say_handler = on_regex(r"^\s*(cnsay|jpsay|ensay)\s+(?P<text>.+?)\s*$", priority=5, block=True)


@_say_handler.handle()
async def _(bot: Bot, event: MessageEvent):
    if not _allow(config.say_cooldown_sec):
        await _say_handler.finish("慢一点~")

    full = event.get_plaintext().strip()
    m = re.match(r"^\s*(cnsay|jpsay|ensay)\s+(?P<text>.+?)\s*$", full, re.I)
    if not m:
        return

    cmd = m.group(1).lower()
    target_lang = LANG_MAP.get(cmd, "zh")

    text = m.group("text").strip()
    text = _normalize_tts_text(text, target_lang)
    chunks = _chunk_text(text, config.say_max_chars)
    if not chunks:
        await _say_handler.finish("你要我说什么呀？")

    ch = chunks[0]
    logger.info(f"[say] target={target_lang} chunks={len(chunks)} text='{ch}'...")

    try:
        tts = _get_tts()
        audio_bytes = await tts.synth(ch, target_lang=target_lang)
    except Exception as e:
        logger.exception(e)
        await _say_handler.finish(f"语音合成失败：{e}")

    try:
        await _send_audio(bot, event, audio_bytes)
    except Exception as e:
        logger.exception(e)
        await _say_handler.finish(f"语音发送失败：{e}")
