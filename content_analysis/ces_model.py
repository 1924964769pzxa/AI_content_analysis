# -*- coding: utf-8 -*-
"""
xhs_ces_scorer.py
小红书笔记评分与筛选（异步、无持久化；仅返回内存结果）
—— 已按你的要求移除 MongoDB 保存操作 ——

使用要点：
1) CES 评分：like*1 + collect*1 + comment*4 + share*4 + follow*8
2) 可选时间衰减（指数半衰期，默认 48h，可按需调整）
3) 并发友好：纯计算；提供“分批让出事件循环”的机制，避免长时间占用主线程

作者: 你
日期: 2025-09-07
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


# =========================
# 配置数据类
# =========================
@dataclass
class CESFilterConfig:
    # —— 基础阈值（可二选一或同时生效）——
    min_ces: float = 0.0                    # 基础 CES 最小值
    min_weighted_ces: float = 0.0           # 时间衰减后的 CES 最小值

    # —— 排序截断（与阈值联动）——
    top_k: Optional[int] = None             # 仅保留加权 CES 前 K
    top_percent: Optional[float] = None     # 或者保留前百分比（0~1 之间）

    # —— 时间相关 ——
    enable_time_decay: bool = True          # 启用时间衰减
    half_life_hours: float = 48.0           # 半衰期（小时），默认 48h
    recency_days: Optional[int] = None      # 仅保留最近 N 天内的内容

    # —— 基本过滤 ——
    allowed_types: Optional[Sequence[str]] = None  # 允许的 type 集合，如 ["video", "normal"]
    required_keywords: Optional[Sequence[str]] = None   # 标题/描述/标签 必须包含其一
    exclude_keywords: Optional[Sequence[str]] = None    # 标题/描述/标签 命中则剔除

    # —— 计算控制（高并发友好）——
    yield_every: int = 1000  # 每处理 N 条让出一次事件循环，避免长时间占用主线程


# =========================
# 工具函数
# =========================
def _parse_count(x: Any) -> int:
    """
    解析小红书常见的计数字段：
    - "5.6万" -> 56000
    - "1.1亿" -> 110000000
    - "2,380" -> 2380
    - 纯整数字符串或 int -> 按整数解析
    """
    if x is None:
        return 0
    if isinstance(x, (int, float)):
        return int(x)
    s = str(x).strip().replace(",", "")
    if not s:
        return 0

    mult = 1
    if s.endswith("万"):
        mult = 10_000
        s = s[:-1]
    elif s.endswith("亿"):
        mult = 100_000_000
        s = s[:-1]

    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def _epoch_ms_to_hours_ago(ms_val: Any, now_ms: Optional[int] = None) -> Optional[float]:
    """将毫秒级时间戳转换为距今多少小时（float）。若无效返回 None。"""
    try:
        ms = int(ms_val)
        if ms <= 0:
            return None
        now = int(now_ms or time.time() * 1000)
        delta_ms = max(0, now - ms)
        return delta_ms / 1000.0 / 3600.0
    except Exception:
        return None


def _time_decay_weight(hours_ago: Optional[float], half_life_hours: float) -> float:
    """
    时间衰减权重（指数衰减）：w = 0.5^(t / half_life)
    - t: 小时数
    - half_life: 半衰期（小时）
    """
    if hours_ago is None:
        return 1.0
    if half_life_hours <= 0:
        return 1.0
    return math.pow(0.5, hours_ago / half_life_hours)


def _text_contains_any(text: str, keywords: Sequence[str]) -> bool:
    t = (text or "").lower()
    return any(k.lower() in t for k in keywords)


# =========================
# 核心评分逻辑
# =========================
def compute_ces(note: Dict[str, Any]) -> Dict[str, Any]:
    """
    计算单条笔记的 CES 分数（不考虑时间衰减），并返回补充后的结构：
    {
      ...原始字段,
      "signals": { "like": int, "collect": int, "comment": int, "share": int, "follow": int },
      "ces": float
    }
    """
    like_ = _parse_count(note.get("liked_count"))
    collect_ = _parse_count(note.get("collected_count"))
    comment_ = _parse_count(note.get("comment_count"))
    share_ = _parse_count(note.get("share_count"))
    follow_ = _parse_count(note.get("follow_count", 0))  # 数据无该字段时自动按 0

    ces = like_ * 1 + collect_ * 1 + comment_ * 4 + share_ * 4 + follow_ * 8

    enriched = dict(note)
    enriched["signals"] = {
        "like": like_,
        "collect": collect_,
        "comment": comment_,
        "share": share_,
        "follow": follow_,
    }
    enriched["ces"] = float(ces)
    return enriched


def apply_time_weight(enriched_note: Dict[str, Any], *, half_life_hours: float) -> Dict[str, Any]:
    """
    为已计算 CES 的笔记叠加时间权重，输出 weighted_ces。
    - 优先用 last_update_time，其次用 time（均为 epoch-ms 字符串/数值）
    """
    hours_ago = None
    for k in ("last_update_time", "time", "last_modify_ts"):
        if hours_ago is None and enriched_note.get(k) is not None:
            hours_ago = _epoch_ms_to_hours_ago(enriched_note.get(k))

    weight = _time_decay_weight(hours_ago, half_life_hours)
    enriched_note["time_weight"] = weight
    enriched_note["weighted_ces"] = enriched_note.get("ces", 0.0) * weight
    return enriched_note


def _passes_basic_filters(note: Dict[str, Any], cfg: CESFilterConfig) -> bool:
    """类型 / 关键词 / 最近 N 天 基础过滤（轻量、纯计算，不阻塞事件循环）。"""
    # type 过滤
    if cfg.allowed_types:
        ntype = (note.get("type") or "").lower()
        if ntype not in set(x.lower() for x in cfg.allowed_types):
            return False

    # 关键词过滤
    combine_text = " ".join(
        [
            str(note.get("title", "")),
            str(note.get("desc", "")),
            str(note.get("tag_list", "")),
        ]
    )
    if cfg.required_keywords:
        if not _text_contains_any(combine_text, cfg.required_keywords):
            return False
    if cfg.exclude_keywords:
        if _text_contains_any(combine_text, cfg.exclude_keywords):
            return False

    # 最近 N 天
    if cfg.recency_days is not None and cfg.recency_days > 0:
        hours_ago = None
        for k in ("last_update_time", "time", "last_modify_ts"):
            if hours_ago is None and note.get(k) is not None:
                hours_ago = _epoch_ms_to_hours_ago(note.get(k))
        if hours_ago is None:
            return False
        if hours_ago > cfg.recency_days * 24:
            return False

    return True


# =========================
# 面向高并发的异步入口
# =========================
async def score_and_filter_notes(
    notes: List[Dict[str, Any]],
    cfg: CESFilterConfig,
) -> List[Dict[str, Any]]:
    """
    异步评分 + 过滤主流程（无持久化）：
    - 纯计算逻辑在事件循环中完成
    - 如启用时间衰减，则叠加 weighted_ces 字段
    - 支持阈值 + Top 截断
    - 提供“分批让出事件循环”的机制，避免批量计算时长时间占用主线程
    """
    if not notes:
        return []

    # 1) 基础过滤
    filtered = []
    for idx, n in enumerate(notes):
        if _passes_basic_filters(n, cfg):
            filtered.append(n)
        if cfg.yield_every and (idx + 1) % cfg.yield_every == 0:
            # —— 高并发友好：定期让出事件循环，避免阻塞 ——
            await asyncio.sleep(0)

    if not filtered:
        return []

    # 2) 计算 CES + 时间衰减
    enriched: List[Dict[str, Any]] = []
    for idx, n in enumerate(filtered):
        e = compute_ces(n)
        if cfg.enable_time_decay:
            e = apply_time_weight(e, half_life_hours=cfg.half_life_hours)
        else:
            e["time_weight"] = 1.0
            e["weighted_ces"] = e["ces"]
        enriched.append(e)

        if cfg.yield_every and (idx + 1) % cfg.yield_every == 0:
            await asyncio.sleep(0)

    # 3) 阈值过滤
    enriched = [
        e
        for e in enriched
        if (e["ces"] >= cfg.min_ces) and (e["weighted_ces"] >= cfg.min_weighted_ces)
    ]
    if not enriched:
        return []

    # 4) 排序（按加权 CES 从高到低）
    enriched.sort(key=lambda x: x.get("weighted_ces", 0.0), reverse=True)

    # 5) Top 截断（可选）
    if cfg.top_percent is not None and 0 < cfg.top_percent < 1:
        cut = max(1, int(len(enriched) * cfg.top_percent))
        enriched = enriched[:cut]
    if cfg.top_k is not None and cfg.top_k > 0:
        enriched = enriched[: cfg.top_k]

    return enriched


# =========================
# 示例：异步调用（仅打印结果）
# =========================
async def demo():
    """
    演示：对传入的 notes 列表评分并筛选，打印最终结果。
    """
    # —— 用你的示例数据（可替换为真实列表）——
    notes = [
  {
    "note_id": "675ab71f000000000103d9fb",
    "type": "normal",
    "title": "聚光投流做好这个，留资率翻倍‼️",
    "desc": "我们跟上千家投聚光的品牌方聊过，涉及教育，旅游，生美，家装...等多个行业，[失望R]发现他们花了很多聚光广告💰，但获客情况却不理想，也许是忽略了私信承接这一步[暗中观察R]\n\t\n❓为什么获客难？\n💔多个账号，1个员工切换回复效率低，流量大后无法承接；\n💔人工客服无法做到24小时全天候在线接待客户；\n💔潜在沉默客户二次触达难，流失客资多。\n\t\n🔥来鼓—小红书官方授权，专为小红书品牌商家提供的一站式私信获客工具。\n✅多账号私信,评论一站聚合\n✅AI员工24小时秒回，快速提高回复率\n✅AI赋能打造营销专家 “人性化”沟通\n✅ 智能追粉，一键唤醒上万潜在客户\n\t\n👉🏻如果你也有聚光获客难，私信管理方面的难题，不如跟我们聊聊[doge]\n评论区留言「试用」，开启效率获客之路 ⚠️仅限投聚光的专业号‼️\n\t\n#来鼓[话题]# #聚光[话题]# #聚光投流[话题]# #专业号[话题]# #获客引流[话题]# #引流获客[话题]# #ai工具[话题]# #智能客服[话题]# #私信通[话题]# #客资[话题]#",
    "video_url": "",
    "time": "1733998367000",
    "last_update_time": "1733998367000",
    "user_id": "665ea88c0000000003031383",
    "nickname": "来鼓AI",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31477u8uq1m005piul260u4s362thv78",
    "liked_count": "3221",
    "collected_count": "1469",
    "comment_count": "1142",
    "share_count": "331",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091346/6df8c494f68319b9b473d05ec9744853/1040g2sg31ba934ceng705piul260u4s325qk7jo!nd_dft_wlteh_webp_3,http://sns-webpic-qc.xhscdn.com/202509091346/86fdc9ce8364d166f565b7fdb8c74ba0/1040g2sg31ba934ceng7g5piul260u4s3mj09uto!nd_dft_wlteh_webp_3,http://sns-webpic-qc.xhscdn.com/202509091346/d71efeede8ee8cd8a6524d777899a0f3/1040g2sg31ba934ceng805piul260u4s3tqruqro!nd_dft_wlteh_webp_3,http://sns-webpic-qc.xhscdn.com/202509091346/e27c2f526ee3a179d3155bc7f41b7348/1040g2sg31ba934ceng8g5piul260u4s37bl52qo!nd_dft_wlteh_webp_3,http://sns-webpic-qc.xhscdn.com/202509091346/eb3565e279e07ff90151d4381ed08104/1040g2sg31ba934ceng905piul260u4s3g1o75v0!nd_dft_wlteh_webp_3",
    "tag_list": "来鼓,聚光,聚光投流,专业号,获客引流,引流获客,ai工具,智能客服,私信通,客资",
    "last_modify_ts": "1757396823752",
    "note_url": "https://www.xiaohongshu.com/explore/675ab71f000000000103d9fb?xsec_token=ABDgEYlRyPvD8EqTAj16gb-CYu3RnBLQ5OMoKRMKXXyfA=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "687f4a0f0000000023005f8e",
    "type": "video",
    "title": "deepseek接入红薯，现在都能这么获客了吗..",
    "desc": "那我以前辛辛苦苦做一天客服算什么？算我厉害吗 #聚光[话题]# #企业号[话题]# #私信通[话题]# #KOS[话题]##小红书运营[话题]# #AI工具[话题]# #运营干货[话题]# #自媒体干货[话题]##来鼓AI[话题]#",
    "video_url": "http://sns-video-bd.xhscdn.com/1040g2so31k90m2joio0g5q0b8ll5vob9hs2pha8",
    "time": "1753268447000",
    "last_update_time": "1753241262000",
    "user_id": "680b456a000000001703e169",
    "nickname": "北岭聊AI",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31gnvfhjs3sc05q0b8ll5vob9spm65p8",
    "liked_count": "3139",
    "collected_count": "1202",
    "comment_count": "7",
    "share_count": "103",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091346/4cb612b586ebae12c046594a95b723ea/1040g2sg31k7vi55t2uk05q0b8ll5vob976vfpno!nd_dft_wlteh_webp_3",
    "tag_list": "聚光,企业号,私信通,KOS,小红书运营,AI工具,运营干货,自媒体干货,来鼓AI",
    "last_modify_ts": "1757396823755",
    "note_url": "https://www.xiaohongshu.com/explore/687f4a0f0000000023005f8e?xsec_token=ABR2ig02oCGy_8mpBGj19YRGD9R9EfaQcAajXiUHspmk0=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "682b003f00000000030398fe",
    "type": "video",
    "title": "1分钟搞懂小红书官方🔥AI获客员工！好🐮",
    "desc": "🎉来鼓AI-小红书官方授权的AI私信获客工具🧰\n让它来接管你的小红书账号，[自拍R]躺赚不是梦~\n\t\n✨来鼓AI2.0已全新升级\n\t\n1️⃣多账号（企业号和KOS账号）私信意向评论聚合到一个工作台\n\t\n2️⃣告别传统AI客服的SOP形式，接入deepseek，GPT等大模型，覆盖全场景，真正的AI聊客户\n\t\n3️⃣AI团队接管你的小红书，AI主管汇报数据，自动“调教”优化AI客服，越用越聪明[黄金薯R]\n\t\n4️⃣AI瞅准时机，🌟自动发送名片卡、留资卡‼️还能安抚客户情绪\n\t\n5️⃣7*24小时全年无休的AI员工，秒回客户，快速提升小红书咨询分📊，提高账号权重\n\t\n6️⃣多场景自动化——未留资客户追回，自动回复意向评论，你想要的都在这👏🏻👏🏻\n\t\n[自拍R]立刻接入你的企业号/KOS号，体验真AI带来的留资效果吧~\n⚠️‼️‼️现在私戳@来鼓AI 填写留资卡即可领取免费试用哟✅填写留资卡7天内领取\n感谢@赛博鸭AIGC 宝子的分享 💗\n#小红书运营[话题]#       #AI工具[话题]#          #来鼓AI[话题]#        #聚光[话题]#       #私信通[话题]#       #来鼓[话题]#       #AI[话题]#       #效率神器[话题]#       #数字化[话题]#       #数字化企业[话题]#       #小红书企业号运营[话题]#       #AI工具[话题]#       #聚光[话题]#       #AI人工智能[话题]#     #kos[话题]#     #引流获客[话题]#   #广告投放[话题]# #来鼓[话题]# #私信通[话题]# #聚光[话题]# #AI[话题]# #引流获客[话题]# #小红书企业号运营[话题]# #AI[话题]# #AI工具[话题]# #AI人工智能[话题]#",
    "video_url": "http://sns-video-bd.xhscdn.com/pre_post/1040g2t031hlldslj3ifg5piul260u4s3sn08sdg",
    "time": "1747648575000",
    "last_update_time": "1750909398000",
    "user_id": "665ea88c0000000003031383",
    "nickname": "来鼓AI",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31477u8uq1m005piul260u4s362thv78",
    "liked_count": "2647",
    "collected_count": "2170",
    "comment_count": "114",
    "share_count": "690",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091346/7a77502370d37afb4fd4bd4f96c27e97/1040g2sg31hlm0oqqjq0g5piul260u4s3ggmrkfg!nd_dft_wlteh_jpg_3",
    "tag_list": "小红书运营,AI工具,来鼓AI,聚光,私信通,来鼓,AI,效率神器,数字化,数字化企业,小红书企业号运营,AI人工智能,kos,引流获客,广告投放",
    "last_modify_ts": "1757396823757",
    "note_url": "https://www.xiaohongshu.com/explore/682b003f00000000030398fe?xsec_token=ABAiGrmjYHQo7Kq19Ex8BYZ3ZEnDxAV0euI12NWCfFofI=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "6800ddbe000000000b015cf6",
    "type": "video",
    "title": "🔥AI+小红书私信获客工具‼️王炸新功能升级",
    "desc": "小红书私信获客工具🧰又有大‼️变‼️化‼️啦，只能通过✅名片卡✅留资卡的方式在私信里获取客资,且需要近3️⃣0️⃣天聚光消耗大于0🔥\n\t\n‼️来鼓【AI员工】支持自动发送名片卡/留资卡[赞R]\n\t\n来鼓AI—小红书🍠官方授权的私信获客工具‼️‼️真正实现1个AI员工，全自动获客\n\t\n🔥来鼓AI员工亮点功能：\n✅AI自动发送名片卡/留资卡\n✅7*24小时全天候自动回复\n✅纯AI接待💁，账号接入即AI上岗\n✅快速提升留资率\n\t\n🔥现在左下角【立即咨询】填写留资卡，即可获取免费试用7天\n[赞R]聚光后台即可授权接入，想🆓的品牌评论区暗号【试用】！！\n#来鼓[话题]#     #AI[话题]#     #小红书运营干货[话题]#     #提升用户体验[话题]#     #聚光[话题]#     #聚光接入来鼓ai[话题]#     #引流获客[话题]#     #私信预约[话题]#     #私信通[话题]#",
    "video_url": "http://sns-video-bd.xhscdn.com/pre_post/1040g2t031ii0c5rag8e05piul260u4s3iae77io",
    "time": "1744887230000",
    "last_update_time": "1749549353000",
    "user_id": "665ea88c0000000003031383",
    "nickname": "来鼓AI",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31477u8uq1m005piul260u4s362thv78",
    "liked_count": "1894",
    "collected_count": "897",
    "comment_count": "506",
    "share_count": "268",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091346/6179f3fb6dcf400e5475dfaa7980c052/1040g2sg31gcha082jo705piul260u4s3klmtfv8!nd_dft_wlteh_jpg_3",
    "tag_list": "来鼓,AI,小红书运营干货,提升用户体验,聚光,聚光接入来鼓ai,引流获客,私信预约,私信通",
    "last_modify_ts": "1757396823759",
    "note_url": "https://www.xiaohongshu.com/explore/6800ddbe000000000b015cf6?xsec_token=ABOTWDnLv5O_ZJKWxnYlDBWt2MBatvEIeaiX_N-obXOO4=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "68a1e391000000001d00062f",
    "type": "video",
    "title": "零基础搭建小红书AI客服，轻松实现爆单获客",
    "desc": "﻿#来鼓AI[话题]#﻿ ﻿#AI工具[话题]#﻿ ﻿#AI客服[话题]#﻿ ﻿#小红书工具[话题]#﻿ ﻿#小红书运营[话题]#﻿ ﻿#小红书商家[话题]#﻿ ﻿#品牌运营[话题]#﻿ ﻿#AI助手[话题]#﻿ ﻿#智能客服[话题]#﻿ ﻿#AI营销[话题]#﻿",
    "video_url": "http://sns-video-bd.xhscdn.com/spectrum/1040g0jg31l9ohcvhl0005nps1c908bb4h5m7g5g",
    "time": "1755486059000",
    "last_update_time": "1755440018000",
    "user_id": "5f3c0b120000000001002d64",
    "nickname": "树懒TV",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31450hnne1o005nps1c908bb4ps5euvo",
    "liked_count": "1763",
    "collected_count": "1201",
    "comment_count": "4",
    "share_count": "71",
    "sticky": "False",
    "ip_location": "日本",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091346/7e5be3817500b34c623e6d9ac11ee286/spectrum/1040g0k031l9onmib4s005nps1c908bb4f4htiug!nd_dft_wlteh_jpg_3",
    "tag_list": "来鼓AI,AI工具,AI客服,小红书工具,小红书运营,小红书商家,品牌运营,AI助手,智能客服,AI营销",
    "last_modify_ts": "1757396823761",
    "note_url": "https://www.xiaohongshu.com/explore/68a1e391000000001d00062f?xsec_token=ABuHL-8vc1UI6admbRSVyi5YH6P3L3Ec2UE4_Hk9Y40mw=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "67469c3d0000000002018390",
    "type": "video",
    "title": "太好啦！割韭菜653万的团队终于被抓了...",
    "desc": "割韭菜653万的团队终于被抓了！这个AI获客黑科技果真开源免费？ #找客户[话题]# #割韭菜[话题]# #小红书[话题]# #数据抓取[话题]# #开源[话题]#",
    "video_url": "http://sns-video-bd.xhscdn.com/spectrum/1040g35831amkph2lng0g5p1l3fbl22kqv3j789o",
    "time": "1733047284000",
    "last_update_time": "1732680765000",
    "user_id": "64351bd70000000014010a9a",
    "nickname": "大黄AI黑科技",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31iguvp3a0e005p1l3fbl22kqilqk4t0",
    "liked_count": "1693",
    "collected_count": "2187",
    "comment_count": "228",
    "share_count": "738",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091346/965a433edbb5867eba8a51dafb86f6c6/spectrum/1040g34o31amkq47qna0g5p1l3fbl22kq2baf9o8!nd_dft_wlteh_jpg_3",
    "tag_list": "找客户,割韭菜,小红书,数据抓取,开源",
    "last_modify_ts": "1757396823762",
    "note_url": "https://www.xiaohongshu.com/explore/67469c3d0000000002018390?xsec_token=ABz0Zj5j4NDGQjGK8maTfS4rFXNEQAULd5gpKNyNI8-OE=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "67a7ef48000000001800e92e",
    "type": "video",
    "title": "ai开店短视频，一秒出爆款",
    "desc": "实体老板，个体创业者们掌握Deepseek真的很重要，做短视频基本都会用到#Deepseek[话题]##AI短视频[话题]##门店获客[话题]##AI素材混剪[话题]##老板思维[话题]##",
    "video_url": "http://sns-video-bd.xhscdn.com/1040g00g31dlm2if3gue05o8vqs8g8dabigrbei0",
    "time": "1739059016000",
    "last_update_time": "1739059017000",
    "user_id": "611fd711000000000100354b",
    "nickname": "仙人掌🌵",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31d0i4rqlgk005o8vqs8g8dabmugerpo",
    "liked_count": "919",
    "collected_count": "781",
    "comment_count": "18",
    "share_count": "245",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091346/ad59800b964a37e5329a334193feefa3/1040g2sg31dlm2iakgm705o8vqs8g8dabeg558qg!nd_dft_wlteh_jpg_3",
    "tag_list": "Deepseek,AI短视频,门店获客,AI素材混剪,老板思维",
    "last_modify_ts": "1757396823764",
    "note_url": "https://www.xiaohongshu.com/explore/67a7ef48000000001800e92e?xsec_token=ABkO6l600TGQtJnoPNAX61yiHD5oYCtzMb8bB2brEvhVI=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "67be898e000000000e0075dc",
    "type": "normal",
    "title": "企业号、kos必看‼️解放双手，留资率蹭蹭上涨",
    "desc": "⚠️小红书官方认证IM私信获客工具+全年免费版=bai捡的业绩增长\n\t\n✅多账号一个平台平台管理，1人轻松完成3人工作量\n✅AI自动追粉+二次触达，开口率飙升80%\n✅24h智能客服在线，深夜咨询自动锁客\n\t\n🎁企业号和kos专享：(2025年9月31日前私信填写留资卡领免费试用🔥)‼️（需开通聚光企业号或kos账号）\n#来鼓[话题]#   #聚光[话题]#   #私信通[话题]#   #AI[话题]#   #数字化企业[话题]#   #小红书企业号运营[话题]#   #广告投放[话题]#    #新媒体运营工具[话题]# #全渠道营销[话题]#    #新媒体运营工具[话题]#",
    "video_url": "",
    "time": "1740540302000",
    "last_update_time": "1756706572000",
    "user_id": "665ea88c0000000003031383",
    "nickname": "来鼓AI",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31477u8uq1m005piul260u4s362thv78",
    "liked_count": "734",
    "collected_count": "635",
    "comment_count": "237",
    "share_count": "249",
    "sticky": "False",
    "ip_location": "四川",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091346/5ff4679712bfc6a871daf30d79629bc6/1040g2sg31eboh4v5gk4g5piul260u4s34eebju8!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091346/bfd05d8b4f35d55f1368936a368008a3/1040g2sg31eboh4v5gk505piul260u4s33uvtnvo!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091346/bd3d67587ec6c5118e346356e9b5aaee/1040g2sg31eboh4v5gk5g5piul260u4s30ldvga0!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091346/0d8b83a399bf2f25607f61e64464d9b4/1040g2sg31eboh4v5gk605piul260u4s3a6r5k30!nd_dft_wlteh_jpg_3",
    "tag_list": "来鼓,聚光,私信通,AI,数字化企业,小红书企业号运营,广告投放,新媒体运营工具,全渠道营销",
    "last_modify_ts": "1757396823765",
    "note_url": "https://www.xiaohongshu.com/explore/67be898e000000000e0075dc?xsec_token=ABPsO-fCo6eTsr_rJlFZgNUgXlah4bdTZh57676kzpdD4=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "66b477e2000000000503b8b5",
    "type": "normal",
    "title": "线上获客知道这些，客户源源不断！",
    "desc": "无论你是传统实体商家，[害羞R]还是做线上生意的企业，想要通过新媒体平台拓客\n你应该都能发现平台的趋势是越来越饱和，不断竞价内卷的情况下，获客成本是越来越高的\n\t\n🤔每一个咨询的客户都有可能转化为宝贵的销售业绩，但很多企业往往忽略了这一步‼️传统的人工客服如果未经过专业培训，它的营销能力专业度都是远远不够的，更别提一个人工不可能做到24小时在线，但偏偏凌晨的小红书，🎵的客户也比较多，那如何破局呢？\n-\n如果你有以下这几个问题⬇️\n1️⃣光有数据没有转化，不知道如何引导客户，不会谈单，进来的流量无法成交【约等于】白干；\n2️⃣做矩阵号，无法聚合多新媒体平台多账号消息，效率低下\n3️⃣数据缺失，无法为市场下一步行动做出决策；\n...\n🌟不妨试试【来鼓AI】\n✅一站式聚合多渠道多账号消息，手机端，pc端随时连接客户\n✅24小时在线的AI营销员工，深夜回复，接入大模型训练，专业度营销能力可以实现自动获客，大大提高获线率\n✅数据整合，为市场提供数据支撑\n\t\n👇🏻点击【立即咨询】回复「试用」我给您免费试用，帮你解决业务核心问题❗️\n#干货分享[话题]# #小红书笔记[话题]# #品牌推广[话题]# #行业[话题]# #小程序[话题]# #来鼓[话题]# #AI[话题]# #智能客服机器人[话题]# #私域流量[话题]# #引流获客[话题]#",
    "video_url": "",
    "time": "1723103202000",
    "last_update_time": "1724138328000",
    "user_id": "665ea88c0000000003031383",
    "nickname": "来鼓AI",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31477u8uq1m005piul260u4s362thv78",
    "liked_count": "696",
    "collected_count": "490",
    "comment_count": "118",
    "share_count": "80",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091346/282a0118f3de265095d2ae5f1d8ae800/1040g008316mc44dl3c4g5piul260u4s363r9hag!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091346/aa1cc9c751e57d397f28ef07e8fb7709/1040g2sg3166p0mbmhaa05piul260u4s305bhnpo!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091346/8e1a5eddee60896244fcf1015a0c72ac/1040g2sg3166p0mbmhaag5piul260u4s3j6trkd0!nd_dft_wlteh_jpg_3",
    "tag_list": "干货分享,小红书笔记,品牌推广,行业,小程序,来鼓,AI,智能客服机器人,私域流量,引流获客",
    "last_modify_ts": "1757396823766",
    "note_url": "https://www.xiaohongshu.com/explore/66b477e2000000000503b8b5?xsec_token=ABUJUxcfTohMvmNFgb1P6zNNQaqJygl1O42W-DTUY9kuk=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "688a3c4c00000000230384ca",
    "type": "video",
    "title": "非常强大的外贸开发神器",
    "desc": "#工具分享[话题]# #AI工具[话题]# #外贸神器[话题]# #外贸AI工具[话题]# #信风AI[话题]# #外贸开发工具[话题]# #外贸开单[话题]# #效率神器[话题]# #外贸开发[话题]# #我的学习进化论[话题]#",
    "video_url": "http://sns-video-bd.xhscdn.com/pre_post/1040g0cg31kim09jj2u005psl5at3idbanmgg6ng",
    "time": "1753957103000",
    "last_update_time": "1753966705000",
    "user_id": "67952aba000000000e01356a",
    "nickname": "捏捏番茄",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31in4um8bha005psl5at3idbakvquphg",
    "liked_count": "678",
    "collected_count": "968",
    "comment_count": "56",
    "share_count": "203",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091346/1fcd4938195938c23717a8a2a22c01b0/1040g00831kim09jjiu005psl5at3idba3usgm40!nd_dft_wlteh_jpg_3",
    "tag_list": "工具分享,AI工具,外贸神器,外贸AI工具,信风AI,外贸开发工具,外贸开单,效率神器,外贸开发,我的学习进化论",
    "last_modify_ts": "1757396823768",
    "note_url": "https://www.xiaohongshu.com/explore/688a3c4c00000000230384ca?xsec_token=ABxLdy67ewE_u1MIFqu3BimmFHqNO36b6Syzd--bEcpOM=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "6801aeb6000000001b024d29",
    "type": "normal",
    "title": "突然发现了在小红书卖茶叶的思路好清晰",
    "desc": "有多少人在小红书做茶叶跟我说很迷茫，不知道该做什么，大家好，我是哆哆，喜欢研.究小红薯弓|流和AI。今天就来给大家拆解这个茶叶赛道在小红书的正确玩法。\n.\n茶叶赛道\n.\n1⃣我们要研究茶叶的客户画像\n喝茶的人太多了，但不是所有人都愿意花大价钱。我们要找的是那些追求品质生活、注重健康养生、或者需要高端礼品的人。他们可能是有一定经济基础的中青年，可能是注重生活仪式感的白领，也可能是需要送礼维护人脉关系的企业主。他们买茶不只是解渴，更是为了健康、品味、甚至是一种社交货币。\n.\n2⃣用户平时关心什么 （这些都可以是我们文案里的对应话术）\n🟢怕买到假货、劣质茶，喝坏身体；\n🟢怕不懂装懂，被忽悠花了冤枉钱；\n🟢面对琳琅满目的茶叶种类（绿茶、红茶、普洱、乌龙…）选择困难，不知道哪种适合自己；\n🟢担心农残、重金属超标，喝茶本来为养生，结果变伤身；\n🟢茶叶储存不好，几百上千的茶没多久就变味了，心疼；\n🟢想送礼但不知道送什么档次的、什么品种的茶叶显得有品位又不踩雷；\n🟢看到别人说某个茶多好多好，但自己喝不出个所以然，怀疑是不是智商税。\n.\n3⃣我们可以用什么视角来做内容\n① 资深茶友/评茶师：分享品茶心得、茶叶知识、冲泡技巧，显得你很专业，能帮大家避坑选好茶。比如，“小白入门，这3款口粮茶闭眼入，好喝不贵！”\n②  原产地茶农/主理人：直接展示茶园环境、制茶工艺，强调源头好货，建立信任感。\n.\n4⃣怎么找到这个赛道的有钱人群体\n① 健康价值：比如宣传有机、无农残、特定养生功效（降三高、助眠等），他们愿意为健康投入更多。\n② 社交礼品价值：针对送礼场景，强调包装精美、品牌知名度、文化寓意、稀缺性。\n③ 独特体验/稀缺性：比如古树茶、大师监制、特定产区、年份茶等，讲好故事，突出其稀有和收藏价值。\n.\n5⃣很多人的错误做法\n✔ 只会干巴巴介绍产品，说自己的茶多好，缺乏用户视角和痛点共鸣。\n✔ 忽视信任建立，上来就推高价茶，用户凭什么相信你？没有铺垫和价值塑造，转化难。\n✔ 对高付费人群的需求理解不到位，以为贵就是好，忽略了他们更深层次的（健康、社交、文化）需求。\n.\n6⃣AI怎么帮助到我们\n🔥 AI可以帮我们套用其他行业的爆款标题公式\n🔥 AI可以帮我们写各种框架的文案\n🔥 AI可以帮我们做细致的调研\n.\nOK，我是哆哆，专注小红薯弓｜流和AI\n#茶叶赛道[话题]# #运营[话题]# #小红书运营[话题]# #自媒体创业[话题]# #新媒体运营[话题]# #ai获客[话题]#",
    "video_url": "",
    "time": "1744940726000",
    "last_update_time": "1744940726000",
    "user_id": "65715cd3000000003d036883",
    "nickname": "小哆爱运营",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo30slci6mf467g5pbhbj9veq43q12kbeo",
    "liked_count": "667",
    "collected_count": "760",
    "comment_count": "53",
    "share_count": "198",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091346/727f720b565d4e506a6623ea82bc4b3e/1040g00831gdaq8ppjs605pbhbj9veq43shs8r6o!nd_dft_wlteh_jpg_3",
    "tag_list": "茶叶赛道,运营,小红书运营,自媒体创业,新媒体运营,ai获客",
    "last_modify_ts": "1757396823770",
    "note_url": "https://www.xiaohongshu.com/explore/6801aeb6000000001b024d29?xsec_token=ABIInJYDm3FSrS9c_GVDNLV9Hmee0341P1B-B--A8FbBc=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "68941d520000000025021086",
    "type": "normal",
    "title": "外贸客户开发效率的天花板👌被AI打破了！",
    "desc": "做外贸这么久，越来越清晰地意识到：自主开发客户，是外贸人最核心、但也最“伤神”的环节\n同时也深刻感受到：再不用AI，就真的被同行卷死了！\n.\n以前用人工开发客户，一步步像“挖矿”：\n✅用 Google🔍关键词（英文+当地语言），配合 Google Map 找公司；\n✅一个个点开官网，翻主营业务、扒邮箱 （大多是售后或销售邮箱，关键人很难找）\n✅领英搜公司名，找老板or采购，再根据人名+公司域名推邮箱；\n✅Facebook找公司主页，发消息碰碰运气\n✅准备开发信模板发发发、建开发表格、跟进、更新……\n整个流程耗时耗力，关键是—客户不是不回你，而是我们开发的，都是被人挑剩下的了！\n.\n💥现在是AI开发的时代了！\n同行借助AI工具就能快速识别、智能匹配客户，并及时发送更有针对性的开发信，而我们还在人工扒邮箱、人肉发信，只能永远慢半拍，越干越累！\n.\n✨ 直到近期用了 OKKI AiReach 第一次感受到：外贸开发，原来还能这么高效！\n从锁定目标客户 → 背调 → 写信，这些重复且复杂的工作都由AI数字人完成，它不仅是做完，而且是高效、智能地做得更好，精准挖掘客户、深度分析背调，甚至能根据客户官网内容和业务方向，自动生成个性化开发信。\n真正有回复、有明确意向的客户，才交给我继续跟进，我只负责最关键的环节：谈判 & 成交，也能腾出更多精力，服务好已有客户。\n.\n✅ 速度提上来了，客户池也被打通了！\n以前我们人工挖客户，每天能找到10来个精准潜客就不错了；现在AI数字人一次就能筛出几万个潜在客户，自动打标签、分优先级，还会通过上下游供应链图谱，挖出我从未接触过的客户类型，灵感直接拉满！\n✅ 更惊喜的是转化效率！\n我导入了之前未成交的客户名单，系统识别出某德国采购商近期有采购动作，AI自动发了一封“差异化开发信”，48小时内就收到了对方的回复，还预约了线上会议！这效率是我追半个月都未必能换来。\n.\n🧠 总结：AI是外贸人新一轮的竞争力分水岭！ 外贸人要聪明干，不是一直重复干😂\n把重复体力活交给OKKI AiReach，把精力留给谈判和成交，才是高效开发客户的正确打开方式😊\n﻿#外贸[话题]#﻿ ﻿#外贸找客户[话题]#﻿ ﻿#外贸获客[话题]#﻿ ﻿#外贸客户开发[话题]#﻿ ﻿#外贸开发客户[话题]#﻿ ﻿#外贸AI[话题]#﻿ ﻿#外贸AI工具[话题]#﻿ ﻿#OKKI[话题]#﻿ ﻿#小满科技[话题]#﻿﻿",
    "video_url": "",
    "time": "1754537298000",
    "last_update_time": "1754537359000",
    "user_id": "653a2382000000000301dbe5",
    "nickname": "OKKI",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/653a24c023c0f9000172bbe1.jpg",
    "liked_count": "627",
    "collected_count": "647",
    "comment_count": "2",
    "share_count": "48",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091346/ddb180047c3d175d78887f9b774bd08e/spectrum/1040g0k031ksajkooia005p9q4e10rmv5h1eqa48!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091346/a7aabc2e76ec0ab335615e3aa248b872/spectrum/1040g0k031ksajkooia0g5p9q4e10rmv5j01ju78!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091346/e42401b648b6406c0528b4a7b9af1d25/spectrum/1040g0k031ksajkooia105p9q4e10rmv51e6qgno!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091346/a5d0f32630187fed48b4ee6d7d5d7b7b/spectrum/1040g0k031ksajkooia1g5p9q4e10rmv5s9rd9h0!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091346/ad2669bfc8459300b593bd0365b4006e/spectrum/1040g0k031ksajkooia205p9q4e10rmv5nvgjbh0!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091346/8ea1f7a90f1ead2d3916e1c3fa3e7758/spectrum/1040g0k031ksajkooia2g5p9q4e10rmv5vg7tgmo!nd_dft_wlteh_jpg_3",
    "tag_list": "外贸,外贸找客户,外贸获客,外贸客户开发,外贸开发客户,外贸AI,外贸AI工具,OKKI,小满科技",
    "last_modify_ts": "1757396823771",
    "note_url": "https://www.xiaohongshu.com/explore/68941d520000000025021086?xsec_token=ABFnTfNdjHvKXQ0yCjcsL7d9v7l1EOoHGX91W3MgLKghc=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "689323c0000000002501c86e",
    "type": "normal",
    "title": "v我50👀教外贸姐妹无痛开发国外客户",
    "desc": "谁懂啊家人们！外贸客户开发搞到头秃的日子终于结束了！\n挖新客、盘老客、盯线索...一个人当八个人用？😭\n最近挖到一个宝藏AI工具——OKKI AiReach，简直是我的“客户开发外挂脑”！\n用了它，感觉打开了新世界的大门！分享几个让我尖叫的功能👇\n.\n1⃣️ 主动出击找新客户🔍 再也不怕漏掉大鱼\nOKKI AiReach就像个“行业侦探”，能顺着行业关系网（供应链图谱），自动帮我挖出上下游的精准潜客！那些藏在深处的客户，它都能揪出来！\n还能自己干活！深度背调找到关键人，然后自动发送“千邮千面”的定制化邮件营销！省心省力，线索滚滚来~ ✨\n.\n2️⃣ 老客户唤醒计划⏰别让金子埋在沙子里\nOKKI AiReach自动帮我筛选出最值得盘活的“金矿客户”！分析我的客户池，告诉我哪些优先跟进、能带来多少增长空间，连行动建议都安排好了！\n实时监控“金矿客户”动态！全网背调更新、行为追踪，一有风吹草动（商机）自动执行营销触达，我只用等着它交付有回复的客户💰\n.\n3️⃣ 越用越懂我的AI搭子🧠这默契感绝了\n超主动！ 不用我催，OKKI AiReach会主动汇报工作进展，比我还上心！\n有记忆！ 记住我的偏好和业务习惯，沟通超丝滑~\n能学习！ 持续复盘优化策略，用越久越懂我，像个真正的业务伙伴！\n.\n🙋一句话总结： 感觉多了个24小时不眠不休、智商爆表、还超懂我的“客户开发搭子”！\n找新客、盘老客效率翻倍，业绩增长肉眼可见！外贸姐妹们真的可以冲！💃\n﻿#外贸[话题]#﻿ ﻿#外贸找客户[话题]#﻿ ﻿#外贸获客[话题]#﻿ ﻿#外贸客户开发[话题]#﻿ ﻿#外贸开发客户[话题]#﻿ ﻿#外贸工具[话题]#﻿ ﻿#外贸AI[话题]#﻿ ﻿#外贸AI工具[话题]#﻿ ﻿#OKKI[话题]#﻿ ﻿#小满科技[话题]#﻿﻿",
    "video_url": "",
    "time": "1754473408000",
    "last_update_time": "1755050226000",
    "user_id": "653a2382000000000301dbe5",
    "nickname": "OKKI",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/653a24c023c0f9000172bbe1.jpg",
    "liked_count": "595",
    "collected_count": "618",
    "comment_count": "3",
    "share_count": "58",
    "sticky": "False",
    "ip_location": "广东",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/523492548a7e1e44032d397771e04a2b/spectrum/1040g0k031krc92rmjq005p9q4e10rmv58mijs1o!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/339fd60db2414c30d3fcba5d0b77ded6/spectrum/1040g0k031krc92rmjq0g5p9q4e10rmv5g2lf388!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/8f5977b452b1164d46d9f6c989ce7b6e/spectrum/1040g0k031krc92rmjq105p9q4e10rmv5m70mcko!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/07e53fb91a9c81f979625825f24a2c38/spectrum/1040g0k031krc92rmjq1g5p9q4e10rmv56pgrsno!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/ee7fd0420cf2b2b0c0fd36f05bbcd266/spectrum/1040g0k031krc92rmjq205p9q4e10rmv58b3hvno!nd_dft_wlteh_jpg_3",
    "tag_list": "外贸,外贸找客户,外贸获客,外贸客户开发,外贸开发客户,外贸工具,外贸AI,外贸AI工具,OKKI,小满科技",
    "last_modify_ts": "1757396823773",
    "note_url": "https://www.xiaohongshu.com/explore/689323c0000000002501c86e?xsec_token=ABr5sYLNzIy0Jq1r02ymqfBWwFo-JC8cccT6us9CHCRt0=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "6899b8a600000000250203df",
    "type": "normal",
    "title": "做外贸开发，不会用AI真的吃大亏😭",
    "desc": "你还在用Excel表格+人海战术开发外贸客户？😱\n熬夜搜邮箱、手动发开发信、苦等国外客户回复...\n🔥 别傻了，现在不用AI，等于把客户拱手让人！\n亲测用超级外贸营销AI搭子——OKKI AiReach，让客户开发效率指数级提升👇\n-\n1️⃣ 【AI开发新客户👁️】\n❌ 传统模式：\n手动扒FB/LY等社媒 → 开发信石沉大海 → 线索≈0\n✅ OKKI AiReach模式：\n➔ AI根据你的公司画像，秒出上下游供应链图谱 🔍甚至挖出了完全“意向不到”的潜客类型\n➔ 深度背调关键决策人联系方式 + “千邮千面”精准营销\n-\n2️⃣ 【AI盘活老客户🧪】\n❌ 传统模式：\n凭感觉盘客户 → 重点客户漏没发现 → 丢单拍大腿！\n✅ OKKI AiReach模式：\n➔ AI自动扫描客户池，揪出 “高潜沉睡户” ⏰\n➔ 实时盯梢客户动态（贸易数据采购信息等） + 个性化激活营销 💬\n-\n3️⃣ 【这AI还会“进化”🧠】\nOKKI AiReach更像是个贴心的小助理：\n主动汇报开发进度、根据业务偏好主动优化策略...\n-\n🙏 说句大实话：不用AI的外贸人，2025年真的会哭晕在厕所！\n🙌 现在上车OKKI AiReach还来得及！评论区见👇\n#外贸[话题]# #外贸开发[话题]# #外贸获客[话题]# #外贸AI[话题]# #外贸AI工具[话题]# #外贸人找客户[话题]# #开发新客户[话题]# #外贸获客[话题]# #OKKI[话题]# #小满科技[话题]#",
    "video_url": "",
    "time": "1754904742000",
    "last_update_time": "1755052121000",
    "user_id": "653a2382000000000301dbe5",
    "nickname": "OKKI",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/653a24c023c0f9000172bbe1.jpg",
    "liked_count": "490",
    "collected_count": "611",
    "comment_count": "2",
    "share_count": "47",
    "sticky": "False",
    "ip_location": "广东",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/58b08a2026d74fe261d4bdff8cc2187b/spectrum/1040g0k031l1prtnl4k005p9q4e10rmv5ptjdn9g!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/ac94e9ec14f12a1d84c9d10ab5214e28/spectrum/1040g0k031l1prtnl4k0g5p9q4e10rmv5a4ol558!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/0051f2541847c1724744155ceab7074d/spectrum/1040g0k031l1prtnl4k105p9q4e10rmv5obai6p0!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/4e84c440b94aa8f1a607984913caea2a/spectrum/1040g0k031l1prtnl4k1g5p9q4e10rmv5qbs7b50!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/e9f23ddd1ab10b05fc933f59f73a2aa2/spectrum/1040g0k031l1prtnl4k205p9q4e10rmv5juos27o!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/644869b40d0040d58105a7307b0037fc/spectrum/1040g0k031l1prtnl4k2g5p9q4e10rmv5i8e4htg!nd_dft_wlteh_jpg_3",
    "tag_list": "外贸,外贸开发,外贸获客,外贸AI,外贸AI工具,外贸人找客户,开发新客户,外贸获客,OKKI,小满科技",
    "last_modify_ts": "1757396823775",
    "note_url": "https://www.xiaohongshu.com/explore/6899b8a600000000250203df?xsec_token=ABMh9t-ek29tZEd5TdSHrNLsEzF8k22n22Gw9LtMHrH58=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "67cc07b8000000002900f6c6",
    "type": "video",
    "title": "当下正火的Ai，老石今天教你如何应用到我们实体商家，为老板引流获客，打爆同城，降本增效！#Ai #deepseek[话题]#",
    "desc": "当下正火的Ai，老石今天教你如何应用到我们实体商家，为老板引流获客，打爆同城，降本增效！#Ai #deepseek[话题]#",
    "video_url": "http://sns-video-bd.xhscdn.com/pre_post/1040g0cg31eou55pj6e505p3cqb7jou1m3issr0g",
    "time": "1741424569000",
    "last_update_time": "1741424569000",
    "user_id": "646cd2cf000000000f007836",
    "nickname": "创业-老石说",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31fri8ljs026g5p3cqb7jou1mhc3vr3o",
    "liked_count": "474",
    "collected_count": "620",
    "comment_count": "16",
    "share_count": "247",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/3347fe599fb9bee199a2e7a0e5605b3b/1040g2sg31eou5vsdmad05p3cqb7jou1m9i4qh8o!nd_dft_wlteh_jpg_3",
    "tag_list": "deepseek",
    "last_modify_ts": "1757396823777",
    "note_url": "https://www.xiaohongshu.com/explore/67cc07b8000000002900f6c6?xsec_token=ABVsGvBCjBLqx0OWs6BkiuoVZFchHFrmwsRqKL5KJBB9E=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "68528c20000000000d027a2c",
    "type": "normal",
    "title": "女朋友用GEMINI做GEO优化效果好恐怖～",
    "desc": "我惊呆了——我女朋友居然用 Gemini 做了一波 GEO（生成式搜索优化），效果好到让我怀疑人生！\n\t\n😱她的独立站没投一分广告，7天内自来流量翻了 3 倍，还持续每天带来 20+ 条精准咨询！什么是 GEO？ GEO（Generative Engine Optimization）是为 AI 问答和搜索引擎（如 Gemini、ChatGPT、Deepseek）量身打造的优化方法。它不再拼关键词排名，而是把你的内容写成 AI “最愿意引用”的标准答案，让用户在提问时直接看到，并跳转到你的站点！！\n\t\n她的实操三步走：\n1⃣ 捕捉高频问题 用 Gemini 模拟用户提问场景：“适合上班族护腰的靠垫？”、“干敏皮用哪款面霜？” 挑出 30 个精准问题锁定目标人群\n\t\n2⃣ 回答结构化 每条内容都用“问–答–要点”形式撰写 一句话总结核心卖点，附上 3 条对比优劣，最后推荐自家产品\n\t\n3⃣ 多平台投喂 & 提交 在官网博客、知乎、小红书同时发布 通过 Gemini API 插件或“外部知识源”入口提交，让 AI 快速抓取 真实数据\n\t\n✨ Gemini 推荐中出现品牌 50+ 次\n✨ 独立站日均 PV 从 100 飙升至 350\n✨ 新增 AI 自然流量用户 140+，高意向率达 25%\n✨ 整体转化率提升 60%，0 广告投放！\n\t\n她笑言：“SEO 抢首页靠运气，GEO 抢答 案靠逻辑。” 现在连我都要赶紧学这套 AI 话术，把流量红利拿下！\n\t\n#GEO优化[话题]# #Gemini引流[话题]# #AI搜索推荐[话题]# #生成式优化[话题]# #独立站流量[话题]# #智能问答流量[话题]# #精准获客[话题]# #内容结构化[话题]# #自然流量增长[话题]# #AISEO[话题]#",
    "video_url": "",
    "time": "1750240288000",
    "last_update_time": "1754104922000",
    "user_id": "6841929d000000001d014e06",
    "nickname": "geo搜索～云端共赢",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31icr93t07m605q21iaenajg6de5d2hg",
    "liked_count": "459",
    "collected_count": "717",
    "comment_count": "195",
    "share_count": "171",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/c17981727858d923203772072a33fe50/notes_pre_post/1040g3k831is9r6840m6g5q21iaenajg6vt22kro!nd_dft_wlteh_jpg_3",
    "tag_list": "GEO优化,Gemini引流,AI搜索推荐,生成式优化,独立站流量,智能问答流量,精准获客,内容结构化,自然流量增长,AISEO",
    "last_modify_ts": "1757396823778",
    "note_url": "https://www.xiaohongshu.com/explore/68528c20000000000d027a2c?xsec_token=AB98QMMxPYeat1m31mkjAk9ZOoQBOikqSQ3SYIUDzXZFQ=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "67a5ca5a000000001800d1d1",
    "type": "normal",
    "title": "🔥美容院用Ai拓客模式，三个月赚47万❗️",
    "desc": "一位95年女生开在小区的美业店，不到60平的的小店，凭着Ai拓客模式，1每天新客不断，1月份就收了21w！🎉\n\t\n不仅每天顾客满满，仅用了三个月时间，就成了当地的网红店🔥~\n\t\n这是怎么做的呢？一起来看看吧\n\t\n🟤 当你还在发传单时，附近的竞争对手已经借助Deepseek分析了周边小区的业主群，自动制作了“宝妈偷美秘籍”表情包，悄然启动了病毒式传播模式🚀\n\t\n🟤 当你在为吸引客户在线烦恼时，同行们已经运用AI技术将美团上的差评转化为“焦虑变现利器”💸！它能精准扫描公域数据，甚至把“效果慢”的负面评论变成了《三天换脸挑战》直播，吸引了大量客户！\n\t\n🟤 当你还在为员工培训话术发愁时，别人已经利用AI客服在深夜为客户制作了“衰老预见图”📸，并通过一整套SOP流程轻松满足客户需求，迅速吸引了线上同城客户。借助同城热词扫描功能，发现“通勤妆急救”搜索量激增300%，立刻推出午休美容卡，迅速占领市场！\n\t\n🟤 更让人震惊的是，客服的朋友圈已经设计了“分魂术”：将“幼儿园门口惊艳妈圈”项目推送给宝妈，将“会议摄像头美颜”套餐推送给白领💼！\n\t\n你还在笑别人不会用AI？事实上，当你还在手动操作时，同行们已经通过算法悄悄地获得了你客户手机里的搜索记录和兴趣偏好！\n这不是危言耸听，而是正在发生的降维打击⚡️！\n\t\n👇点击下方，立刻获取《2025美业AI抢客秘籍》！\n\t\n﻿#美容院[话题]#﻿ ﻿#美容院拓客[话题]#﻿ ﻿#美容院经营[话题]#﻿ ﻿#美容院拓客活动方案[话题]#﻿ ﻿#美容院老板[话题]#﻿ ﻿#AI美业[话题]#﻿ ﻿#山东[话题]#﻿ ﻿#济南[话题]#﻿",
    "video_url": "",
    "time": "1738918490000",
    "last_update_time": "1738918490000",
    "user_id": "66d55144000000001d031723",
    "nickname": "美业助理",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31kap7210k8005pmla527e5p3gac2edg",
    "liked_count": "445",
    "collected_count": "200",
    "comment_count": "187",
    "share_count": "75",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/2b64b4bd7e81bd5f1c92944254dc2cda/spectrum/1040g0k031djihb63h2005pmla527e5p3i13a100!nd_dft_wlteh_jpg_3",
    "tag_list": "美容院,美容院拓客,美容院经营,美容院拓客活动方案,美容院老板,AI美业,山东,济南",
    "last_modify_ts": "1757396823780",
    "note_url": "https://www.xiaohongshu.com/explore/67a5ca5a000000001800d1d1?xsec_token=ABkvRt4wLoLM6y9XiJCi7AHYXhHTYTVMkO0IiSOpVkmh8=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "687a1fec0000000017033fe8",
    "type": "video",
    "title": "很多投聚光的商家还没用上这个AI获客工具😱",
    "desc": "小红书官方授权的私信获客神器—— 「来鼓AI」已经帮助众多小红书头部品牌获客了，覆盖教育 医美 旅游等众多行业，想👀案例的品牌🉑私戳@来鼓AI ‼️‼️‼️\n\t\n还有不懂的商家一定要用起来了\n\t\n‼️3大核心功能：\n✅AI员工7*24小时秒级响应，自动发卡留资！\n\t\n✅自动化获客场景👏🏻一键开启🔛自动化，针对未留资客户，意向评论客户主动私信追回，大大提高留资率\n✅还有智能客户画像，自动打标签🏷️快速识别高价值客户 转化更轻松啦[派对R]\n\t\n如果你也有聚光投放客资成本💰高，多账号管理难，线索浪费等客资难题，[自拍R]不妨跟我们聊聊~\n\t\n现在来鼓AI 推出免费试用‼️\n现在来鼓AI 推出免费试用‼️\n现在来鼓AI 推出免费试用‼️\n左下角【立即咨询】或私戳@来鼓AI，填写留资卡即可接入试用\n[派对R]注意啦，仅限开聚光的企业号和KOS账号哟‼️\n\t\n#来鼓[话题]#     #聚光[话题]#     #私信通[话题]#     #AI[话题]#     #数字化企业[话题]#     #小红书企业号运营[话题]#     #广告投放[话题]#     #引流获客[话题]#     #获客引流[话题]#     #聚光接入来鼓ai[话题]#",
    "video_url": "http://sns-video-bd.xhscdn.com/pre_post/1040g2t031k2mda6miul05piul260u4s38lqb6p0",
    "time": "1752834028000",
    "last_update_time": "1752834029000",
    "user_id": "665ea88c0000000003031383",
    "nickname": "来鼓AI",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31477u8uq1m005piul260u4s362thv78",
    "liked_count": "456",
    "collected_count": "364",
    "comment_count": "38",
    "share_count": "101",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/b0414802613c4d1dfec4da4fe7b3660b/1040g00831k2mda8uio005piul260u4s3tcclh98!nd_dft_wlteh_jpg_3",
    "tag_list": "来鼓,聚光,私信通,AI,数字化企业,小红书企业号运营,广告投放,引流获客,获客引流,聚光接入来鼓ai",
    "last_modify_ts": "1757396823781",
    "note_url": "https://www.xiaohongshu.com/explore/687a1fec0000000017033fe8?xsec_token=ABcAstRh7-3JBIzwnOSEurqb_FWxKJfelpWfMq5zt1JL0=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "680a1f7a000000000f0326f1",
    "type": "normal",
    "title": "律师，请牢牢抓住这5个案源拓展方式",
    "desc": "整理了一下5个案源拓展方式，分享给大家。\n\t\n一、自媒体平台（打造个人品牌）\n1.微信公众号：适合深度法律分析\n2.小红书：短平快普法，吸引年轻用户\n3.抖音/快手：短视频普法，流量大\n4.B站/视频号：中长视频，适合案例解析\n核心方法：持续输出实用法律内容，积累精准客户\n\t\n二、法律科技平台（智能匹配案源）\n1.得理律助：AI推荐案源、数字名片、商机推送\n2.华律网/找法网：入驻接咨询，线上委托\n3.法大大/律呗：电子合同、企业法律服务对接\n适合人群：希望高效获客的律师\n\t\n三、政府公益平台（稳定案源）\n1.12348法网值班：司法局官方咨询，案源稳定\n2.法律援助中心：加入法援律师库，承接指派案件\n3.工会/妇联合作：劳动、家事案源集中\n优势：适合初期积累客户，社会价值高\n\t\n四、商业合作平台（企业案源）\n1.企业法律顾问平台（如“公司宝”）：中小企业常法需求\n2.商会/行业协会：加入本地商会，获取企业客户\n3.银行/保险公司合作：金融纠纷案源\n适合人群：专注企业法律服务的律师\n\t\n五、线下拓展（传统但有效）\n1.社区普法讲座：街道、居委会合作\n2.律所联合推广：同行资源共享\n3.老客户转介绍：优质服务带来新案源\n\t\n⚠️高效组合策略\n-短期快速获客：法律科技平台+政府公益\n-长期品牌建设：自媒体+线下活动\n-高质量案源：商业合作+法援转介\n\t\n﻿#律师案源[话题]#﻿ ﻿#独立律师[话题]#﻿ ﻿#法律人[话题]#﻿ ﻿#青年律师成长[话题]#﻿ ﻿#法律干货[话题]#﻿",
    "video_url": "",
    "time": "1745538002000",
    "last_update_time": "1745493882000",
    "user_id": "65c19350000000000903c3e8",
    "nickname": "RuoRuooooo",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo30uqngkrk544g5pe1i0pifndtvbolc7g",
    "liked_count": "403",
    "collected_count": "574",
    "comment_count": "14",
    "share_count": "103",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/7c346ba92d3b0212b446e4ef4f4e9e0a/spectrum/1040g34o31glihsti380g5pe1id82fgv8vh66b8g!nd_dft_wlteh_jpg_3",
    "tag_list": "律师案源,独立律师,法律人,青年律师成长,法律干货",
    "last_modify_ts": "1757396823783",
    "note_url": "https://www.xiaohongshu.com/explore/680a1f7a000000000f0326f1?xsec_token=ABbn8M0OpW4QQhIyMbQZVpt3C6wba3Y8BvaSb_LlazHzI=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "68835dd10000000022033258",
    "type": "video",
    "title": "不拍不剪！AI帮你自动搞流量！老板必看",
    "desc": "﻿﻿#老高AI[话题]#﻿  ﻿﻿#企业AI[话题]#﻿ ﻿  ﻿﻿#私域[话题]#﻿ ﻿  ﻿﻿#天量商学[话题]#﻿ ﻿  ﻿﻿#商业思维[话题]#﻿ ﻿  ﻿﻿#获客[话题]#﻿ ﻿  ﻿﻿#天量AI商学[话题]#﻿ ﻿  ﻿﻿#AI创业[话题]#﻿ ﻿  ﻿﻿#AI获客[话题]#﻿ ﻿  ﻿﻿#企业AI培训[话题]#﻿",
    "video_url": "http://sns-video-bd.xhscdn.com/spectrum/1040g35031kbvc3133q0g5q0scsfji2hja44rroo",
    "time": "1753439697000",
    "last_update_time": "1753439697000",
    "user_id": "681c671f000000000e010a33",
    "nickname": "广州天晟AI",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/0af665ad-fd1f-3e15-8d82-3ce4e4260167",
    "liked_count": "393",
    "collected_count": "233",
    "comment_count": "346",
    "share_count": "77",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/c1d4a30eeeb314399901e1177db9b837/spectrum/1040g34o31kbvd7v9ia7g5q0scsfji2hjk88oak0!nd_dft_wlteh_jpg_3",
    "tag_list": "老高AI,企业AI,私域,天量商学,商业思维,获客,天量AI商学,AI创业,AI获客,企业AI培训",
    "last_modify_ts": "1757396823784",
    "note_url": "https://www.xiaohongshu.com/explore/68835dd10000000022033258?xsec_token=ABOfUjrxgVdzy095VxgX9CIUF48hCFIGVlVgy2V1vsXaI=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "68885ed1000000002302a468",
    "type": "normal",
    "title": "开单啦，7月第10单，30240美金！💰💰",
    "desc": "经常有很多做外贸的小伙伴咨询我是怎么开单的，除了公司提供的获客平台外之前我主要是用facebook和谷歌开发，刚开始确实出了一些结果，但时间久了会发现这些方式越来越吃力：\n邮箱质量参差不齐；有效联系人的比例很低、大量信息重复；开发信发出去没回应、想换国家试试，却又不知道从哪入手。\n做外贸除了坚持和运气，最重要的还是要找对方法，就会事半功倍。\n\t\n年初跟几个同行聊天，都推荐用这个工具（信风AI）开发。我试了下，发现真的挺适合我们这种靠自己开发客户的人。\n我平时是这样用的：\n输入你要搜索的品类，输入国家和行业关键词，系统就会用AI进行实时搜索，找到对口岗位的决策人邮箱、电话；\n客户信息找到后，可以直接用模板自动发送邮件，还会每周自动跟进新客户；\n不光有邮件营销，还能自定义组合开发，比如开发信+AI电话；WhatsApp+Facebook。。。直接给AI完成就好。\n整体感受就是：以前我是一条条去搜、去发，现在是我设好条件，它自己每周给我推送新的客户资源，然后我再挑重点客户去跟进。\n还是挺推荐大家先试用体验下~\n\t\n#外贸[话题]# #外贸分享[话题]# #外贸soho小伙伴[话题]# #开发新客户[话题]# #外贸开发[话题]# #开单[话题]# #信风AI[话题]#",
    "video_url": "",
    "time": "1753768078000",
    "last_update_time": "1753767633000",
    "user_id": "5e9d53850000000001004817",
    "nickname": "妞酱喵喵喵",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31319r93cmc6g5nktae2g8i0ng8vqlug",
    "liked_count": "227",
    "collected_count": "116",
    "comment_count": "36",
    "share_count": "9",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/df7737f6100b6416b8f31bb914885aba/notes_pre_post/1040g3k831kgrogutioe05nktae2g8i0nqvag1no!nd_dft_wgth_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/0bc89539fda4c84106f183f3d1bfce00/notes_pre_post/1040g3k831kgrogutioc05nktae2g8i0nri6st6o!nd_dft_wgth_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/c20bfb821d63d949db56ab5c80752aa5/notes_pre_post/1040g3k831kgrogutio9g5nktae2g8i0n2vfjtgo!nd_dft_wgth_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/ddb191d3698dd51b9779671ce04074fc/notes_pre_post/1040g3k831kgrogutiocg5nktae2g8i0n4jfijm8!nd_dft_wgth_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/f377aa6917873fff7dff86962e17a768/notes_pre_post/1040g3k831kgrogutiod05nktae2g8i0npri9h2g!nd_dft_wgth_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/5c481486faa82215d8e67af4331b3e9a/notes_pre_post/1040g3k031kgrogutio705nktae2g8i0n1bu8rfo!nd_dft_wgth_jpg_3",
    "tag_list": "外贸,外贸分享,外贸soho小伙伴,开发新客户,外贸开发,开单,信风AI",
    "last_modify_ts": "1757396844629",
    "note_url": "https://www.xiaohongshu.com/explore/68885ed1000000002302a468?xsec_token=ABW_ZJnPJ7uJjH41Yy3CXFgYbpc61PvTYUSlEqvc9P8kc=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "68790318000000001c031f11",
    "type": "video",
    "title": "理发店一个很恶心但是真有用的引流方法",
    "desc": "#高业绩发型师必备[话题]# #发型师朋友圈[话题]# #发型师[话题]# #高业绩发型师[话题]# #发型师朋友圈文案[话题]# #理发店引流拓客[话题]# #AI工具[话题]# #理发师[话题]# #美发[话题]# #AI人工智能[话题]#",
    "video_url": "http://sns-video-bd.xhscdn.com/pre_post/1040g0cg31k1rrbt12org5q1dcofj9ftab1952n8",
    "time": "1752761112000",
    "last_update_time": "1752761113000",
    "user_id": "682d661f000000000d00bfaa",
    "nickname": "武林云科技-美业吼哈itonypro",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/e718d54c-53e5-3a15-a6f6-207595b59159",
    "liked_count": "188",
    "collected_count": "97",
    "comment_count": "20",
    "share_count": "44",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/fbae6a15cfaf3ead0c4736f6c75ec29e/1040g2sg31k1rrbggiod05q1dcofj9ftarfg5pog!nd_dft_wlteh_jpg_3",
    "tag_list": "高业绩发型师必备,发型师朋友圈,发型师,高业绩发型师,发型师朋友圈文案,理发店引流拓客,AI工具,理发师,美发,AI人工智能",
    "last_modify_ts": "1757396844632",
    "note_url": "https://www.xiaohongshu.com/explore/68790318000000001c031f11?xsec_token=ABkm98XOF_6FoUlk1nsPzWSGAANijBeQCYwWRqUs4n_dE=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "682da70900000000200292af",
    "type": "normal",
    "title": "不需要发作品 靠jie流真的能获客嘛？",
    "desc": "前两周也不知道我老板从来弄来的获客工具  ，跟我说不发笔记也能获客，而且不违规不封禁不警⚠️ ，以后用这个工具就能获客了。\n刚开始我是不相信的，但是用了两天，我彻底信了，这小玩意还挺厉害，24小时不间断去同行笔记p论区截流客户，还能针对同行投流推广的这些bj下去自动获取，最主要是截取一手zi源有一套，已经快要把我的职位给淘汰了，这样下去了可怎么办啊[捂脸R]#ai获客[话题]# #引流获客[话题]# #获客渠道[话题]##获客系统[话题]##线上获客[话题]##如何获客[话题]#",
    "video_url": "",
    "time": "1747822345000",
    "last_update_time": "1747822346000",
    "user_id": "67b6ee21000000000601caa4",
    "nickname": "萌奇奇",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31e55v08l106g5ptmtoghjil43adk9tg",
    "liked_count": "79",
    "collected_count": "34",
    "comment_count": "89",
    "share_count": "9",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/df6a56b8589129364e0d381357754611/notes_pre_post/1040g3k831ho8hlug3s605ptmtoghjil46k9a1bo!nd_dft_wlteh_jpg_3",
    "tag_list": "ai获客,引流获客,获客渠道,获客系统,线上获客,如何获客",
    "last_modify_ts": "1757396844635",
    "note_url": "https://www.xiaohongshu.com/explore/682da70900000000200292af?xsec_token=ABOQ4_v31zig42CZNE1gP5cWjM2id8I4p690UwlmSKp6M=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "669cd2e1000000000a026b60",
    "type": "video",
    "title": "好产品配上AI获客，简直就是印钞机！！",
    "desc": "好产品配上AI获客，简直就是印钞机，1天帮你加1000个精准客户#企业[话题]##AI[话题]##获客[话题]##客户[话题]##精准客户[话题]##拓客[话题]##精准流量[话题]##老板[话题]##企业[话题]#",
    "video_url": "http://sns-video-bd.xhscdn.com/1040g00g315gqti5hgu6g5pcemedog5ibqgu88g0",
    "time": "1721553633000",
    "last_update_time": "1721553633000",
    "user_id": "658eb39b000000002200164b",
    "nickname": "小红薯680F7137",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo314ki0dma6g6g5pcemedog5ibmmkd9og",
    "liked_count": "85",
    "collected_count": "49",
    "comment_count": "64",
    "share_count": "12",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/34ad2d9439786f4b12393c6aa5cb6d83/1040g008315gqv1o9hc5g5pcemedog5ibqm1mpog!nd_dft_wlteh_jpg_3",
    "tag_list": "企业,AI,获客,客户,精准客户,拓客,精准流量,老板",
    "last_modify_ts": "1757396844636",
    "note_url": "https://www.xiaohongshu.com/explore/669cd2e1000000000a026b60?xsec_token=ABs2ZuSem1LyhDa5XgI7aors2LTZEmo7S30Dna0Z5uezQ=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "67cbcd1e000000002902caa5",
    "type": "normal",
    "title": "麻了麻了 私域做裂变，还得是用ai",
    "desc": "宝子们，每天早上起来，手机就“叮咚叮咚”响个不停，全是消息和好友申请😭。\n我好想把这些事分给别人做，但看看周围，大家都忙着玩，就我一个人在这儿努力搞事情，感觉自己像个“孤军奋战的战士”🛡️！\n不过，我也只能咬咬牙坚持啦，希望我的努力不会白费，苦尽甘来的一天一定会来，冲鸭🚀！#私域[话题]# #私域流量[话题]# #引流获客[话题]# #引流[话题]#",
    "video_url": "",
    "time": "1741409566000",
    "last_update_time": "1741409566000",
    "user_id": "676ce7b000000000180142a8",
    "nickname": "雪儿姐姐笔记",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31btk45fl0s005prcsuo62gl881rhhpo",
    "liked_count": "128",
    "collected_count": "29",
    "comment_count": "68",
    "share_count": "5",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/28dfd5072da1b125d652f447c19c27f1/1040g00831enge6mj6e105prremhm3r8c6a9sq8o!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/d2fb31f2ba101c70b299585807f686c7/1040g00831enge6mj6e1g5prremhm3r8c9i5m9r8!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/5ba6d65113a6c8c66b3f337883931c8e/1040g00831enge6mj6e205prremhm3r8c47d1i4o!nd_dft_wlteh_jpg_3",
    "tag_list": "私域,私域流量,引流获客,引流",
    "last_modify_ts": "1757396844638",
    "note_url": "https://www.xiaohongshu.com/explore/67cbcd1e000000002902caa5?xsec_token=ABe5BQ3gV40obpLjwUOMfm6NMS7Kk0XuupEKs-Fgy3WU8=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "68775e26000000001201582d",
    "type": "normal",
    "title": "获客新革命 私信躺赢",
    "desc": "告别无效客服！🤖AI私信追粉秘籍大公开，留资率暴涨200%🔥\n💥救命！家人们谁懂啊？线上获客难哭😭，用户已读不回太扎心💔！\n直到用了住小橙AI客服——全渠道私信躺平收客资，简直开挂✨！\n🧠【黑科技追粉】\nAI拟人化聊天绝了🎭！精准识别潜台词+情绪波动😏～智能追问时机拿捏死死⏱️，连沉默用户都能撬开嘴🗣️！留资后自动打标🏷️，用户需求智能全解析🏠～\n📊【真实暴涨案例】\n家装客户用它替代20个客服👩💻！开口率⬆️留资率⬆️，客资质量吊打表单留资💯！眼科医院靠它小红书追粉，留资率蹭蹭涨📶～\n👇【薅羊毛通道】\n评论“AI秘籍”，送你《私信留资SOP话术包》🎁！企业主必冲，真·0成本躺赚🤯！\n🔥AI追粉、留资暴涨、全渠道获客、0成本客服、私信转化\n现在点击左下角【立即咨询】🉐7天免费试用，让你的小红书私信变成24小时不打烊的“自动获客机器”！\n﻿#住小橙[话题]#﻿   ﻿#聚光[话题]#﻿   ﻿#专业号[话题]#﻿   ﻿#引流获客[话题]#﻿   ﻿#ai工具[话题]#﻿   ﻿#智能客服[话题]#﻿   ﻿#私信通[话题]#﻿   ﻿#获客技巧[话题]#﻿   ﻿#小红书创业[话题]#﻿   ﻿#企业数字化转型[话题]#﻿﻿",
    "video_url": "",
    "time": "1752653350000",
    "last_update_time": "1753954854000",
    "user_id": "668dff220000000003033395",
    "nickname": "住小橙丨AI",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/6870defb5ea71e0001082333.jpg",
    "liked_count": "79",
    "collected_count": "19",
    "comment_count": "35",
    "share_count": "12",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/957cbf563714316e0fda69036a8071d9/spectrum/1040g34o31k07i3s9jq505pkdvsh0ucsl783av5o!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/4342e64f204de5b2931e49d01b74333a/spectrum/1040g34o31k07i3s9jq5g5pkdvsh0ucslnbdvg4o!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/7991144f890d0122edf7946f391e9298/spectrum/1040g34o31k07i3s9jq605pkdvsh0ucsl9nv2ig0!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/0bf1af89a8c68c33f1ff6ce4274a0b2a/spectrum/1040g34o31k07i3s9jq6g5pkdvsh0ucsl5aupulg!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/1ee7be5afcab905e12a5b614df256ce0/spectrum/1040g34o31k07i3s9jq705pkdvsh0ucslap0b0io!nd_dft_wlteh_jpg_3",
    "tag_list": "住小橙,聚光,专业号,引流获客,ai工具,智能客服,私信通,获客技巧,小红书创业,企业数字化转型",
    "last_modify_ts": "1757396844639",
    "note_url": "https://www.xiaohongshu.com/explore/68775e26000000001201582d?xsec_token=ABYVY0rU0aSyl_mcCFrkvM4mCwKBhs2XvtJ8CkxKp0Umw=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "68076dda000000000b02caea",
    "type": "normal",
    "title": "重磅‼️用这个AI自动获客💁小红书新功能",
    "desc": "📣重大通知 小红书私信获客规则大升级\n只能通过✅名片卡/留资卡获取线索\n且需近30天聚光消耗＞0才能解锁权限！\n\t\n💥别让新规拖慢你的转化节奏！\n\t\n【来鼓AI员工】小红书官方认证工具，目前接入🍠客户坠多的三方IM商\n\t\n✨ 来鼓AI三大王炸功能\n❶ AI24*7无休聊客户，自动发🍠私信卡片💳留资卡/名片卡，🉑自动分配，销售直接跟进[自拍R]\n❷ 多账号企业号&KOS账号聚合，数据📊私信评论一个平台全搞定\n❸ 批量追回未留资为开口客户，留资率🆙🆙\n\t\n只需两步即🉑接入\n限时福🎁评论区扣【试用】并私@来鼓AI 领AI员工体验权\n\t\n#小红书企业号运营[话题]# #私信通[话题]# #来鼓[话题]# #AI[话题]# #聚光接入来鼓ai[话题]# #AI人工智能[话题]# #内容运营[话题]# #广告投放[话题]# #聚光[话题]# #聚光平台[话题]# #引流获客[话题]# #小红书运营干货[话题]# #小红书新规[话题]#",
    "video_url": "",
    "time": "1745317338000",
    "last_update_time": "1747986592000",
    "user_id": "665ea88c0000000003031383",
    "nickname": "来鼓AI",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31477u8uq1m005piul260u4s362thv78",
    "liked_count": "73",
    "collected_count": "44",
    "comment_count": "34",
    "share_count": "32",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/5aea57116549a89e39f4682bdef0422f/notes_pre_post/1040g3k031giu0n1a3i305piul260u4s332ievpg!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/5e6d819e34c79549afd35a52ebeb15da/notes_pre_post/1040g3k031giu0n1a3i3g5piul260u4s3pne5oog!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/5c4c50439302c763c59fc519b23b33a6/notes_pre_post/1040g3k031giu0n1a3i405piul260u4s3arqn990!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/ab9c1fb351366e4995472f61aba5bbf8/notes_pre_post/1040g3k031giu0n1a3i4g5piul260u4s3gqqdpe0!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/dc0999056c89981bce9163d14d8b5f04/notes_pre_post/1040g3k031giu0n1a3i505piul260u4s3i4qgi58!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/f89963860808dc1aae58344a4847a328/notes_pre_post/1040g3k031giu0n1a3i5g5piul260u4s3jndef2o!nd_dft_wlteh_jpg_3",
    "tag_list": "小红书企业号运营,私信通,来鼓,AI,聚光接入来鼓ai,AI人工智能,内容运营,广告投放,聚光,聚光平台,引流获客,小红书运营干货,小红书新规",
    "last_modify_ts": "1757396844641",
    "note_url": "https://www.xiaohongshu.com/explore/68076dda000000000b02caea?xsec_token=ABUT1e9DdOjBQ9EaSraWlpv4k-nrhoWwksafDI4r7DbzA=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "66e55c820000000012010faa",
    "type": "normal",
    "title": "暴力AI的方法一天可以加1000人",
    "desc": "如今，流量越来越昂贵，很多企业和个人都在努力将流量引入私域。私域引流的重要性日益凸显，紧接着就是通过精细化的运营，提高复购率。\n\t\n第一点，截流术的确存在争议。这种方法不依赖优质内容，而是通过在他人的评论区进行互动来吸引关注。想象一下，有人辛苦种出一棵大树，而你却在旁边如同蛙虫一样吸血，这就是截流术的本质。\n\t\n第二点，这种方法的威力不容小觑。有的公司甚至组建了100人的截流团队，每人每天可以引流50到200人，一个月下来，能将30万流量引入私域，实在是让人震惊！千里之堤毁于蚁穴，这种方式确实恐怖。\n\t\n举个例子，某个健康领域的团队，每个人手上有10部手机，每天加200个精准粉丝。操作方式非常简单：在相关帖子下，看到评论者提问“如何消肿”，便用小号互动，询问具体方法，再用主号回复，形成互动和引流。\n\t\n这样的评论和互动，能有效吸引关注，只要视频有流量，就会源源不断地收到私信。这些人悄无声息地进入你的私域，每天可能收到上千条私信。\n\t\n关键在于，当有人私信你时，可以借助其他人的力量回应，形成口碑传播。这种人传人的方式是极其有效的。\n想要放量操作，一个人可以同时使用10台手机，每个账号每小时在行业评论区评论一条，尽量模拟真人账号，以避免封号风险。只要保持循环，这样一天能加100人，如果有10个人一起操作，每天就能轻松获取1000人！[哇R]\n\t\n#微营销[话题]# #社群运营[话题]# #营销方案[话题]# #线上引流拓客[话题]# #身体店引流[话题]# #私域引流技巧[话题]#",
    "video_url": "",
    "time": "1726307458000",
    "last_update_time": "1751885070000",
    "user_id": "669e34c8000000002402046a",
    "nickname": "老高商业（50万私域粉丝）",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31670a778hc605pku6j49413a5bkv420",
    "liked_count": "153",
    "collected_count": "72",
    "comment_count": "48",
    "share_count": "20",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/2a2fd268dd9ce0e1c6a862952a41c991/1040g2sg317nlp526ju5g5pku6j49413au1b7ra0!nd_dft_wlteh_jpg_3",
    "tag_list": "微营销,社群运营,营销方案,线上引流拓客,身体店引流,私域引流技巧",
    "last_modify_ts": "1757396844642",
    "note_url": "https://www.xiaohongshu.com/explore/66e55c820000000012010faa?xsec_token=ABr_i6DcOmbohRrLRfBAFlQMCEQFk0LZF9Tx7z7-3bviw=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "66828d97000000001f0073e9",
    "type": "normal",
    "title": "00后野路子，AI+引流，真的太香啦",
    "desc": "#私域[话题]#  #00后[话题]#  #野路子[话题]#  #AI[话题]#  #太香了[话题]#  #引流[话题]#  #引流获客[话题]#",
    "video_url": "",
    "time": "1719831959000",
    "last_update_time": "1719831959000",
    "user_id": "605fd0e90000000001005c02",
    "nickname": "支撑她的颠沛流离",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/605fd12336f2ae0001d93633.jpg",
    "liked_count": "189",
    "collected_count": "60",
    "comment_count": "57",
    "share_count": "0",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/5992f8f265ae14f721ff3f419f635811/spectrum/1040g34o314n603utlq0g5o2vq3kg8n02943vdhg!nd_dft_wlteh_jpg_3",
    "tag_list": "私域,00后,野路子,AI,太香了,引流,引流获客",
    "last_modify_ts": "1757396844643",
    "note_url": "https://www.xiaohongshu.com/explore/66828d97000000001f0073e9?xsec_token=ABatHb1RjlV8PuQeXn7zJ-21pOOrYf-MhO1HKct62r1xw=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "68a304a2000000001c00451c",
    "type": "video",
    "title": "用AI接入小红书多账号，这个工具获客绝了！",
    "desc": "#小红书运营[话题]# #来鼓[话题]# #来鼓AI[话题]# #AI客服[话题]# #AI工具[话题]# #内容营销[话题]##ai矩阵获客[话题]# #留资获客[话题]# #小红书获客[话题]# #小红书变现[话题]#",
    "video_url": "http://sns-video-bd.xhscdn.com/pre_post/1040g2t031lasehgple0g5pf1dm2h9b6lo6qp3h8",
    "time": "1755520832000",
    "last_update_time": "1755518843000",
    "user_id": "65e16d85000000000500acd5",
    "nickname": "科技阿黑",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31e43p1s8gm005pf1dm2h9b6lu0mvmtg",
    "liked_count": "57",
    "collected_count": "53",
    "comment_count": "3",
    "share_count": "8",
    "sticky": "False",
    "ip_location": "重庆",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/38ba56c83d8f77a086aa5659ada904c5/1040g00831lasehgp5m005pf1dm2h9b6lbn9anr0!nd_dft_wlteh_jpg_3",
    "tag_list": "小红书运营,来鼓,来鼓AI,AI客服,AI工具,内容营销,ai矩阵获客,留资获客,小红书获客,小红书变现",
    "last_modify_ts": "1757396844645",
    "note_url": "https://www.xiaohongshu.com/explore/68a304a2000000001c00451c?xsec_token=ABLEMnJeq-xRy1Q_851-hF4ysLrP63gnKoFj5TEqzFpeY=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "68022413000000001d001ffd",
    "type": "normal",
    "title": "AI获客系统是骗人的吗？是不是智商税？",
    "desc": "最近刷到很多AI获客相关视频，大概内容就是30分钟成交一个客户，永不疲倦的AI销冠等等，看了很是心动，心想有这么一套系统，那不是可以躺赚了！但我都觉得简单的事情肯定没有那么简单！[笑哭R]\n\t\n那AI销冠到底是不是智商税呢？作为一个十年经验且从事相关行业的销售，我想谈谈自己的看法[暗中观察R]\n我觉得在特定的，单一的任务上，AI是可以超越人类的。但销售是一系列复杂任务的组合，比拼的是综合能力，当前的AI跟人类销售差距还很大，更别提销冠了[大笑R]\n\t\n如果抱着有这么一套AI系统就能躺赚的想法，肯定是无法实现的。但是如果你把他当做一个高效的辅助性工具，通过人+AI工具高效组合，那他就会成为你业绩增长的强力伙伴。[点赞R]\n\t\n分享一下我最近使用AI销售工具经历[暗中观察R]\n\t\n我主要是把找客户、触达客户这些平常需要花费80%时间的事情，交给系统去完成，而我的时间都用在跟意向客户沟通、制作方案、报价上面，效率提升了几倍，随之带来了业绩的增长📈\n\t\n这个AI销售工具有以下几个特点👇\n\t\n一是自带客户库🗂️\n完善客户画像就能获得优质客户推荐，数据来源是国内某获客行业独角兽公司，数据全，相对精准。\n\t\n二是内置了常用APP自动化程序📱\n起到高效触达和筛选潜在客户的作用\n\t\n三是支持app6️⃣开\n用于增加每天触达客户的上限，也可以作为新媒体矩阵，增加内容基础曝光，等客户主动咨询。\n\t\n四是具备客户管理功能📁\n支持聚合多平台客户统一管理，自动生成互动记录，方便二次跟进\n\t\n五是内置了deepseek 大模型和多个效率工具🛠️\n可以推荐视频素材、生成各类文案、话术、图片素材，能提取文案、去水印、去重等，进一步提升效率。\n\t\n我平常使用主要分为两种方式[红色心形R]\n\t\n一是主动获客[一R]\n通过完善客户画像，将推荐的客户导出到客户池，再通过自动化程序触达，筛选出意向客户，再去重点跟进。\n\t\n二是被动获客[二R]\n通过6开运营多个账号发内容，获得更多基础曝光，让客户主动咨询，通过客户管理进行分类，高意向重点跟进，潜在客户通过自动化工具进行维系，确保客户有需求时能联系到。\n\t\n那AI销冠是不是智商税呢？相信你已经有答案了[doge]\n\t\n我的看法是，就当前技术水平来说，AI销冠更多是噱头，无法超越人类销冠，但确实可以代替一些重复性工作，AI+人目前才是最优组合！\n\t\n如果有不同看法，欢迎评论区沟通交流哦[飞吻R]\n#AI[话题]#  #人工智能[话题]#  #AI销售[话题]#  #AI获客[话题]#  #AI销冠[话题]# #AI工具[话题]# #销售[话题]#",
    "video_url": "",
    "time": "1744970771000",
    "last_update_time": "1744970771000",
    "user_id": "643bff54000000000d019a32",
    "nickname": "开单果 上海",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31e88i1g7ge505p1rvta3b6hiaq5qva8",
    "liked_count": "65",
    "collected_count": "52",
    "comment_count": "68",
    "share_count": "11",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/1235b50567fe400df22875549f737b3c/1040g2sg31gdp4n8sja6g5p1rvta3b6hihk82gn0!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/2dd32943307cbb4cfad2b2eba9ab78bc/1040g2sg31gdp4n8sja4g5p1rvta3b6hijou2h2g!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/341a86a2978f6f9573e5010377151c90/1040g2sg31gdp4n8sja3g5p1rvta3b6hirrutv30!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/e9307ec9b7823a6d8af92be95ff19390/1040g2sg31gdp4n8sja305p1rvta3b6hicfvmoio!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/835af098d1340cbb76a946f4e5f298b2/1040g2sg31gdp4n8sja605p1rvta3b6hivm97t40!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/db7b6d07d66f16b09fd0285c4e578098/1040g2sg31gdp4n8sja205p1rvta3b6hiqs2bv5o!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/adddd1a8d96562ae1c438cff7d14811c/1040g2sg31gdp4n8sja2g5p1rvta3b6hi4sjtcf0!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/6e31513f563f21a0dc7e5aab7f0c7fe4/1040g2sg31gdp4n8sja0g5p1rvta3b6his1v6vuo!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/8a3db597de84b39c9710528e5aec9f64/1040g2sg31gdp4n8sja1g5p1rvta3b6hicujsg7g!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/a2e6961119307014cccece9a63c930ad/1040g2sg31gdp4n8sja5g5p1rvta3b6hiimqfup0!nd_dft_wlteh_jpg_3",
    "tag_list": "AI,人工智能,AI销售,AI获客,AI销冠,AI工具,销售",
    "last_modify_ts": "1757396844646",
    "note_url": "https://www.xiaohongshu.com/explore/68022413000000001d001ffd?xsec_token=ABXrZyvxTkVCUR4eKmPzPp07hJAJnl110cQuCQe7esDcE=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "67a9b2f70000000018011465",
    "type": "normal",
    "title": "替你们试过了，deepseek写的文案真能出爆款",
    "desc": "烘焙店没人来，三天爆单500人，就靠这个黑科技，不会拍视频，没团队，今天教你自己用AI做出爆款。\n\t\n第一步，用DeepSeek输入这段话，我是一名烘焙店店主，想在某音/某书发一条同城到店的引流视频。但我是一名小白，请你帮我写一条300字的同城到店文案，需要结合某音/某书短视频爆款逻辑，并且要求说人话，等待DeepSeek生成文案。\n\t\n第二步，复制deept的文案，打开剪映的AI成片，把文案粘贴进去，选择使用本地素材。\n\t\n最后把我们拍的视频填进去就可以了。\n\t\n如果你还不会，我这里整理了一份AI落地的实测资料，⬇️⬇️⬇️暗号【111】直接发！\n\t\n﻿#deepseek获客[话题]#﻿ ﻿#seepseek[话题]#﻿ ﻿#AI[话题]#﻿ ﻿#AI获客[话题]#﻿ ﻿#咖啡与晓晓[话题]#﻿ ﻿#咖啡与晓晓[话题]#﻿ ﻿#烘焙创业[话题]#﻿ ﻿#爆款文案[话题]#﻿",
    "video_url": "",
    "time": "1739174647000",
    "last_update_time": "1739174647000",
    "user_id": "57d64fe750c4b45215e0762a",
    "nickname": "晓晓与咖啡",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/bbd2276b-f5a2-37a1-918d-cc36e8d85710",
    "liked_count": "56",
    "collected_count": "34",
    "comment_count": "21",
    "share_count": "7",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/b7eb23b125fab61bfc451f06a37a1998/spectrum/1040g34o31dnd6s5pgc0g48mhqn7uetha71o9v2g!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/2e73969558b2a0080dbbe12287528ee9/spectrum/1040g34o31dnd718ah20g48mhqn7uethanrnpof8!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/f1940d781ee6dcc97c8ba10d7d97909a/spectrum/1040g34o31dnd737ggc0g48mhqn7uethas2iju08!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/59c31bdfea9572cecea9d1be36cdf124/spectrum/1040g0k031dnd7a38087048mhqn7uethajllaskg!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/ac2d110b407c9d05f0c363cf82165402/spectrum/1040g34o31dnd7bum0e0g48mhqn7uethah1he718!nd_dft_wlteh_jpg_3",
    "tag_list": "deepseek获客,seepseek,AI,AI获客,咖啡与晓晓,咖啡与晓晓,烘焙创业,爆款文案",
    "last_modify_ts": "1757396844648",
    "note_url": "https://www.xiaohongshu.com/explore/67a9b2f70000000018011465?xsec_token=AB8vD_KsN6y29adUb3WdL-u92V6rr6R5g2WpPb7ewwNXg=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "6776368a000000000b0153a9",
    "type": "normal",
    "title": "\"AI助阵，营销新潮流！精准获客攻略\"\"\"",
    "desc": "🌊🔮 随着数字化的巨浪翻涌，我们立足时代的拐点，AI技术的融合与创新，正引领着客户吸引策略的新潮流。这是一个闪耀着无限可能的未来，正待我们探索！✨\n🔍️ 智能洞察，精准锁定\n跳出传统营销的盲目摸索，AI以其精准的数据分析能力，洞察用户行为，绘制出栩栩如生的客户画像。如此，我们的营销不再是大海捞针，而是精准狙击，心有灵犀。\n🚀️ AI引领的自动化营销\nAI技术赋予营销自动化新的速度与力量，从内容创意到客户互动，全方位提升效率，同时确保每一位客户享受到连贯而个性化的服务体验。\n📈️ 数据洞察，未来策略\n数据是新时代的石油，AI正是我们开采和提炼的利器。它分析历史与实时数据，预测市场动向，引领企业做出有远见的决策，先一步抢占市场高地。\n🌐️ 全渠道智能交互\n在AI的助力下，线上线下渠道得以无缝对接，打造一致而个性化的客户旅程。每一次互动，都是我们与客户建立信任和关系的珍贵时刻。\n🛠️ 持续进化，不断优化\nAI系统拥有自我进化的神奇力量，它通过不断学习市场反馈和客户互动，自我调整策略，让获客效果随着时间的沉淀不断精进。\n📢️ 在这个数据赋能的时代，AI技术是企业增长的加速器，它不仅提升了营销的精准度和效率，更为企业注入了源源不断的增长动力。\n🚀️ 加入AI赋能的行列，让您的企业在激烈的市场竞争中一骑绝尘，引领未来！  #行业[话题]#  #引流获客[话题]#  #ai[话题]#  #ai营销[话题]#  #流量获客[话题]#  #创业[话题]#  #商业思维[话题]#  #商业[话题]#  #风口[话题]#  #获客[话题]#",
    "video_url": "",
    "time": "1735804841000",
    "last_update_time": "1735800458000",
    "user_id": "5b7033b2157d6f0001966813",
    "nickname": "AI小z",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/67763296066e83cb9bfbf5f5.jpg",
    "liked_count": "84",
    "collected_count": "29",
    "comment_count": "24",
    "share_count": "8",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/66a9ea9c2f0b0bf8461f02e0b9567b34/spectrum/1040g34o31c54cs610s0g4ad2utpr4q0jupb9gjg!nd_dft_wlteh_jpg_3",
    "tag_list": "行业,引流获客,ai,ai营销,流量获客,创业,商业思维,商业,风口,获客",
    "last_modify_ts": "1757396844649",
    "note_url": "https://www.xiaohongshu.com/explore/6776368a000000000b0153a9?xsec_token=ABmHdisHUChwzZEx9OvX5U9qUS9Yt9yIpxfo5G_zeO56Y=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "67e0c301000000000e004fcd",
    "type": "normal",
    "title": "求Ai拓客系统",
    "desc": "#实体店[话题]# #实体店铺[话题]#",
    "video_url": "",
    "time": "1742783233000",
    "last_update_time": "1742783333000",
    "user_id": "6220cca7000000001000fb09",
    "nickname": "小红薯6220DF87",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/2b4cefbb01e0a0824f5c173dde7f98b2.jpg",
    "liked_count": "80",
    "collected_count": "17",
    "comment_count": "91",
    "share_count": "10",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/35c60e78b7d5bb260166931e73b53478/1040g2sg31fd60trn6id05oh0pijk1uo9742ua5g!nd_dft_wlteh_jpg_3",
    "tag_list": "实体店,实体店铺",
    "last_modify_ts": "1757396844651",
    "note_url": "https://www.xiaohongshu.com/explore/67e0c301000000000e004fcd?xsec_token=ABvFIpXVrkfbbF9-ubM7Et5ZriarZvLE15So4xa0Nziaw=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "67e52ad6000000001c03c981",
    "type": "normal",
    "title": "入职中医馆，当月获客到店300人 什么水平",
    "desc": "我是一名00后运营，只做白号矩阵，2月入职一家中医馆，没有三甲认证，一个月用短视频+ai矩阵化运营，带了三个人，当月就给医馆引流到店300多人，是什么水平，2025运营ai技术加白号矩阵，也太爽了吧，完成可以提高人效，比起以前我带10个人才完成300+。现在只需要4个人。\n分享一些小技巧  想了解跟多矩阵私我\n1⃣ 绝对化用语❌，医疗效果别“封神”\n⚠ 严禁使用“最先进”“全国第一”“100%有效”等极限词或无法验证的表述。\n正确姿势👉 用客观数据替代，如“根据临床数据，某项目满意度达XX%”\n避雷案例：某医美机构因宣称“全球顶尖技术”被罚款10\n2⃣ 治愈率、效果保证？达咩！\n🚫 “当天见效”“彻底根治”“零复发”等承诺性词汇均属违规！\n替代方案👉 可描述为“部分用户反馈改善明显”，并强调个体差异\n真实案例：某减肥产品笔记因“7天瘦10斤”被限流并下架\n3⃣ 患者形象禁止“现身说法”\n📵 禁止使用患者术前术后对比图、医生/专家名义推荐！\n合规操作👉 改用科普动画或示意图，标注“效果因人而异”\n踩坑警示：上海某医美机构因发布患者治疗视频被立案调查\n4⃣ 迷信词汇别碰，科学才是硬道理\n🔮 “招财进宝”“逢凶化吉”等玄学词汇易触发审核！\n替代方向👉 聚焦科学原理，如“通过XX技术改善皮肤代谢\n违规示例：某养生账号因宣传“风水调理健康”被封禁\n5⃣ 化妆品≠医疗，界限要分清\n💄 普通化妆品不可宣称“消炎”“抗敏”“生发”等医疗功效。\n合规话术👉 调整为“温和清洁”“保湿舒缓”等基础功能描述\n典型违规：某洗发水因暗示“防脱发”被判定虚假宣传\n6⃣ 限时促销？医疗广告不玩“紧迫感”\n⏰ “最后一天优惠”“随时涨价”等诱导性话术属高危禁区！\n安全方案👉 注明具体活动时间，如“3月1日-3月31日特惠”\n平台动态：小红书专项治理已封禁超5万违规账号，严打营销话术#传承中医国粹[话题]# #实体店线上运营[话题]# #实体行业破局[话题]# #深圳[话题]#",
    "video_url": "",
    "time": "1743071958000",
    "last_update_time": "1743071958000",
    "user_id": "676cfda30000000015004694",
    "nickname": "00后运营只做白号矩阵",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31fhf8kr17e6g5prcvmhl8hkk9pa3utg",
    "liked_count": "57",
    "collected_count": "45",
    "comment_count": "16",
    "share_count": "3",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/8c33516a5dded5537f225b42658dd27b/1040g2sg31fhfn50lnidg5prcvmhl8hkkvmmtp00!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/9168b05f47faae860a409d4e24366e69/1040g2sg31fhfn50lnicg5prcvmhl8hkk7amfg30!nd_dft_wlteh_jpg_3",
    "tag_list": "传承中医国粹,实体店线上运营,实体行业破局,深圳",
    "last_modify_ts": "1757396844652",
    "note_url": "https://www.xiaohongshu.com/explore/67e52ad6000000001c03c981?xsec_token=ABNHbJU6g6Ek1BoTY_Gtgnpn_Ah15i0nT8aL5W6iqmWT4=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "684a9e360000000022035ab4",
    "type": "normal",
    "title": "房产中介别硬扛！结合DeepSeek3个月卖20套",
    "desc": "房产中介别傻发房源了！3个月开单20套，流量密码在这！[黄色心形R]\n\t\n还在朋友圈刷屏发房源？小红书账号没人看？别卷了！教你一套真实管用的打法，3个月开单20套的核心，就靠这3招做流量：\n\t\n[一R]1. 账号不是中介，是“房产问题解决专家”\n\t\n别叫XX房产！ 改名叫“XX教你买对房”或“XX避坑指南”。定位清晰：你不是卖房的，是帮人选好房、避大坑的！\n\t\nAI帮你找痛点： 用AI工具（比如deepseek）搜“买房最担心什么”、“选房最怕什么”，直接抓出本地人最关心的“烂尾楼”、“学qu划分”、“坎 价技巧”这些关键词，围绕它们做内容！\n\t\n[二R]2. 内容不推房源，讲“真话”和“干货”\n\t\nAI批量生成选题库： 让AI根据你所在城市+关键词（如“刚需”、“置换”、“学区”），生成100个具体问题选题。比如“XX区300万预算，买A还是B盘？”、“XX楼盘样板间vs实景，差别有多大？”\n\t\n内容核心：讲人话，说内幕！\n\t\n避坑指南： “这3类房再便宜也别碰！”、“中介绝不会告诉你的5个看房细节”。\n\t\n硬核干货： “手把手教你砍价10万话术”、“贷款选等额本息/本金？算完这笔账就懂”。\n\t\n本地实探： “暴走XX板块，看完10个盘，我只推荐这2个！”（用手机拍真实视频/图片）。\n\t\nAI是文案小助手，不是代笔！ 用AI生成初稿框架或灵感，但必须加入你的真实案例、本地信息和犀利观点！ 把AI的“官话”改成你的“大实话”，才有灵魂！\n\t\n[三R]3. 流量变客户，靠“钩子”和“私域”\n\t\n内容里埋钩子： 干货讲一半！比如“教你3招砍价技巧，第3招最狠（想知道的频论区扣1）”。或者“XX小区优缺点清单（踢踢岭）”。\n\t\n小红书煮页留入口： 简介写清楚“专注XX区域房产X年，免费咨询”，引导思 信。\n\t\n核心心法： 流量不是等来的！用AI帮你高效找方向、出草稿，但内容必须是你真实的经验+本地化的信息+利他的价值。持续输出“有用”的内容，建立信任，客户自然来找你！别懒，干就完了！\n\t\n[彩虹R]授人以鱼不如授人以渔，让您从依耐到独立～ 我准备了《房产运营百宝书SOP》，不光有拆解0-1的方法，还有全域打粉的模板，如果你在做自媒体遇到了瓶颈，又想在自媒体分一杯羹的话，不妨看看~此文与﻿@房次元﻿共创﻿#房产获客[话题]#﻿ ﻿#朗沁传媒陪跑[话题]#﻿ ﻿#新媒体卖房[话题]#﻿ #房产中介[话题]# #AI获客[话题]#",
    "video_url": "",
    "time": "1749725105000",
    "last_update_time": "1750737539000",
    "user_id": "67288860000000001c01ae64",
    "nickname": "懂小妹咨询",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/1040g2jo31h4jscc0k26g5pp8h1g73bj4q5n8l48",
    "liked_count": "46",
    "collected_count": "51",
    "comment_count": "44",
    "share_count": "12",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/e7c8a9100b53b902353345e896b9bb95/1040g00831j3muk2e1i6g5pp8h1g73bj43ebpoa0!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/b8629dd93de55c367e5919f8a69257c9/1040g00831j3muk3e0o6g5pp8h1g73bj4gcmaikg!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/10500011959cd8c09dd5ccac19cac481/1040g00831j3muk39gm6g5pp8h1g73bj4q4c9kgo!nd_dft_wgth_jpg_3",
    "tag_list": "房产获客,朗沁传媒陪跑,新媒体卖房,房产中介,AI获客",
    "last_modify_ts": "1757396844653",
    "note_url": "https://www.xiaohongshu.com/explore/684a9e360000000022035ab4?xsec_token=AB7D5plvAISO_6AiY_lU7Cee4PxOjqkLk7DM8PGH04Vgs=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "6763ea28000000000900d5a9",
    "type": "normal",
    "title": "利用AI＋获客：如何一天1000+精准客户",
    "desc": "用AI高效赋能，实现一天获客10000+精准客户，分享以下实战经验！\n\t\n✅人人都能操作，核心总结为三大步骤：\n1. 选择合适的yin流渠道\n你的客户在哪儿，你就去哪儿！\n- 参考同行：观察你的竞争对手在哪些平台引流，直接跟进。这些渠道已经被验证有效，你只需模仿和优化即可。\n- 不用怕竞争：竞争多说明市场成熟，同行铺好的路你可以复制，哪怕是边角料也能让你获利。\n-\n2. 精准定位客户需求\n很多人以为自己的产品客户需求显而易见，其实并非如此。\n- 深挖需求：举个例子，你卖护肤品，客户不只是“需要护肤”，他们可能是油性肌、干性肌、敏感肌等不同肤质，需求差异巨大。\n- 展现专业性：如果你不能清晰判断客户的具体需求，对方可能觉得你不专业，从而失去信任。精准需求匹配是成交的关键。\n-\n3. 掌握营销技巧，提升转化率\n引流只是第一步，最终能不能成交，关键在于你的营销能力。\n- 内容优化：朋友圈、社交平台内容需要多样化，除了产品，还要展示客户反馈、场景应用等。例如，服装类卖家增加穿搭建议、客户反馈后，一周成交量实现突破。\n- 懂客户心理：所有营销的本质就是“懂人心”。理解客户需求、痛点和心理，你的产品才会让人觉得“刚需”，成交也会变得自然。\n#引流[话题]# #引流[话题]##经验分享[话题]##干货分享[话题]#  #引流[话题]##获客[话题]# #打造个人IP[话题]#  #引流[话题]##那点事[话题]##小红书引流[话题]#",
    "video_url": "",
    "time": "1734601256000",
    "last_update_time": "1734601257000",
    "user_id": "66ceb81e000000001d0307b8",
    "nickname": "艾文AI流量获客",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/ca24a235-8d52-3182-b9ea-c1e1fd891c3a",
    "liked_count": "40",
    "collected_count": "18",
    "comment_count": "23",
    "share_count": "5",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/a7aaec4e89478d108797bbca6dfb7ae5/1040g2sg31bj8ig2bgm005pmen0f7e1tokvh7oe8!nd_dft_wlteh_jpg_3",
    "tag_list": "引流,经验分享,干货分享,获客,打造个人IP,那点事,小红书引流",
    "last_modify_ts": "1757396844655",
    "note_url": "https://www.xiaohongshu.com/explore/6763ea28000000000900d5a9?xsec_token=ABdYPkBSTi2H-CjfDeOW2Swa1RDlD556W7v0QIfJXfyto=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "67dfeeb1000000001d01d8ac",
    "type": "normal",
    "title": "ai自动获客工具是真的吗？",
    "desc": "求科普#真的假的🤨[话题]# #引流拓客[话题]# #AI工具[话题]# #工具[话题]# #私域引流[话题]#",
    "video_url": "",
    "time": "1742728881000",
    "last_update_time": "1742728882000",
    "user_id": "667f66420000000007006587",
    "nickname": "小红薯cc",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/645b7efe1fc3de4c930effac.jpg",
    "liked_count": "39",
    "collected_count": "15",
    "comment_count": "67",
    "share_count": "2",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/af59bc2291f03584ef5e4db58ec4dcf7/1040g2sg31fcc479t6ecg5pjvcp11opc79tbvtdo!nd_dft_wlteh_webp_3",
    "tag_list": "真的假的🤨,引流拓客,AI工具,工具,私域引流",
    "last_modify_ts": "1757396844656",
    "note_url": "https://www.xiaohongshu.com/explore/67dfeeb1000000001d01d8ac?xsec_token=ABX0NwhJzPqoc7wfr7iF8e6x_ouh5XwBynsTP1B6UkXEM=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "67e7b13d000000000b0145cd",
    "type": "video",
    "title": "设计师IP+AI获客实操课，极限30小时~！",
    "desc": "3天3夜线下课，打通设计师从定位、账号搭建、选题、脚本、拍摄、剪辑、投放、直播，自媒体IP获客一条龙！现场听课+实操+点评，不藏私，真心交付！99.9%好评！\n﻿#设计师[话题]#﻿ ﻿#设计师做自媒体[话题]#﻿ ﻿#设计师做小红书[话题]#﻿ ﻿#设计师IP打造[话题]#﻿ ﻿#阿萌设计师IP获客[话题]#﻿ ﻿#设计师自媒体IP[话题]#﻿﻿",
    "video_url": "http://sns-video-bd.xhscdn.com/spectrum/1040g0jg31fjud7of7e0049eg4tqg7a7rea9c1fg",
    "time": "1743237437000",
    "last_update_time": "1745322320000",
    "user_id": "5900b5035e87e7560a08a8fb",
    "nickname": "阿萌-设计师IP获客（星标）",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/672f3d2fbcf9fe1bd9b18350.jpg",
    "liked_count": "23",
    "collected_count": "27",
    "comment_count": "20",
    "share_count": "13",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/5426bc412e2b9df379333c2bc10b8276/spectrum/1040g34o31fjueh787g0g49eg4tqg7a7r6sljnjo!nd_dft_wlteh_jpg_3",
    "tag_list": "设计师,设计师做自媒体,设计师做小红书,设计师IP打造,阿萌设计师IP获客,设计师自媒体IP",
    "last_modify_ts": "1757396851902",
    "note_url": "https://www.xiaohongshu.com/explore/67e7b13d000000000b0145cd?xsec_token=ABD9K-GFspoSWS7rqx6oEkCCXCvI1Gq-JBSQdh5zFRvOE=&xsec_source=None",
    "source_keyword": "ai获客"
  },
  {
    "note_id": "6822fdf7000000001200584e",
    "type": "normal",
    "title": "小红书私信获客工具，自动发送名片留资卡！",
    "desc": "[向右R]企业号和kos号的老板们注意啦，还不知道这个私信自动获客工具？那你可就out了！\n聚光咨询消息多？\n人工客服效率低？\n获客成本高？\n你只需要这个小红书官方授权的私信获客工具——米多客AI，就可以实现全流程自动获客！\n🔥米多客AI真的太太太太好用啦！\n[派对R]AI自动发送发送名片卡、留资卡，解放人工双手！\n[飞吻R]24h*7全天候在线回复，不错过任何一条宝贵线索\n[害羞R]AI化身营销专家，细分行业对话技能max\n[大笑R]批量二次私信发送，激活沉默客户\n[庆祝R][庆祝R]现在评论区回复【试用】后私戳我，可以获取【体验卡】，赶快开启你的小红书自动获客时代吧~~~[庆祝R]\n﻿#米多客[话题]#﻿ ﻿#AI客服[话题]#﻿ ﻿#引流获客[话题]#﻿ ﻿#聚光[话题]#﻿ ﻿#聚光投放[话题]#﻿ ﻿#小红书运营[话题]#﻿ ﻿#私信获客[话题]#﻿ ﻿#客服[话题]#﻿ ﻿#小红书新规[话题]#﻿﻿",
    "video_url": "",
    "time": "1747123703000",
    "last_update_time": "1747125539000",
    "user_id": "65433ec70000000030033a8e",
    "nickname": "大连米云科技有限公司",
    "avatar": "https://sns-avatar-qc.xhscdn.com/avatar/6588e1c9286aab000155ddc9.jpg",
    "liked_count": "20",
    "collected_count": "9",
    "comment_count": "19",
    "share_count": "11",
    "sticky": "False",
    "ip_location": "",
    "image_list": "http://sns-webpic-qc.xhscdn.com/202509091347/febd0821af22ff91b722a04f6e89e7cb/spectrum/1040g0k031hdra7gc421g5pa37r3s6ekel7uqb70!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/da7883e79b3dc2e2a7922e5c49635bd7/spectrum/1040g0k031hdrbftu427g5pa37r3s6ekensfpuho!nd_dft_wlteh_jpg_3,http://sns-webpic-qc.xhscdn.com/202509091347/ebee48c6ae0feca6b7a1cd377f91fdf6/spectrum/1040g0k031hdrbftu42705pa37r3s6ekeb9ubdj0!nd_dft_wlteh_jpg_3",
    "tag_list": "米多客,AI客服,引流获客,聚光,聚光投放,小红书运营,私信获客,客服,小红书新规",
    "last_modify_ts": "1757396851903",
    "note_url": "https://www.xiaohongshu.com/explore/6822fdf7000000001200584e?xsec_token=ABSX7UGrtAGo1nxrN5PHMghbTbcCpf9FMSWwhmNCBZ3Jk=&xsec_source=None",
    "source_keyword": "ai获客"
  }
]

    cfg = CESFilterConfig(
        # —— 典型配置 ——
        min_ces=0,
        min_weighted_ces=0,             # demo 为展示结果，设为 0 保证能看到输出
        enable_time_decay=True,
        half_life_hours=48.0,
        recency_days=365,               # 仅看近一年
        allowed_types=["normal"],
        required_keywords=None,
        exclude_keywords=None,
        top_k=None,                     # 不截断；如需只看前 50 条：top_k=50
        yield_every=1000,
    )

    result = await score_and_filter_notes(notes, cfg)

    # —— 打印最终结果（按加权 CES 降序）——
    print(f"共保留 {len(result)} 条笔记：")
    for r in result:
        print(
            f"[note_id={r.get('note_id')}] "
            f"CES={r.get('ces'):.0f} | W_CES={r.get('weighted_ces'):.0f} | "
            f"type={r.get('type')} | title={r.get('title')}"
        )


if __name__ == "__main__":
    # 直接运行本文件以快速自测
    asyncio.run(demo())
