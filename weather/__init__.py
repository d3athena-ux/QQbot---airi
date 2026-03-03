import datetime
import asyncio
from typing import Optional, Dict, Tuple, List

import httpx
import re
from nonebot import on_regex, require, get_bots, logger
from nonebot.params import RegexMatched
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Bot

# =========================================================
# ▼▼▼ 需要用到定时任务：nonebot-plugin-apscheduler ▼▼▼
# =========================================================
require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

# =========================================================
# ▼▼▼ 配置区：你只要改这里 ▼▼▼
# =========================================================

# 每天 08:30 推送到哪些群,以及推送哪个城市(群号: 城市)
WEATHER_PUSH_TARGETS: Dict[int, str] = {
    1077782560: "南京市",
    172947643: "北京市",
    1025487087: "南京市",
    1022858719: "南京市",
    232947193: "南京市",
}

# Clash 代理配置（不用代理就设为 None）
PROXY_URL: Optional[str] = "http://127.0.0.1:7890"

# 中国大陆:优先从地理编码结果里挑 country_code=CN 且人口(population)更高的那个
PREFER_CHINA_RESULT = True

# =========================================================
# ▼▼▼ 天气码 -> 中文描述(常用 WMO weather interpretation codes)▼▼▼
# =========================================================
WEATHER_CODE_ZH = {
    0: "晴",
    1: "大部晴朗",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇雾",
    51: "小毛毛雨",
    53: "中等毛毛雨",
    55: "强毛毛雨",
    56: "小冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "小冻雨",
    67: "大冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "中阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "大阵雪",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴大冰雹",
}


def code_to_text(code: Optional[int]) -> str:
    if code is None:
        return "未知"
    return WEATHER_CODE_ZH.get(int(code), f"未知({code})")


# =========================================================
# ▼▼▼ 一些"建议"生成(穿衣/出行/雨伞/大风等)▼▼▼
# =========================================================
def make_suggestion_today(
    tmin: Optional[float],
    tmax: Optional[float],
    precip_sum: Optional[float],
    wind_max: Optional[float],
    weather_code: Optional[int],
    precip_prob_max: Optional[float] = None,
) -> str:
    tips: List[str] = []

    if tmax is not None:
        if tmax >= 30:
            tips.append("偏热:短袖为主,注意防晒补水")
        elif 20 <= tmax < 30:
            tips.append("舒适:短袖/薄外套即可")
        elif 10 <= tmax < 20:
            tips.append("偏凉:建议长袖+外套")
        elif 0 <= tmax < 10:
            tips.append("较冷:厚外套/毛衣,注意保暖")
        else:
            tips.append("严寒:羽绒服+保暖装备")
    elif tmin is not None and tmin < 5:
        tips.append("偏冷:注意保暖")

    rain_risk = False
    if precip_sum is not None and precip_sum >= 1:
        rain_risk = True
    if precip_prob_max is not None and precip_prob_max >= 50:
        rain_risk = True
    if weather_code is not None and int(weather_code) in {61, 63, 65, 80, 81, 82, 95, 96, 99}:
        rain_risk = True
    if rain_risk:
        tips.append("有降水风险:出门记得带伞")

    if weather_code is not None and int(weather_code) in {95, 96, 99}:
        tips.append("注意雷暴:尽量减少户外停留,避开空旷高处")

    if wind_max is not None:
        if wind_max >= 10:
            tips.append("风很大:注意高空坠物,骑车更要小心")
        elif wind_max >= 7:
            tips.append("风偏大:体感会更冷,外出注意防风")

    if not tips:
        return "无特别提醒,祝你一天顺利~"
    return ";".join(tips) + "。"


# =========================================================
# ▼▼▼ httpx 客户端（兼容不同版本 proxy/proxies 参数）▼▼▼
# =========================================================
def _make_client(timeout: float = 30.0) -> httpx.AsyncClient:
    if PROXY_URL:
        try:
            return httpx.AsyncClient(timeout=timeout, proxy=PROXY_URL)
        except TypeError:
            return httpx.AsyncClient(timeout=timeout, proxies=PROXY_URL)
    return httpx.AsyncClient(timeout=timeout)


# =========================================================
# ▼▼▼ Open-Meteo:地理编码 + 天气请求(带代理和重试)▼▼▼
# =========================================================
async def geocode_city(city: str, retry: int = 3) -> Optional[Tuple[float, float, str]]:
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {
        "name": city,
        "count": 10,
        "language": "zh",
        "format": "json",
        "feature_type": "city",
    }

    data = None
    for attempt in range(retry):
        try:
            async with _make_client(timeout=30.0) as client:
                r = await client.get(url, params=params)
                data = r.json()
                break
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
            logger.warning(f"地理编码请求超时/失败 (尝试 {attempt + 1}/{retry}): {city} - {e}")
            if attempt == retry - 1:
                logger.error(f"地理编码最终失败: {city}")
                return None
            await asyncio.sleep(2)

    if data is None:
        return None

    results = data.get("results") or []
    if not results:
        return None

    if PREFER_CHINA_RESULT:
        cn = [x for x in results if x.get("country_code") == "CN"]
        if cn:
            results = cn

    city_plain = city.strip().replace("市", "")
    if city_plain == "南京":
        best = None
        for x in results:
            if (x.get("name") == "南京") and ("江苏" in (x.get("admin1") or "")):
                best = x
                break
        if best is None:
            results.sort(key=lambda x: (x.get("population") or 0), reverse=True)
            best = results[0]
    else:
        results.sort(key=lambda x: (x.get("population") or 0), reverse=True)
        best = results[0]

    lat = float(best["latitude"])
    lon = float(best["longitude"])
    name = best.get("name", city)
    admin1 = best.get("admin1")
    country = best.get("country")

    display = name
    if admin1:
        display += f"({admin1})"
    elif country:
        display += f"({country})"

    return lat, lon, display


async def fetch_daily_yesterday_today_tomorrow(lat: float, lon: float, retry: int = 3) -> dict:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": "auto",
        "wind_speed_unit": "ms",
        "precipitation_unit": "mm",
        "past_days": 1,
        "forecast_days": 2,
        "daily": ",".join([
            "weather_code",
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_sum",
            "precipitation_probability_max",
            "wind_speed_10m_max",
        ]),
    }

    for attempt in range(retry):
        try:
            async with _make_client(timeout=30.0) as client:
                r = await client.get(url, params=params)
                return r.json()
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
            logger.warning(f"天气数据请求超时/失败 (尝试 {attempt + 1}/{retry}): {e}")
            if attempt == retry - 1:
                raise
            await asyncio.sleep(2)


async def fetch_hourly_last1_now_next3(lat: float, lon: float, retry: int = 3) -> dict:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": "auto",
        "wind_speed_unit": "ms",
        "precipitation_unit": "mm",
        "past_hours": 1,
        "forecast_hours": 4,
        "current": ",".join([
            "temperature_2m",
            "apparent_temperature",
            "relative_humidity_2m",
            "wind_speed_10m",
            "precipitation",
            "weather_code",
        ]),
        "hourly": ",".join([
            "temperature_2m",
            "apparent_temperature",
            "relative_humidity_2m",
            "wind_speed_10m",
            "precipitation",
            "precipitation_probability",
            "weather_code",
        ]),
    }

    for attempt in range(retry):
        try:
            async with _make_client(timeout=30.0) as client:
                r = await client.get(url, params=params)
                return r.json()
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
            logger.warning(f"小时数据请求超时/失败 (尝试 {attempt + 1}/{retry}): {e}")
            if attempt == retry - 1:
                raise
            await asyncio.sleep(2)


# =========================================================
# ▼▼▼ 生成早报文本（定时推送 & 手动测试共用）▼▼▼
# =========================================================
async def build_morning_text(city: str) -> Optional[str]:
    geo = await geocode_city(city)
    if not geo:
        return None

    lat, lon, display_city = geo
    data = await fetch_daily_yesterday_today_tomorrow(lat, lon)
    daily = data.get("daily") or {}

    dates = daily.get("time") or []
    tmax = daily.get("temperature_2m_max") or []
    tmin = daily.get("temperature_2m_min") or []
    p_sum = daily.get("precipitation_sum") or []
    p_prob = daily.get("precipitation_probability_max") or []
    wind = daily.get("wind_speed_10m_max") or []
    code = daily.get("weather_code") or []

    if len(dates) < 2:
        return None

    suggestion = make_suggestion_today(
        tmin=tmin[1] if len(tmin) > 1 else None,
        tmax=tmax[1] if len(tmax) > 1 else None,
        precip_sum=p_sum[1] if len(p_sum) > 1 else None,
        wind_max=wind[1] if len(wind) > 1 else None,
        weather_code=code[1] if len(code) > 1 else None,
        precip_prob_max=p_prob[1] if len(p_prob) > 1 else None,
    )

    text = (
        f"☀️ 早安!{display_city} 今日天气\n"
        f"{dates[1]}:{code_to_text(code[1])}, {tmin[1]}~{tmax[1]}°C\n"
        f"降水 {p_sum[1]}mm(概率 {p_prob[1]}%), 阵风 {wind[1]}m/s\n"
        f"💡 建议:{suggestion}"
    )
    return text


# =========================================================
# ▼▼▼ 输入"天气"时给三四行指引 ▼▼▼
# =========================================================
weather_guide = on_regex(r"^天气$", priority=5, block=True)

@weather_guide.handle()
async def _(event: GroupMessageEvent):
    guide = (
        "📌 天气查询用法:\n"
        "1) 天气 南京  → 昨天/今天/明天简报 + 今日建议\n"
        "2) 天气 南京 详细  → 过去1小时/当前/未来3小时详情\n"
        "3) 天气 测试  → 测试本群早报是否正常\n"
        "4) 示例: 天气 上海 / 天气 北京 详细"
    )
    await weather_guide.send(guide)
    return


# =========================================================
# ▼▼▼ 手动测试早报：天气 测试（在当前群发一条早报）▼▼▼
# =========================================================
weather_test = on_regex(r"^天气\s+测试$", priority=4, block=True)

@weather_test.handle()
async def _(event: GroupMessageEvent):
    group_id = event.group_id
    city = WEATHER_PUSH_TARGETS.get(group_id)

    if not city:
        await weather_test.send("本群未配置早报推送城市，请先在 WEATHER_PUSH_TARGETS 里添加：群号 -> 城市")
        return

    try:
        text = await build_morning_text(city)
        if not text:
            await weather_test.send("早报生成失败：天气数据不足或城市解析失败")
            return
        await weather_test.send(text)
        return
    except Exception as e:
        logger.exception(e)
        await weather_test.send("早报测试失败：天气接口请求异常，请稍后再试。")
        return


# =========================================================
# ▼▼▼ 消息指令:天气 城市 / 天气 城市 详细(严格整条消息)▼▼▼
# 关键：排除 “天气 测试” 被当作城市
# =========================================================
weather_matcher = on_regex(
    r"^天气\s+(?!测试$)(?P<city>.+?)(?:\s+(?P<detail>详细))?$",
    priority=5,
    block=True
)

@weather_matcher.handle()
async def _(event: GroupMessageEvent, m: re.Match[str] = RegexMatched()):
    city = m.group("city").strip()
    detailed = m.group("detail") is not None

    geo = await geocode_city(city)
    if not geo:
        await weather_matcher.send(f"未找到城市:{city}")
        return

    lat, lon, display_city = geo

    try:
        if not detailed:
            data = await fetch_daily_yesterday_today_tomorrow(lat, lon)
            daily = data.get("daily") or {}

            dates = daily.get("time") or []
            tmax = daily.get("temperature_2m_max") or []
            tmin = daily.get("temperature_2m_min") or []
            p_sum = daily.get("precipitation_sum") or []
            p_prob = daily.get("precipitation_probability_max") or []
            wind = daily.get("wind_speed_10m_max") or []
            code = daily.get("weather_code") or []

            if len(dates) < 3:
                await weather_matcher.send("天气数据不足,请稍后再试")
                return

            labels = ["昨天", "今天", "明天"]
            lines = [f"📍 {display_city}(昨日/今日/明日)"]
            for i in range(3):
                lines.append(
                    f"{labels[i]} {dates[i]}:{code_to_text(code[i])}, "
                    f"{tmin[i]}~{tmax[i]}°C, 降水 {p_sum[i]}mm, "
                    f"降水概率 {p_prob[i]}%, 阵风 {wind[i]}m/s"
                )

            suggestion = make_suggestion_today(
                tmin=tmin[1] if len(tmin) > 1 else None,
                tmax=tmax[1] if len(tmax) > 1 else None,
                precip_sum=p_sum[1] if len(p_sum) > 1 else None,
                wind_max=wind[1] if len(wind) > 1 else None,
                weather_code=code[1] if len(code) > 1 else None,
                precip_prob_max=p_prob[1] if len(p_prob) > 1 else None,
            )
            lines.append("———")
            lines.append(f"💡 今日建议:{suggestion}")

            await weather_matcher.send("\n".join(lines))
            return

        else:
            data = await fetch_hourly_last1_now_next3(lat, lon)
            current = data.get("current") or {}
            hourly = data.get("hourly") or {}

            now_temp = current.get("temperature_2m")
            now_feel = current.get("apparent_temperature")
            now_humi = current.get("relative_humidity_2m")
            now_wind = current.get("wind_speed_10m")
            now_prec = current.get("precipitation")
            now_code = current.get("weather_code")

            times = hourly.get("time") or []
            temps = hourly.get("temperature_2m") or []
            feels = hourly.get("apparent_temperature") or []
            humis = hourly.get("relative_humidity_2m") or []
            winds = hourly.get("wind_speed_10m") or []
            precs = hourly.get("precipitation") or []
            probs = hourly.get("precipitation_probability") or []
            codes = hourly.get("weather_code") or []

            lines = [f"📍 {display_city}(详细:过去1小时/当前/未来3小时)"]
            lines.append(
                f"当前:{code_to_text(now_code)}, {now_temp}°C(体感 {now_feel}°C), "
                f"湿度 {now_humi}%, 风 {now_wind}m/s, 降水 {now_prec}mm"
            )
            lines.append("———")
            lines.append("小时趋势:")

            for idx, (t, tp, fl, hu, wi, pr, pb, cd) in enumerate(
                zip(times, temps, feels, humis, winds, precs, probs, codes)
            ):
                hhmm = t.split("T")[-1]
                if idx == 0:
                    tag = "1小时前"
                elif idx == 1:
                    tag = "当前小时"
                else:
                    tag = f"+{idx-1}小时"
                lines.append(
                    f"{tag} {hhmm}:{code_to_text(cd)}, {tp}°C(体感{fl}°C), "
                    f"湿度{hu}%, 风{wi}m/s, 降水{pr}mm, 降水概率{pb}%"
                )

            max_prob = max(probs) if probs else None
            max_wind = max(winds) if winds else None
            suggestion = make_suggestion_today(
                tmin=None,
                tmax=now_temp,
                precip_sum=None,
                wind_max=max_wind,
                weather_code=now_code,
                precip_prob_max=max_prob,
            )
            lines.append("———")
            lines.append(f"💡 出行建议:{suggestion}")

            await weather_matcher.send("\n".join(lines))
            return

    except Exception as e:
        logger.exception(e)
        await weather_matcher.send("天气接口请求失败,请稍后再试。")
        return


# =========================================================
# ▼▼▼ 每天 08:30 群推送:天气早报(按 WEATHER_PUSH_TARGETS 配置)
# =========================================================
@scheduler.scheduled_job("cron", hour=8, minute=30, id="weather_morning_push_0830")
async def _push_weather_0830():
    if not WEATHER_PUSH_TARGETS:
        return

    all_bots = get_bots()
    if not all_bots:
        logger.warning("早报推送:未找到可用 Bot")
        return

    bot: Bot = next(iter(all_bots.values()))

    for group_id, city in WEATHER_PUSH_TARGETS.items():
        try:
            text = await build_morning_text(city)
            if not text:
                continue
            await bot.call_api("send_group_msg", group_id=group_id, message=text)

        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError):
            continue
        except Exception:
            logger.exception(f"早报推送异常 (群 {group_id}, 城市 {city})")
            continue
