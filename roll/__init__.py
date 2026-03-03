import random
from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.rule import Rule
from nonebot.log import logger

USAGE = "用法：@我 发送“A还是B还是C”我来帮你选😋"


def _at_me(event: GroupMessageEvent) -> bool:
    """
    优先用 OneBot 自带 to_me 判定（最稳），再兜底查 at 段
    """
    # OneBot v11 GroupMessageEvent 通常有 is_tome()
    try:
        if event.is_tome():
            return True
    except Exception:
        pass

    # 兜底：手动查 at 段
    for seg in event.message:
        if seg.type == "at":
            qq = seg.data.get("qq")
            if qq is None:
                continue
            if str(qq) == str(event.self_id):
                return True
    return False


def _should_choose(event: GroupMessageEvent) -> bool:
    """
    只在 @我 且（包含“还是”或是关键词）时触发
    """
    text = event.get_plaintext().strip()
    if text in {"决定", "选择", "帮选"}:
        return True
    return "还是" in text


roll = on_message(
    rule=Rule(_at_me) & Rule(_should_choose),
    priority=-9999,  # 抢在 chat 前
    block=True       # 命中就拦截，避免 chat 再处理
)


@roll.handle()
async def _(event: GroupMessageEvent):
    raw = getattr(event, "raw_message", "")
    plain = event.get_plaintext()
    logger.warning(f"[roll] got raw={raw!r} plain={plain!r} to_me={getattr(event, 'to_me', None)!r}")

    msg = plain.strip()

    # 关键词提示
    if msg in {"决定", "选择", "帮选"}:
        await roll.finish(USAGE)

    # 选择逻辑
    if "还是还是" in msg:
        await roll.finish("你这个问法有点小狡猾啦～把每个选项写完整我才好帮你选🥰")

    parts = [p.strip() for p in msg.split("还是")]
    if any(p == "" for p in parts):
        await roll.finish("选项里好像有空的呀～再写清楚一点我就能帮你决定啦🥰")

    if len(parts) < 2:
        return

    pick = random.choice(parts)
    await roll.finish(f"嘻嘻，小爱莉建议你 {pick} 哟🥰")
