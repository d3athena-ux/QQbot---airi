import base64
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple, Any, List, Dict

import aiohttp
from nonebot import logger, on_regex
from nonebot.exception import FinishedException
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import GroupMessageEvent

# =========================================================
# ▼▼▼ 配置区 ▼▼▼
# =========================================================

# ✅ 绝对最高优先级：负数越小越先执行
PRIORITY_ABSOLUTE_HIGHEST = -10000

# ffmpeg 可执行文件（已加 PATH 就用 "ffmpeg"，否则填绝对路径）
FFMPEG_PATH = "ffmpeg"

# 临时目录
TMP_DIR = Path(tempfile.gettempdir()) / "pjskbot_reverse_cache"
TMP_DIR.mkdir(parents=True, exist_ok=True)

# =========================================================
# ▼▼▼ 去重：防止重复事件/重复加载导致发两条 ▼▼▼
# - 内存去重：防“重复投递”
# - 文件锁：防“插件被加载两次”
# =========================================================
_DEDUP_TTL_SEC = 12
_processed: Dict[int, float] = {}

LOCK_DIR = TMP_DIR / "locks"
LOCK_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_locks(ttl_sec: int = 30) -> None:
    now = time.time()
    for p in LOCK_DIR.glob("*.lock"):
        try:
            if now - p.stat().st_mtime > ttl_sec:
                p.unlink(missing_ok=True)
        except Exception:
            pass


def acquire_lock(message_id: int) -> Optional[Path]:
    _cleanup_locks()
    lock_path = LOCK_DIR / f"{message_id}.lock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(time.time()).encode("utf-8"))
        os.close(fd)
        return lock_path
    except FileExistsError:
        return None


def release_lock(lock_path: Optional[Path]) -> None:
    if not lock_path:
        return
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass


def is_duplicate_event(message_id: int) -> bool:
    now = time.time()
    for k in list(_processed.keys()):
        if now - _processed[k] > _DEDUP_TTL_SEC:
            _processed.pop(k, None)

    if message_id in _processed:
        return True

    _processed[message_id] = now
    return False


# =========================================================
# ▼▼▼ 触发器：严格匹配 “倒放” ▼▼▼
# =========================================================
reverse_matcher = on_regex(r"^倒放$", priority=PRIORITY_ABSOLUTE_HIGHEST, block=True)


# =========================================================
# ▼▼▼ 日志：严格拼成你给的格式 ▼▼▼
# =========================================================
def log_like_nonebot_success(event: GroupMessageEvent) -> None:
    msg_text = str(event.message)
    if event.reply:
        msg_text = f"[reply:id={event.reply.message_id}]{msg_text}"

    logger.success(
        f"nonebot | OneBot V11 {event.self_id} | [message.group.{event.sub_type}]: "
        f"Message {event.message_id} from {event.user_id}@[群:{event.group_id}] "
        f"'{msg_text}'"
    )


# =========================================================
# ▼▼▼ 网络下载 ▼▼▼
# =========================================================
async def download_bytes(url: str) -> bytes:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


# =========================================================
# ▼▼▼ NapCat get_msg 兼容：list[dict] -> MessageSegment -> Message ▼▼▼
# =========================================================
def _raw_message_to_message(raw: Any) -> Message:
    if isinstance(raw, Message):
        return raw
    if isinstance(raw, str):
        return Message(raw)
    segs: List[MessageSegment] = []
    for s in raw:
        segs.append(MessageSegment(type=s.get("type"), data=s.get("data") or {}))
    return Message(segs)


async def extract_reply_media(bot: Bot, event: GroupMessageEvent) -> Tuple[Optional[MessageSegment], Optional[str]]:
    if not event.reply:
        return None, None

    # 有些实现 reply 自带 message
    if getattr(event.reply, "message", None):
        reply_msg = event.reply.message
    else:
        data = await bot.get_msg(message_id=event.reply.message_id)
        reply_msg = _raw_message_to_message(data.get("message"))

    for seg in reply_msg:
        if seg.type == "image":
            return seg, "image"
        if seg.type == "record":
            return seg, "record"
    return None, None


# =========================================================
# ▼▼▼ ffmpeg 执行器：打印尾部20行 ▼▼▼
# =========================================================
def run_ffmpeg(cmd: List[str]) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode == 0:
        return
    stderr = p.stderr or ""
    stdout = p.stdout or ""
    tail = "\n".join((stderr.splitlines() or stdout.splitlines())[-20:])
    raise RuntimeError(f"ffmpeg failed (code={p.returncode}):\n{tail}")


# =========================================================
# ▼▼▼ 动图倒放：展开完整帧 + reverse（解决残影）▼▼▼
# =========================================================
def reverse_animated_image_bytes_ffmpeg(data: bytes, tag: str) -> bytes:
    in_path = TMP_DIR / f"{tag}_in.bin"
    out_path = TMP_DIR / f"{tag}_out.gif"
    in_path.write_bytes(data)

    filter_complex = (
        "[0:v]format=rgba,reverse,setpts=N/FRAME_RATE/TB,split[v1][v2];"
        "[v1]palettegen=stats_mode=diff[p];"
        "[v2][p]paletteuse=new=1"
    )

    cmd = [
        FFMPEG_PATH, "-y",
        "-hide_banner", "-loglevel", "error",
        "-i", str(in_path),
        "-filter_complex", filter_complex,
        "-loop", "0",
        str(out_path),
    ]
    run_ffmpeg(cmd)
    return out_path.read_bytes()


# =========================================================
# ▼▼▼ 音频滤镜：HiFi 但不破音 ▼▼▼
# =========================================================
VOICE_FILTER_HIFI = (
    "aresample=48000,"
    "areverse,"
    "highpass=f=80,"
    "acompressor=threshold=-18dB:ratio=3:attack=5:release=120,"
    "alimiter=limit=0.90"
)

PLAIN_FILTER_HIFI = (
    "aresample=48000,"
    "areverse,"
    "acompressor=threshold=-16dB:ratio=2.5:attack=5:release=120,"
    "alimiter=limit=0.92"
)


def choose_audio_filter(is_voice_like: bool) -> str:
    return VOICE_FILTER_HIFI if is_voice_like else PLAIN_FILTER_HIFI


# =========================================================
# ▼▼▼ 主处理：回复/引用媒体，然后发送“倒放” ▼▼▼
# =========================================================
@reverse_matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    lock_path: Optional[Path] = None
    try:
        # ✅ 内存去重（重复投递）
        if is_duplicate_event(event.message_id):
            return

        # ✅ 文件锁去重（插件重复加载也不会发两条）
        lock_path = acquire_lock(event.message_id)
        if lock_path is None:
            return

        log_like_nonebot_success(event)

        seg, kind = await extract_reply_media(bot, event)
        if not seg or not kind:
            await reverse_matcher.finish("用法：请【回复/引用】一条【动图/动画表情 或 语音】消息，然后发送“倒放”")
            return

        tag = f"{event.group_id}_{event.user_id}_{event.message_id}"

        # ========== 动图/图片 ==========
        if kind == "image":
            url = seg.data.get("url")
            if not url:
                await reverse_matcher.finish("获取图片失败：image 段没有 url（NapCat 一般会带）")
                return

            raw = await download_bytes(url)
            out_gif = reverse_animated_image_bytes_ffmpeg(raw, tag=tag)

            b64 = base64.b64encode(out_gif).decode("utf-8")
            await reverse_matcher.finish(MessageSegment.image(f"base64://{b64}"))
            return

        # ========== 语音/音频 ==========
        if kind == "record":
            url = seg.data.get("url")
            local_path = seg.data.get("path")
            file_name = (seg.data.get("file") or "").lower()

            in_path = TMP_DIR / f"{tag}_input.bin"
            out_mp3 = TMP_DIR / f"{tag}_reverse_hifi.mp3"

            # 1) 输入：优先 url（更标准）
            if url:
                raw = await download_bytes(url)
                in_path.write_bytes(raw)
            elif local_path and os.path.exists(local_path):
                in_path = Path(local_path)
                raw = in_path.read_bytes()
            else:
                await reverse_matcher.finish("获取语音失败：record 段既没有 url，也没有有效 path。")
                return

            # 语音判断
            is_voice_like = (
                ("format=amr" in (url or "").lower())
                or file_name.endswith(".amr")
                or str(in_path).lower().endswith(".amr")
            )
            af = choose_audio_filter(is_voice_like)

            # 2) 高质量 MP3 输出：VBR q=2
            try:
                cmd_mp3 = [
                    FFMPEG_PATH, "-y",
                    "-hide_banner", "-loglevel", "error",
                    "-i", str(in_path),
                    "-af", af,
                    "-ar", "44100",
                    "-c:a", "libmp3lame",
                    "-q:a", "2",
                    str(out_mp3),
                ]
                run_ffmpeg(cmd_mp3)

                # ✅ 这里 finish 会抛 FinishedException（正常），不要被下面 except 当失败
                await reverse_matcher.finish(MessageSegment.record(file=str(out_mp3)))
                return

            except FinishedException:
                raise
            except Exception as e:
                logger.warning(f"HiFi MP3 编码失败: {e}")

            # 3) 补 AMR 头兜底（极端情况）
            if isinstance(raw, (bytes, bytearray)) and not raw.startswith(b"#!AMR"):
                patched_path = TMP_DIR / f"{tag}_input_patched.amr"
                patched_path.write_bytes(b"#!AMR\n" + raw)
                try:
                    cmd_mp3_patched = [
                        FFMPEG_PATH, "-y",
                        "-hide_banner", "-loglevel", "error",
                        "-f", "amr", "-i", str(patched_path),
                        "-af", af,
                        "-ar", "44100",
                        "-c:a", "libmp3lame",
                        "-q:a", "2",
                        str(out_mp3),
                    ]
                    run_ffmpeg(cmd_mp3_patched)
                    await reverse_matcher.finish(MessageSegment.record(file=str(out_mp3)))
                    return

                except FinishedException:
                    raise
                except Exception as e:
                    logger.warning(f"补头后 MP3 编码失败: {e}")

            await reverse_matcher.finish("语音倒放失败：输入格式无法被 ffmpeg 识别。")
            return

    except FinishedException:
        # finish 的正常结束信号，不算错误
        raise
    except Exception as e:
        logger.exception(e)
        await reverse_matcher.finish("处理失败：请看插件报错。")
        return
    finally:
        release_lock(lock_path)


# =========================================================
# ▼▼▼ 本地测试（可选）▼▼▼
# =========================================================
def test_reverse_gif_file(input_path: str, output_path: str) -> None:
    data = Path(input_path).read_bytes()
    out = reverse_animated_image_bytes_ffmpeg(data, tag="localtest_gif")
    Path(output_path).write_bytes(out)
    print(f"[OK] saved: {output_path}")


if __name__ == "__main__":
    # test_reverse_gif_file("input.gif", "output_reverse.gif")
    pass
