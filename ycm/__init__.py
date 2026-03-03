from nonebot import on_regex
from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent
from typing import Dict, List
import re
from datetime import datetime, timedelta

# 存储房间信息的字典
# 格式: {群号: [{"room_id": 房间号, "owner": 车主QQ, "count": 当前人数, "members": [成员QQ列表], "create_time": 创建时间, "is_full": 是否已满员}, ...]}
rooms: Dict[int, List[Dict[str, any]]] = {}


# =========================================================
# 工具函数：检查并清理过期房间（静默清理）
# =========================================================
def clean_expired_rooms(group_id: int):
    """清理超过1小时的房间（静默）"""
    if group_id not in rooms:
        return

    now = datetime.now()
    expired_rooms = []

    for room in rooms[group_id]:
        if now - room["create_time"] > timedelta(hours=1):
            expired_rooms.append(room)

    for room in expired_rooms:
        rooms[group_id].remove(room)

    if len(rooms[group_id]) == 0:
        del rooms[group_id]


# =========================================================
# 工具函数：检查用户是否已在某个房间
# =========================================================
def find_user_room(group_id: int, user_id: str):
    """查找用户所在的房间"""
    if group_id not in rooms:
        return None

    for room in rooms[group_id]:
        if user_id in room["members"]:
            return room
    return None


# =========================================================
# 1. 创建房间 (kc xxxxx / 开车 xxxxx / @全体成员 kc xxxxx / @全体成员 开车 xxxxx / @全体成员 xxxxx)
# =========================================================
create_room_matcher = on_regex(
    r"^(\[CQ:at,qq=all\]\s*)((kc|开车)\s*)?(\d{5})$",
    priority=10,
    block=True
)


@create_room_matcher.handle()
async def handle_create_room(event: Event):
    """创建新房间"""
    if not isinstance(event, GroupMessageEvent):
        return

    group_id = event.group_id
    user_id = event.get_user_id()

    # 静默清理过期房间
    clean_expired_rooms(group_id)

    # 提取房间号 - 支持三种格式：
    # 1. @全体成员 kc 12345
    # 2. @全体成员 开车 12345
    # 3. @全体成员 12345
    # 4. kc 12345
    # 5. 开车 12345
    match = re.match(r"^(\[CQ:at,qq=all\]\s*)((kc|开车)\s*)?(\d{5})$", event.get_plaintext())
    room_id = match.group(4)

    # 验证房间号范围（00001-99999）
    room_id_int = int(room_id)
    if room_id_int < 1 or room_id_int > 99999:
        await create_room_matcher.finish("房间号必须在 00001-99999 范围内！")

    # 初始化群的房间列表
    if group_id not in rooms:
        rooms[group_id] = []

    # 检查用户是否已经在某个房间
    existing_room = find_user_room(group_id, user_id)
    if existing_room:
        await create_room_matcher.finish(f"你已经在房间 {existing_room['room_id']} 了，一个人只能上一辆车哦！")

    # 检查房间号是否已存在
    for room in rooms[group_id]:
        if room["room_id"] == room_id:
            await create_room_matcher.finish(f"房间号 {room_id} 已存在，请换一个房间号！")

    # 创建新房间
    new_room = {
        "room_id": room_id,
        "owner": user_id,
        "count": 1,
        "members": [user_id],
        "create_time": datetime.now(),
        "is_full": False
    }
    rooms[group_id].append(new_room)

    owner_qq = event.get_user_id()
    owner_name = None
    try:
        # OneBot 事件一般会带 sender
        sender = getattr(event, "sender", None) or {}
        owner_name = (sender.get("card") or sender.get("nickname") or "").strip() or None
    except Exception:
        owner_name = None

    rooms[group_id][room_id] = {
        "room_id": room_id,
        "owner": owner_qq,
        "owner_name": owner_name or str(owner_qq),
        "count": 1,
        "members": [owner_qq],
        "create_time": datetime.now(),
        "is_full": False,
    }

    print(f"[房间系统] 群 {group_id} 用户 {user_id} 创建房间 {room_id}")
    await create_room_matcher.finish(f"已创建多人房间，房间号为 {room_id}，当前人数 1")


# =========================================================
# 2. 查询房间 (ycm / 有车没)
# =========================================================
query_room_matcher = on_regex(
    r"^(ycm|有车没)$",
    priority=10,
    block=True
)


@query_room_matcher.handle()
async def handle_query_room(event: Event):
    """查询当前所有房间"""
    if not isinstance(event, GroupMessageEvent):
        return

    group_id = event.group_id

    # 静默清理过期房间
    clean_expired_rooms(group_id)

    # 检查是否有房间
    if group_id not in rooms or len(rooms[group_id]) == 0:
        await query_room_matcher.finish("没有车！快来开车吧！")

    # 构建房间列表信息
    room_list = []
    for idx, room in enumerate(rooms[group_id], 1):
        room_id = room["room_id"]
        count = room["count"]
        status = "【已满员】" if room["is_full"] else ""
        room_list.append(f"{idx}. 房间号 {room_id}，当前人数 {count} {status}")

    result = "有车！当前房间：\n" + "\n".join(room_list)
    await query_room_matcher.finish(result)


# =========================================================
# 3. 上车 (车++/wscl/车+1/sc/上车 [1-4] [房间号])
# =========================================================
board_room_matcher = on_regex(
    r"^(车\+\+|wscl|车\+1|(sc|上车)(\s+([1-4]))?(\s+(\d{5}))?)$",
    priority=10,
    block=True
)


@board_room_matcher.handle()
async def handle_board_room(event: Event):
    """上车（增加人数）"""
    if not isinstance(event, GroupMessageEvent):
        return

    group_id = event.group_id
    user_id = event.get_user_id()
    message = event.get_plaintext()

    # 静默清理过期房间
    clean_expired_rooms(group_id)

    # 检查是否有房间
    if group_id not in rooms or len(rooms[group_id]) == 0:
        await board_room_matcher.finish("当前没有房间，请先开车！")

    # 检查用户是否已在某个房间
    existing_room = find_user_room(group_id, user_id)
    if existing_room:
        await board_room_matcher.finish(f"你已经在房间 {existing_room['room_id']} 了，不能重复上车！")

    # 解析上车人数和目标房间号
    add_count = 1
    target_room_id = None

    if message in ["车++", "wscl", "车+1"]:
        add_count = 1
    else:
        match = re.match(r"^(sc|上车)(\s+([1-4]))?(\s+(\d{5}))?$", message)
        if match.group(3):
            add_count = int(match.group(3))
        if match.group(5):
            target_room_id = match.group(5)

    # 确定目标房间
    target_room = None

    # 如果只有一辆车，直接使用
    if len(rooms[group_id]) == 1:
        target_room = rooms[group_id][0]
    elif target_room_id:
        # 指定了房间号
        for room in rooms[group_id]:
            if room["room_id"] == target_room_id:
                target_room = room
                break
        if not target_room:
            await board_room_matcher.finish(f"房间号 {target_room_id} 不存在！")
    else:
        # 多辆车且没指定房间号
        await board_room_matcher.finish("当前有多辆车，请指定房间号，例如：上车 12345")

    # 检查房间是否已满员
    if target_room["is_full"]:
        await board_room_matcher.finish(
            f"房间 {target_room['room_id']} 已满员发车，无法再上车！请等待有人下车或上其他车。")

    current_count = target_room["count"]
    new_count = current_count + add_count

    # 检查是否超过5人
    if new_count > 5:
        await board_room_matcher.finish(
            f"车已满！房间 {target_room['room_id']} 当前人数 {current_count}，无法再上 {add_count} 人")

    # 更新人数（这里简化处理，只记录发起者）
    target_room["count"] = new_count
    target_room["members"].append(user_id)
    room_id = target_room["room_id"]

    print(f"[房间系统] 群 {group_id} 用户 {user_id} 上车 {add_count} 人到房间 {room_id}，当前 {new_count} 人")

    # 如果满5人，标记为已满员但不删除
    if new_count == 5:
        target_room["is_full"] = True
        await board_room_matcher.finish(f"房间 {room_id} 已满员（5/5），发车啦！🚗\n房间将保留，请车主使用「砸车」指令清理。")
    else:
        await board_room_matcher.finish(f"上车成功！房间号 {room_id}，当前人数 {new_count}")


# =========================================================
# 4. 下车 (车--/wxcl/车-1/xc/下车 [1-4] [房间号])
# =========================================================
leave_room_matcher = on_regex(
    r"^(车--|wxcl|车-1|(xc|下车)(\s+([1-4]))?(\s+(\d{5}))?)$",
    priority=10,
    block=True
)


@leave_room_matcher.handle()
async def handle_leave_room(event: Event):
    """下车（减少人数）"""
    if not isinstance(event, GroupMessageEvent):
        return

    group_id = event.group_id
    user_id = event.get_user_id()
    message = event.get_plaintext()

    # 静默清理过期房间
    clean_expired_rooms(group_id)

    # 检查是否有房间
    if group_id not in rooms or len(rooms[group_id]) == 0:
        await leave_room_matcher.finish("当前没有房间！")

    # 解析下车人数和目标房间号
    sub_count = 1
    target_room_id = None

    if message in ["车--", "wxcl", "车-1"]:
        sub_count = 1
    else:
        match = re.match(r"^(xc|下车)(\s+([1-4]))?(\s+(\d{5}))?$", message)
        if match.group(3):
            sub_count = int(match.group(3))
        if match.group(5):
            target_room_id = match.group(5)

    # 确定目标房间
    target_room = None

    # 如果只有一辆车，直接使用
    if len(rooms[group_id]) == 1:
        target_room = rooms[group_id][0]
    elif target_room_id:
        for room in rooms[group_id]:
            if room["room_id"] == target_room_id:
                target_room = room
                break
        if not target_room:
            await leave_room_matcher.finish(f"房间号 {target_room_id} 不存在！")
    else:
        # 多辆车且没指定房间号
        await leave_room_matcher.finish("当前有多辆车，请指定房间号，例如：下车 12345")

    # 房主不能下车
    if target_room["owner"] == user_id:
        await leave_room_matcher.finish(f"车主不能下车！请使用「砸车」指令解散房间。")

    # 检查用户是否在这辆车上
    if user_id not in target_room["members"]:
        await leave_room_matcher.finish(f"你不在房间 {target_room['room_id']} 上，无法下车！")

    current_count = target_room["count"]
    new_count = current_count - sub_count

    # 检查是否人数为0或负数
    if new_count <= 0:
        await leave_room_matcher.finish("人数不能为0，请使用「砸车」指令解散房间！")

    # 更新人数
    target_room["count"] = new_count
    target_room["members"].remove(user_id)

    # 如果之前是满员状态，下车后解除满员，重新开放上车
    was_full = target_room["is_full"]
    if was_full and new_count < 5:
        target_room["is_full"] = False

    room_id = target_room["room_id"]

    print(f"[房间系统] 群 {group_id} 用户 {user_id} 下车 {sub_count} 人从房间 {room_id}，当前 {new_count} 人")

    if was_full and new_count < 5:
        await leave_room_matcher.finish(f"下车成功！房间号 {room_id}，当前人数 {new_count}\n房间已重新开放上车！")
    else:
        await leave_room_matcher.finish(f"下车成功！房间号 {room_id}，当前人数 {new_count}")


# =========================================================
# 5. 砸车 (砸车 [xxxxx] / zc [xxxxx] / 砸车所有 / zcsy / 没车 / mc)
# =========================================================
cancel_room_matcher = on_regex(
    r"^(砸车所有|zcsy)$|^(砸车|zc)(\s+(\d{5}))?$|^(没车|mc)$",
    priority=10,
    block=True
)


@cancel_room_matcher.handle()
async def handle_cancel_room(event: Event):
    """砸车（注销房间）"""
    if not isinstance(event, GroupMessageEvent):
        return

    group_id = event.group_id
    user_id = event.get_user_id()
    message = event.get_plaintext()

    # 静默清理过期房间
    clean_expired_rooms(group_id)

    # 检查是否有房间
    if group_id not in rooms or len(rooms[group_id]) == 0:
        await cancel_room_matcher.finish("当前没有房间！")

    # 砸车所有
    if message in ["砸车所有", "zcsy"]:
        room_count = len(rooms[group_id])
        del rooms[group_id]
        print(f"[房间系统] 群 {group_id} 用户 {user_id} 砸掉所有车（共{room_count}辆）")
        await cancel_room_matcher.finish(f"已砸掉所有车（共 {room_count} 辆）")

    # 解析目标房间号
    target_room_id = None
    match = re.match(r"^(砸车|zc)(\s+(\d{5}))?$", message)
    if match and match.group(3):
        target_room_id = match.group(3)

    # 确定要砸的车
    if target_room_id:
        # 指定了房间号，砸指定的车
        target_room = None
        for room in rooms[group_id]:
            if room["room_id"] == target_room_id:
                target_room = room
                break

        if not target_room:
            await cancel_room_matcher.finish(f"房间号 {target_room_id} 不存在！")

        # 只有车主才能砸指定的车
        if target_room["owner"] != user_id:
            await cancel_room_matcher.finish(f"只有车主才能砸车！房间 {target_room_id} 的车主不是你。")

        room_id = target_room["room_id"]
        rooms[group_id].remove(target_room)
        if len(rooms[group_id]) == 0:
            del rooms[group_id]

        print(f"[房间系统] 群 {group_id} 用户 {user_id} 砸车 {room_id}")
        await cancel_room_matcher.finish(f"房间 {room_id} 已砸车")

    else:
        # 没指定房间号，砸自己开的车
        user_rooms = [room for room in rooms[group_id] if room["owner"] == user_id]

        if not user_rooms:
            await cancel_room_matcher.finish("你没有开车，无法砸车！")

        # 砸自己的车（辆车）
        target_room = user_rooms[0]
        room_id = target_room["room_id"]
        rooms[group_id].remove(target_room)
        if len(rooms[group_id]) == 0:
            del rooms[group_id]

        print(f"[房间系统] 群 {group_id} 用户 {user_id} 砸车 {room_id}（自己的车）")
        await cancel_room_matcher.finish(f"房间 {room_id} 已砸车")


# =========================================================
# 6. 换车 (hc xxxxx / 换车 xxxxx)
# =========================================================
change_room_matcher = on_regex(
    r"^(hc|换车)\s*(\d{5})$",
    priority=10,
    block=True
)


@change_room_matcher.handle()
async def handle_change_room(event: Event):
    """换车（更换自己的房间号）"""
    if not isinstance(event, GroupMessageEvent):
        return

    group_id = event.group_id
    user_id = event.get_user_id()

    # 静默清理过期房间
    clean_expired_rooms(group_id)

    # 提取新房间号
    match = re.match(r"^(hc|换车)\s*(\d{5})$", event.get_plaintext())
    new_room_id = match.group(2)

    # 验证房间号范围（00001-99999）
    room_id_int = int(new_room_id)
    if room_id_int < 1 or room_id_int > 99999:
        await change_room_matcher.finish("房间号必须在 00001-99999 范围内！")

    # 初始化群的房间列表
    if group_id not in rooms:
        rooms[group_id] = []

    # 检查新房间号是否已存在
    for room in rooms[group_id]:
        if room["room_id"] == new_room_id:
            await change_room_matcher.finish(f"房间号 {new_room_id} 已存在，请换一个房间号！")

    # 找到用户自己开的车
    user_rooms = [room for room in rooms[group_id] if room["owner"] == user_id]

    if user_rooms:
        # 换自己的车（一个用户只有一辆车）
        old_room = user_rooms[0]
        old_room_id = old_room["room_id"]
        old_count = old_room["count"]
        old_room["room_id"] = new_room_id

        print(f"[房间系统] 群 {group_id} 用户 {user_id} 换车：{old_room_id} -> {new_room_id}")
        await change_room_matcher.finish(
            f"已换车！新房间号为 {new_room_id}，当前人数 {old_count}"
        )
    else:
        # 没有自己的车，创建新车
        new_room = {
            "room_id": new_room_id,
            "owner": user_id,
            "count": 1,
            "members": [user_id],
            "create_time": datetime.now(),
            "is_full": False
        }
        rooms[group_id].append(new_room)

        print(f"[房间系统] 群 {group_id} 用户 {user_id} 换车（新建）：{new_room_id}")
        await change_room_matcher.finish(
            f"已创建新房间，房间号为 {new_room_id}，当前人数 1"
        )
