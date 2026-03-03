from nonebot import on_regex, logger
from nonebot.adapters.onebot.v11 import MessageSegment, Bot, GroupMessageEvent
from nonebot.exception import FinishedException
from nonebot.params import RegexGroup
from PIL import Image, ImageOps
import httpx
import io
import asyncio
import traceback
import numpy as np
import math
import base64
from functools import partial

# =========================================================
# 工具函数
# =========================================================

def _to_b64file(data: bytes) -> str:
    """转换为base64格式"""
    return "base64://" + base64.b64encode(data).decode("ascii")


def _limit_size(img: Image.Image, max_side: int = 800) -> Image.Image:
    """限制图片尺寸"""
    w, h = img.size
    if max(w, h) <= max_side:
        return img
    scale = max_side / max(w, h)
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


async def get_image_data(event: GroupMessageEvent) -> tuple:
    """
    获取图片数据
    优先级：引用图片 > 消息图片 > @头像
    返回: (img_data: bytes, source_type: str)
    """
    reply_img_url = None
    msg_img_url = None
    at_qq = None

    # 优先检查引用消息中的图片
    if event.reply:
        for seg in event.reply.message:
            if seg.type == "image":
                reply_img_url = seg.data.get("url")
                logger.info("从引用消息中获取图片")
                break

    # 检查当前消息中的图片
    if not reply_img_url:
        for seg in event.original_message:
            if seg.type == "image":
                msg_img_url = seg.data.get("url")
                logger.info("从消息中获取图片")
                break

    # 最后检查@
    if not reply_img_url and not msg_img_url:
        for seg in event.original_message:
            if seg.type == "at":
                at_qq = seg.data.get("qq")
                break

    # 确定URL
    target_url = None
    if reply_img_url:
        target_url = reply_img_url
    elif msg_img_url:
        target_url = msg_img_url
    elif at_qq:
        target_url = f"https://q1.qlogo.cn/g?b=qq&nk={at_qq}&s=640"
        logger.info(f"获取@用户头像: {at_qq}")
    else:
        return None, None

    # 下载图片
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(target_url)
            if resp.status_code == 200:
                return resp.content, "image"
    except Exception as e:
        logger.error(f"下载图片失败: {e}")

    return None, None


# =========================================================
# 图片处理效果函数
# =========================================================

def effect_symmetry(img_bytes: bytes, direction: str) -> bytes:
    """对称效果"""
    img = Image.open(io.BytesIO(img_bytes))

    # 处理GIF
    if getattr(img, "is_animated", False):
        output_frames = []
        durations = []

        for i in range(img.n_frames):
            img.seek(i)
            frame = img.convert("RGBA")
            w, h = frame.size

            if direction == "左":
                half = frame.crop((0, 0, w // 2, h))
                frame.paste(ImageOps.mirror(half), (w // 2, 0))
            elif direction == "右":
                half = frame.crop((w // 2, 0, w, h))
                frame.paste(ImageOps.mirror(half), (0, 0))
            elif direction == "上":
                half = frame.crop((0, 0, w, h // 2))
                frame.paste(ImageOps.flip(half), (0, h // 2))
            else:  # 下
                half = frame.crop((0, h // 2, w, h))
                frame.paste(ImageOps.flip(half), (0, 0))

            output_frames.append(frame)
            durations.append(img.info.get("duration", 100))

        output = io.BytesIO()
        output_frames[0].save(
            output, format="GIF", save_all=True,
            append_images=output_frames[1:],
            duration=durations, loop=img.info.get("loop", 0), disposal=2
        )
        return output.getvalue()

    # 处理静态图
    img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "PA") else "RGB")
    w, h = img.size

    if direction == "左":
        half = img.crop((0, 0, w // 2, h))
        img.paste(ImageOps.mirror(half), (w // 2, 0))
    elif direction == "右":
        half = img.crop((w // 2, 0, w, h))
        img.paste(ImageOps.mirror(half), (0, 0))
    elif direction == "上":
        half = img.crop((0, 0, w, h // 2))
        img.paste(ImageOps.flip(half), (0, h // 2))
    else:  # 下
        half = img.crop((0, h // 2, w, h))
        img.paste(ImageOps.flip(half), (0, 0))

    output = io.BytesIO()
    img.save(output, format="PNG")
    return output.getvalue()


def effect_fisheye(img_bytes: bytes) -> bytes:
    """鱼眼呼吸效果"""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    width, height = img.size
    center_x, center_y = width / 2, height / 2
    max_radius = math.sqrt(center_x ** 2 + center_y ** 2)

    y_coords, x_coords = np.mgrid[0:height, 0:width]
    dx = x_coords - center_x
    dy = y_coords - center_y
    distances = np.sqrt(dx ** 2 + dy ** 2)
    normalized_distances = np.clip(distances / max_radius, 0, 1)
    img_array = np.array(img)

    frames = []
    for i in range(15):
        strength = 0.5 * math.sin(i / 14 * math.pi)
        factors = 1.0 - strength * (1.0 - normalized_distances ** 2)
        src_x = np.clip(center_x + dx * factors, 0, width - 1).astype(np.int32)
        src_y = np.clip(center_y + dy * factors, 0, height - 1).astype(np.int32)
        frames.append(Image.fromarray(img_array[src_y, src_x], "RGBA"))

    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True,
                   append_images=frames[1:], duration=60, loop=0, disposal=2)
    return output.getvalue()


def effect_invert(img_bytes: bytes) -> bytes:
    """反色效果"""
    img = Image.open(io.BytesIO(img_bytes))

    # 处理GIF
    if getattr(img, "is_animated", False):
        output_frames = []
        durations = []

        for i in range(img.n_frames):
            img.seek(i)
            frame = ImageOps.invert(img.convert("RGB"))
            output_frames.append(frame)
            durations.append(img.info.get("duration", 100))

        output = io.BytesIO()
        output_frames[0].save(
            output, format="GIF", save_all=True,
            append_images=output_frames[1:],
            duration=durations, loop=img.info.get("loop", 0), disposal=2
        )
        return output.getvalue()

    # 处理静态图
    inverted = ImageOps.invert(img.convert("RGB"))
    output = io.BytesIO()
    inverted.save(output, format="PNG")
    return output.getvalue()


def effect_blinds(img_bytes: bytes) -> bytes:
    """百叶窗展开效果"""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    width, height = img.size
    slat_count = 15
    slat_height = max(1, height // slat_count)

    frames = []
    for frame_idx in range(20):
        progress = frame_idx / 19
        new_frame = Image.new("RGBA", (width, height), (0, 0, 0, 0))

        for slat_idx in range(slat_count):
            y_start = slat_idx * slat_height
            y_end = min((slat_idx + 1) * slat_height, height)
            visible_height = int((y_end - y_start) * progress)

            if visible_height > 0:
                slat = img.crop((0, y_start, width, y_start + visible_height))
                new_frame.paste(slat, (0, y_end - visible_height), slat)

        frames.append(new_frame)

    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True,
                   append_images=frames[1:], duration=50, loop=0, disposal=2)
    return output.getvalue()


def effect_windmill(img_bytes: bytes) -> bytes:
    """风车旋转效果"""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    img = _limit_size(img)

    # 转为正方形
    w, h = img.size
    s = max(w, h)
    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    canvas.paste(img, ((s - w) // 2, (s - h) // 2))

    # 缩小为4个tile
    tile = canvas.resize((s // 2, s // 2), Image.LANCZOS)

    # 拼接4格
    base = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    base.paste(tile.rotate(0, expand=False), (0, 0))
    base.paste(tile.rotate(90, expand=False), (s // 2, 0))
    base.paste(tile.rotate(180, expand=False), (s // 2, s // 2))
    base.paste(tile.rotate(270, expand=False), (0, s // 2))

    # 生成旋转动画
    frames = []
    for i in range(24):
        rotated = base.rotate(-i * 15, resample=Image.BICUBIC, expand=False)
        frames.append(rotated)

    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True,
                   append_images=frames[1:], duration=50, loop=0, disposal=2)
    return output.getvalue()


def effect_rotate(img_bytes: bytes) -> bytes:
    """旋转效果"""
    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    img = _limit_size(img)

    # 计算最大尺寸
    orig_w, orig_h = img.size
    canvas_size = int(math.sqrt(orig_w ** 2 + orig_h ** 2)) + 10

    frames = []
    for i in range(24):
        rotated = img.rotate(-i * 15, resample=Image.BICUBIC, expand=True)
        canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
        rot_w, rot_h = rotated.size
        canvas.paste(rotated, ((canvas_size - rot_w) // 2, (canvas_size - rot_h) // 2), rotated)
        frames.append(canvas)

    output = io.BytesIO()
    frames[0].save(output, format="GIF", save_all=True,
                   append_images=frames[1:], duration=50, loop=0, disposal=2)
    return output.getvalue()


# =========================================================
# 通用处理器
# =========================================================

async def process_effect(
        event: GroupMessageEvent,
        matcher,
        effect_func,
        effect_name: str,
        processing_msg: str,
        error_msg: str,
        **kwargs
):
    """通用效果处理器"""
    try:
        logger.info(f"========== {effect_name}效果触发 ==========")
        img_data, _ = await get_image_data(event)

        if not img_data:
            await matcher.finish(error_msg)

        await matcher.send(processing_msg)

        loop = asyncio.get_running_loop()
        func = partial(effect_func, img_data, **kwargs)
        result_bytes = await loop.run_in_executor(None, func)

        await matcher.finish(MessageSegment.image(_to_b64file(result_bytes)))

    except FinishedException:
        raise
    except Exception as e:
        logger.error(f"{effect_name}效果异常: {e}\n{traceback.format_exc()}")
        await matcher.finish(f"❌ 处理失败: {str(e)}")


# =========================================================
# 效果注册
# =========================================================

# 对称效果
sym_matcher = on_regex(r"^对称(左|右|上|下)?$", priority=3, block=True)


@sym_matcher.handle()
async def _(event: GroupMessageEvent, groups: tuple = RegexGroup()):
    direction = groups[0] if groups and groups[0] else "右"
    await process_effect(
        event, sym_matcher, effect_symmetry, "对称",
        f"⏳ 正在进行{direction}对称...",
        "🖼️ 请引用图片/发送图片/@某人，然后说对称~",
        direction=direction
    )


# 鱼眼效果
fisheye_matcher = on_regex(r"^鱼眼$", priority=3, block=True)


@fisheye_matcher.handle()
async def _(event: GroupMessageEvent):
    await process_effect(
        event, fisheye_matcher, effect_fisheye, "鱼眼",
        "⏳ 正在生成鱼眼呼吸动画...",
        "🐟 请引用图片/发送图片/@某人，然后说鱼眼~"
    )


# 反色效果
invert_matcher = on_regex(r"^反色$", priority=3, block=True)


@invert_matcher.handle()
async def _(event: GroupMessageEvent):
    await process_effect(
        event, invert_matcher, effect_invert, "反色",
        "⏳ 正在进行反色处理...",
        "🎨 请引用图片/发送图片/@某人，然后说反色~"
    )


# 百叶窗效果
blinds_matcher = on_regex(r"^百叶窗$", priority=3, block=True)


@blinds_matcher.handle()
async def _(event: GroupMessageEvent):
    await process_effect(
        event, blinds_matcher, effect_blinds, "百叶窗",
        "⏳ 正在生成百叶窗展开动画...",
        "🪟 请引用图片/发送图片/@某人，然后说百叶窗~"
    )


# 风车效果
windmill_matcher = on_regex(r"^风车$", priority=3, block=True)


@windmill_matcher.handle()
async def _(event: GroupMessageEvent):
    await process_effect(
        event, windmill_matcher, effect_windmill, "风车",
        "⏳ 正在生成风车动画...",
        "🌀 请引用图片/发送图片/@某人，然后说风车~"
    )


# 转效果
rotate_matcher = on_regex(r"^转$", priority=3, block=True)


@rotate_matcher.handle()
async def _(event: GroupMessageEvent):
    await process_effect(
        event, rotate_matcher, effect_rotate, "转",
        "⏳ 正在生成旋转动画...",
        "🔄 请引用图片/发送图片/@某人，然后说转~"
    )
