import re
from typing import Dict, Optional
from nonebot import on_message
from pathlib import Path
from nonebot import on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    GroupDecreaseNoticeEvent,
    MessageSegment,
)
# =========================================================
# ▼▼▼ 复读机 (Repeater) ▼▼▼
# =========================================================
# 功能：同一群内连续两条相同消息 -> 机器人自动再发一遍
# 记录每个群“上一条消息”的 CQ 字符串（精确比较用）
_last_msg_by_group: Dict[int, str] = {}

# 监听“所有群消息”
repeater_matcher = on_message(priority=50, block=False)


@repeater_matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    """
    复读机核心逻辑：
    1) 只处理群消息（GroupMessageEvent）
    2) 忽略机器人自己发的消息（防止循环）
    3) 使用 CQ 字符串精确比较（表情/CQ码也算）
    4) 连续两条相同 -> 复读一次，并清空上一条，避免“被机器人打断却仍算连续”
    """

    # ---------- 1) 忽略机器人自己发的消息，防止无限复读 ----------
    if str(event.user_id) == str(event.self_id):
        return

    group_id = event.group_id

    # ---------- 2) 把消息转成 CQ 字符串（最“严格”的等价判断） ----------
    # 例：纯文本 "表情"；或 "[CQ:face,id=123]"；或混合消息等
    msg_cq = str(event.message)

    # ---------- 3) 正则过滤：空消息/纯空白不参与复读 ----------
    # 你想更严格也很方便改：比如只允许纯文本等
    if not re.fullmatch(r"(?!\s*$).+", msg_cq):
        return

    # ---------- 4) 取出该群上一条消息 ----------
    last_msg: Optional[str] = _last_msg_by_group.get(group_id)

    # ---------- 5) 判断是否“连续两条完全一致” ----------
    if last_msg is not None and msg_cq == last_msg:
        # ✅ 连续两条相同：复读一次
        await bot.send(event, message=event.message)

        # ✅ 关键：清空上一条，避免机器人发言后仍被当作“连续”
        # （因为我们忽略 bot 自己的消息，所以要手动打断状态）
        _last_msg_by_group.pop(group_id, None)
        return

    # ---------- 6) 否则更新“上一条消息” ----------
    _last_msg_by_group[group_id] = msg_cq


# =========================================================
# ▼▼▼ 欢迎/退群 (Welcome & Farewell) ▼▼▼
# =========================================================

WELCOME_GIF = Path(r"F:\useful\223E325DABB40FCAE0CB876AF7B4C7B8.gif")
FAREWELL_PNG = Path(r"F:\useful\stamp0114.png")

# 统一用 on_notice，分别处理 group_increase / group_decrease
notice_matcher = on_notice(priority=30, block=False)


def _file_uri(p: Path) -> str:
    """
    把 Windows 路径变成 file:// URI，OneBot 本地文件发送最稳的写法之一。
    注意：这个路径必须是“运行 OneBot 的那台机器”能访问到的路径。
    """
    return p.resolve().as_uri()


def _is_nonempty(s: str) -> bool:
    """用正则做一个直观的非空判断（方便你学习 fullmatch 用法）"""
    return re.fullmatch(r"(?!\s*$).+", s) is not None


@notice_matcher.handle()
async def _(bot: Bot, event):
    # ---------- 1) 欢迎新人：group_increase ----------
    if isinstance(event, GroupIncreaseNoticeEvent):
        await bot.send(event=event, message="欢迎新龙龙")
        await bot.send(event=event, message=MessageSegment.image(_file_uri(WELCOME_GIF)))
        return

    # ---------- 2) 退群/被踢：group_decrease ----------
    if isinstance(event, GroupDecreaseNoticeEvent):
        group_id = event.group_id
        user_id = event.user_id

        name = str(user_id)

        # 1) 先尝试群成员信息（可能因为已退群而失败）
        try:
            info = await bot.call_api(
                "get_group_member_info",
                group_id=group_id,
                user_id=user_id,
                no_cache=False,
            )
            card = (info.get("card") or "").strip()
            nickname = (info.get("nickname") or "").strip()
            if _is_nonempty(card):
                name = card
            elif _is_nonempty(nickname):
                name = nickname
        except Exception:
            pass

        # 2) 如果还只是QQ号，再尝试陌生人信息（通常能拿到QQ昵称）
        if name == str(user_id):
            try:
                s = await bot.call_api("get_stranger_info", user_id=user_id, no_cache=False)
                nickname = (s.get("nickname") or "").strip()
                if _is_nonempty(nickname):
                    name = nickname
            except Exception:
                pass

        text = f"呜呜，{name}永远离开了我们的sekai😭"
        await bot.send(event=event, message=MessageSegment.text(text) + MessageSegment.image(_file_uri(FAREWELL_PNG)))
        return
# =========================================================
# ▼▼▼ 五数百分制计算 (Strict) ▼▼▼
# =========================================================
PATTERN = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$")

matcher = on_message(priority=50, block=False)


@matcher.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    text = event.get_plaintext()
    m = PATTERN.fullmatch(text)
    if not m:
        return  # 不符合格式：不提醒，直接无响应

    a, b, c, d, e = map(int, m.groups())

    nums = [a, b, c, d, e]
    if any(n < 0 or n > 200 for n in nums):
        return

    if a % 5 != 0:
        return

    engine = a + (b + c + d + e) * 0.2

    score = engine + 100
    multi = score / 100

    def fmt_percent(x: float) -> str:
        return f"{int(x)}" if abs(x - int(x)) < 1e-9 else f"{x: .2f}"

    line1 = f"实际发动机能时的倍率为：{fmt_percent(engine)}%"
    line2 = f"实际得分为原本的：{fmt_percent(score)}%，即{multi: .2f}倍"

    reply = MessageSegment.reply(event.message_id)
    await bot.send(event=event, message=reply + MessageSegment.text(line1 + "\n" + line2))
