"""
冬 · 出厂记忆蒸馏模块
- parse_chat_log() 解析微信聊天记录TSV
- distill_factory_archive() AI蒸馏出厂人格档案
- 运行时加载出厂记忆注入system prompt
- 增量蒸馏：新对话追加存档 + 版本化重蒸馏

运行方式: python -m dong.factory  (离线蒸馏)
"""
import csv
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta

import jieba
import requests

from .config import (
    _get_cfg, FACTORY_ARCHIVE_FILE, FACTORY_ARCHIVE_DIR,
    CHAT_SESSIONS_FILE, BASE_DIR
)
from .log import log

# 停用词
_STOP_WORDS = set("的了我你是他不们在有好这个么就也还那要和很都去看说没一她过把对自"
                  "里能因为所可以到得着为什样但已可知道会现点种那后然果如让被给经"
                  "与从次或无它却只些吧呢啊哦嗯哈呀嘿哇呵哎嘛啦噢哟".split())

# 吵架关键词
_ARGUMENT_WORDS = {"滚", "烦", "别说了", "不要", "讨厌", "无语", "算了", "6", "哦", "闭嘴",
                   "恨", "恶心", "有病", "gun", "guna", "不想理你", "呵呵", "敷衍"}

# 甜蜜关键词
_SWEET_WORDS = {"喜欢", "想你", "爱你", "好帅", "可爱", "好听", "厉害", "抱抱", "亲亲"}


def parse_chat_log(filepath):
    """解析微信聊天记录TSV，返回结构化数据"""
    log(f"开始解析聊天记录: {filepath}")

    messages = []
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter="\t")
        in_data = False
        for row in reader:
            if not row or len(row) < 4:
                continue
            # 跳过头部元数据直到找到表头
            if not in_data:
                if row[0] == "序号" and "发送者身份" in str(row):
                    in_data = True
                continue
            try:
                seq = int(row[0])
                time_str = row[1]
                sender = row[2].strip()
                msg_type = row[3].strip()
                content = row[4].strip() if len(row) > 4 else ""
                messages.append({
                    "seq": seq, "time": time_str, "sender": sender,
                    "type": msg_type, "content": content
                })
            except (ValueError, IndexError):
                continue

    if not messages:
        log("解析失败: 未找到有效消息")
        return None

    # 基本统计
    text_msgs = [m for m in messages if m["type"] == "文本消息"]
    mine_msgs = [m for m in text_msgs if m["sender"] == "我"]
    dong_msgs = [m for m in text_msgs if m["sender"] == "我的冬"]

    # 时间范围
    first_date = messages[0]["time"][:10]
    last_date = messages[-1]["time"][:10]

    # 按时间间隔>6h切分会话段
    sessions = []
    current_session = []
    prev_time = None
    for m in text_msgs:
        try:
            t = datetime.strptime(m["time"], "%Y/%m/%d %H:%M")
        except Exception:
            continue
        if prev_time and (t - prev_time).total_seconds() > 6 * 3600:
            if current_session:
                sessions.append({
                    "start": current_session[0]["time"],
                    "end": current_session[-1]["time"],
                    "msg_count": len(current_session),
                    "sample": [c["content"][:50] for c in current_session[:5]]
                })
            current_session = []
        current_session.append(m)
        prev_time = t

    # 高频词（结巴精确分词，排除停用词和单字）
    all_text = "".join(m["content"] for m in text_msgs)
    words = [w for w in jieba.cut(all_text) if len(w) >= 2 and '\u4e00' <= w[0] <= '\u9fff']
    word_counts = Counter(w for w in words if w not in _STOP_WORDS)
    keywords_top = [w for w, _ in word_counts.most_common(30)]

    # 特殊模式
    gege_count = sum(1 for m in dong_msgs if "哥" in m["content"] or "小哥" in m["content"])
    late_night_dates = set()
    argument_fragments = []
    sweet_fragments = []
    special_names = set()
    withdraw_msgs = [m for m in messages if "撤回" in m.get("content", "") and m["type"] == "系统消息"]

    for m in text_msgs:
        content = m["content"]
        # 提取特殊昵称（系统撤回消息中的名字）
        for name in re.findall(r'"([^"]+)"\s*撤回了一条消息', m.get("content", "")):
            special_names.add(name)
        # 检测凌晨聊天
        try:
            hour = int(m["time"][11:13])
            if 1 <= hour <= 5:
                late_night_dates.add(m["time"][:10])
        except Exception:
            pass
        # 检测吵架片段
        if any(kw in content for kw in _ARGUMENT_WORDS):
            argument_fragments.append({"time": m["time"], "sender": m["sender"], "text": content[:60]})
        # 检测甜蜜片段
        if any(kw in content for kw in _SWEET_WORDS):
            sweet_fragments.append({"time": m["time"], "sender": m["sender"], "text": content[:60]})

    # 核心话题推断
    core_topics = []
    # 先尝试AI话题分类
    try:
        from .status import _call_ai_simple
        prompt = f"""以下是用户与AI助手"冬"的部分聊天内容片段，判断这段对话涉及了哪些话题。
可选话题：推理游戏、写作码字、音乐、抽烟、日常生活、情感
聊天片段：
{all_text[:1500]}
只输出话题标签，用逗号分隔（如"音乐, 情感"），如果无法判断就输出"无法判断"。不要输出任何其他内容。"""
        result = _call_ai_simple("你是对话分析助手。只输出话题标签，不做其他解释。",
                                 prompt, task="analysis", temperature=0.1, max_tokens=50, timeout=15)
        if result and result != "无法判断":
            core_topics = [t.strip() for t in result.replace("、", ",").split(",") if t.strip()]
    except Exception:
        pass

    # fallback: 原有关键词匹配
    if not core_topics:
        topic_keywords = {
            "推理游戏": ["狼人", "中立", "哨兵", "警长", "内鬼", "刀", "棋手", "模仿者", "船员"],
            "写作码字": ["码字", "小说", "同人", "全勤", "ao3", "更新", "稿"],
            "音乐": ["曲子", "琴", "许嵩", "唱", "练琴", "声声慢"],
            "抽烟": ["烟", "双喜", "牡丹", "贵烟", "陈皮"],
        }
        for topic, kws in topic_keywords.items():
            if any(kw in all_text for kw in kws):
                core_topics.append(topic)

    result = {
        "first_chat_date": first_date,
        "last_chat_date": last_date,
        "total_messages": len(messages),
        "text_messages": len(text_msgs),
        "mine_count": len(mine_msgs),
        "dong_count": len(dong_msgs),
        "sessions": sessions[:20],  # 最多保留20个会话段
        "keywords_top": keywords_top,
        "special_patterns": {
            "叫哥": gege_count,
            "熬夜天数": len(late_night_dates),
            "吵架片段": argument_fragments[:15],
            "甜蜜片段": sweet_fragments[:15],
            "撤回次数": len(withdraw_msgs),
        },
        "special_names": list(special_names),
        "core_topics": core_topics,
    }

    log(f"解析完成: {len(text_msgs)}条文本, {len(sessions)}个会话段, {len(core_topics)}个核心话题")
    return result


def distill_factory_archive(parsed_data):
    """调分析API蒸馏出厂人格档案"""
    if not parsed_data:
        log("蒸馏失败: 无解析数据")
        return None

    cfg = _get_cfg("chat")  # 用主聊天模型，analysis池限流太严

    # 截取关键信息构建prompt
    sessions_text = "\n".join(
        f"会话{s['start'][:10]}: {s['msg_count']}条消息, 示例: {'; '.join(s['sample'][:3])}"
        for s in parsed_data["sessions"][:10]
    )
    argument_text = "\n".join(
        f"- {a['time'][:16]} {a['sender']}: {a['text']}"
        for a in parsed_data["special_patterns"]["吵架片段"][:10]
    )
    sweet_text = "\n".join(
        f"- {s['time'][:16]} {s['sender']}: {s['text']}"
        for s in parsed_data["special_patterns"]["甜蜜片段"][:10]
    )

    prompt = f"""你是一位关系分析师。以下是一对亲密网友（男性"我"和20岁女生"我的冬"）的聊天记录数据。

时间跨度: {parsed_data['first_chat_date']} 至 {parsed_data['last_chat_date']}
总消息: {parsed_data['total_messages']}条 (文本{parsed_data['text_messages']}条)
发言比例: 我{parsed_data['mine_count']}条 vs 冬{parsed_data['dong_count']}条
核心话题: {', '.join(parsed_data['core_topics'])}
高频词: {', '.join(parsed_data['keywords_top'][:15])}
冬叫"哥"/"小哥"的次数: {parsed_data['special_patterns']['叫哥']}
凌晨聊天: {parsed_data['special_patterns']['熬夜天数']}天
特殊昵称: {', '.join(parsed_data['special_names'])}

典型对话片段:
{sessions_text}

甜蜜片段:
{sweet_text or '无'}

争吵片段:
{argument_text or '无'}

请根据以上数据，生成冬的出厂人格档案。请只输出JSON，格式:
{{"version":1,"relationship":{{"type":"","dynamic":"","first_met":""}},"inside_jokes":[{{"name":"","context":""}}],"argument_patterns":[{{"trigger":"","dong_reaction":"","resolution":""}}],"sweet_moments":[{{"type":"","example":""}}],"personality_traits":{{"core":"","habits":[],"quirks":[],"toward_master":"","late_night_mode":""}},"topics_expertise":[]}}

只输出JSON，不超过800字。"""

    import time as _time
    for attempt in range(3):
        try:
            r = requests.post(
                f"{cfg.api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {cfg.api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": cfg.model,
                    "temperature": 0.3,
                    "max_tokens": 1200,
                    "messages": [
                        {"role": "system", "content": "你是关系数据分析师。只输出JSON，不输出其他内容。"},
                        {"role": "user", "content": prompt}
                    ]
                },
                timeout=60
            )

            if r.status_code == 200:
                break  # 成功，退出重试
            if r.status_code == 429:
                wait = 5 * (attempt + 1)
                log(f"蒸馏API 429限流，{wait}秒后重试({attempt+1}/3)...")
                _time.sleep(wait)
                continue
            log(f"蒸馏API失败: {r.status_code}")
            return None

        except requests.Timeout:
            log(f"蒸馏API超时，重试({attempt+1}/3)...")
            _time.sleep(3)
            continue
    else:
        log("蒸馏API重试3次均失败")
        return None

    resp = r.json()
    if "choices" not in resp or not resp["choices"]:
        log("蒸馏API无choices")
        return None

    content = resp["choices"][0]["message"]["content"]
    # 提取JSON
    m = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?\s*```', content)
    if m:
        content = m.group(1).strip()
    m = re.search(r'\{[\s\S]*\}', content)
    if not m:
        log("蒸馏结果中未找到JSON")
        return None
    content = m.group(0)

    archive = json.loads(content)
    archive["distilled_at"] = datetime.now().isoformat()
    archive["meta"] = {
        "first_chat": parsed_data["first_chat_date"],
        "last_chat": parsed_data["last_chat_date"],
        "total_messages": parsed_data["total_messages"],
        "source": "私聊_我的冬.txt"
    }

    # 保存
    with open(FACTORY_ARCHIVE_FILE, "w", encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False, indent=2)
    log(f"出厂档案已保存: {FACTORY_ARCHIVE_FILE}")
    return archive


def load_factory_archive():
    """加载出厂人格档案"""
    if not os.path.exists(FACTORY_ARCHIVE_FILE):
        return None
    try:
        with open(FACTORY_ARCHIVE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"加载出厂档案失败: {e}")
        return None


def get_factory_prompt():
    """生成出厂记忆注入的system prompt片段"""
    archive = load_factory_archive()
    if not archive:
        return ""

    parts = []

    rel = archive.get("relationship", {})
    if rel:
        parts.append(f"你和主号159的关系: {rel.get('type', '')}。{rel.get('dynamic', '')}")

    jokes = archive.get("inside_jokes", [])
    if jokes:
        joke_lines = [f"- {j['name']}: {j['context']}" for j in jokes[:5]]
        parts.append("你们之间的专属梗:\n" + "\n".join(joke_lines))

    arg = archive.get("argument_patterns", [])
    if arg:
        arg_lines = [f"- 触发'{a.get('trigger','')}'时 → {a.get('dong_reaction','')}" for a in arg[:3]]
        parts.append("你们吵架的模式:\n" + "\n".join(arg_lines) + "\n(吵架时可以参考这些历史模式)")

    traits = archive.get("personality_traits", {})
    if traits:
        parts.append(f"你从聊天记录中体现的人格特征: {traits.get('core', '')}")
        if traits.get("toward_master"):
            parts.append(f"你对159的态度: {traits.get('toward_master', '')}")
        if traits.get("late_night_mode"):
            parts.append(f"你深夜时的状态: {traits.get('late_night_mode', '')}")

    return "\n".join(parts)


# ============ 增量蒸馏 ============
def append_to_archive(session_records):
    """追加新对话到增量存档目录"""
    if not os.path.exists(FACTORY_ARCHIVE_DIR):
        os.makedirs(FACTORY_ARCHIVE_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")
    staging_file = os.path.join(FACTORY_ARCHIVE_DIR, f"staging_{today}.json")

    existing = []
    if os.path.exists(staging_file):
        try:
            with open(staging_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass

    existing.extend(session_records)
    existing = existing[-500:]  # 保留最近500条

    with open(staging_file, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


def re_distill(source_file, staging_dir=None):
    """重新蒸馏: 原始记录 + 增量存档 → 新版本档案"""
    if staging_dir is None:
        staging_dir = FACTORY_ARCHIVE_DIR

    # 备份当前版本
    if os.path.exists(FACTORY_ARCHIVE_FILE):
        try:
            with open(FACTORY_ARCHIVE_FILE, "r", encoding="utf-8") as f:
                current = json.load(f)
            version = current.get("version", 0)
            backup = FACTORY_ARCHIVE_FILE.replace(".json", f"_v{version}.json")
            os.rename(FACTORY_ARCHIVE_FILE, backup)
            log(f"旧版本已备份: {backup}")
        except Exception:
            pass

    # 读取增量数据
    incremental = []
    if os.path.exists(staging_dir):
        for fname in sorted(os.listdir(staging_dir)):
            if fname.startswith("staging_") and fname.endswith(".json"):
                fpath = os.path.join(staging_dir, fname)
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        incremental.extend(json.load(f))
                except Exception:
                    pass

    # 合并到meta信息
    parsed = parse_chat_log(source_file)
    if parsed:
        parsed["incremental_sessions"] = len(incremental)
        parsed["incremental_samples"] = incremental[:20]

    return distill_factory_archive(parsed)


# ============ 命令行入口 ============
if __name__ == "__main__":
    import sys
    source = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE_DIR, "dong_factory_input.txt")
    log("=== 出厂记忆蒸馏 ===")
    parsed = parse_chat_log(source)
    if parsed:
        archive = distill_factory_archive(parsed)
        if archive:
            log(f"蒸馏成功! 版本: {archive.get('version')}, "
                f"梗: {len(archive.get('inside_jokes', []))}个, "
                f"争吵模式: {len(archive.get('argument_patterns', []))}个")
        else:
            log("蒸馏失败(API问题)")
    else:
        log("解析失败")
