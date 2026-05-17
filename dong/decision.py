"""
冬 · AI决策模块
- decide_withdraw()  撤回决策（"她会不会后悔"心理模拟）
- 调用分析API，超时3.0s，失败返回None → 调用方降级到硬逻辑
"""
import json
import re
from datetime import datetime

import requests

from .config import _get_cfg
from .log import log


_WITHDRAW_PROMPT = """你是冬，一个20岁女生。你傲娇、嘴硬、不好意思直接表达感情。
你对不同人的态度不一样——对159（你的主号/最亲近的人）更容易后悔暴露真实感受，对333（普通朋友）没那么在意。

[此刻状态]
情绪: {mood}/100 | 疲劳: {fatigue}/100 | 亲密度: level {intimacy_level}
深夜: {late_night} | 对方: {identity}

[当前场景]
你刚回复了这句话："{reply_text}"
最近的对话：
{recent_msgs}

[决策任务]
如果你是她——你刚把上面这句话发出去了。在角色里，问问自己：发出去之后你会不会后悔？

后悔的判断依据：
- 这句话是不是太肉麻、太暴露真实感情？
- 是不是和你平时嘴硬的人设矛盾？
- 对159，你平时就会更在意自己说了什么，所以更容易后悔暴露真实感受
- 对其他人，你通常不太在乎

如果不会后悔 → 回答 keep
如果会后悔 → 回答 withdraw，并选择一种掩饰策略：
- deny：嘴硬否认（"没说什么""你看错了"）
- deflect：岔开话题（给出具体的岔开话题文本）
- ignore：假装什么都没发生
- act_cute：撒娇糊弄（"手滑了嘛"）

action 只能是 "keep" 或 "withdraw"。
after_withdraw 只能是 "deny"、"deflect"、"ignore"、"act_cute" 中的一个。

请只输出JSON，不要加任何解释、引号或代码块标记。

不撤回示例：
{{"action": "keep", "reason": "这句话没什么，符合平时说话方式", "after_withdraw": "", "follow_up": ""}}

撤回示例：
{{"action": "withdraw", "reason": "太肉麻了，不符合人设", "after_withdraw": "deflect", "follow_up": "岔开话题的一句日常关心或提问"}}
注意：follow_up 必须是你自己生成的、符合当前对话语境的自然岔开话题文本，绝对不能重复使用固定的句子。"""


def _parse_decision_response(text):
    """从API返回中提取JSON决策"""
    if not text:
        return None
    text = text.strip()

    # 处理 ```json ... ``` 包裹
    m = re.search(r'```(?:json)?\s*\n?([\s\S]*?)\n?\s*```', text)
    if m:
        text = m.group(1).strip()

    # 提取最外层 {}
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        log(f"  决策解析失败: 未找到JSON对象")
        return None
    text = m.group(0)

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        log(f"  决策JSON解析失败: {e}")
        return None

    # 验证必填字段
    if "action" not in data:
        log(f"  决策缺少action字段: {data}")
        return None

    action = data["action"]
    if action not in ("keep", "withdraw"):
        log(f"  决策action非法: {action}，默认视为keep")
        return {"action": "keep", "reason": f"非法action已降级", "after_withdraw": "", "follow_up": ""}

    if action == "keep":
        return data

    # withdraw 需要验证 after_withdraw
    strategy = data.get("after_withdraw", "ignore")
    if strategy not in ("deny", "deflect", "ignore", "act_cute"):
        log(f"  决策after_withdraw非法: {strategy}，降级为ignore")
        data["after_withdraw"] = "ignore"

    return data


def decide_withdraw(reply_text, uid, context):
    """调用分析API判断她是否会后悔发出这句话。返回决策dict或None。"""
    try:
        cfg = _get_cfg("chat")  # 用主聊天池，避免和摘要争analysis池

        identity = "主号159" if context.get("is_master") else "普通朋友"
        late_night = "是" if context.get("is_late_night") else "否"

        prompt = _WITHDRAW_PROMPT.format(
            mood=context.get("mood", 60),
            fatigue=context.get("fatigue", 50),
            intimacy_level=context.get("intimacy_level", 1),
            late_night=late_night,
            identity=identity,
            reply_text=reply_text[:300],
            recent_msgs=context.get("recent_msgs", "无")[:500],
        )

        messages = [
            {"role": "system", "content": "你是冬的内心决策者。只输出JSON，不输出其他内容。"},
            {"role": "user", "content": prompt},
        ]

        r = requests.post(
            f"{cfg.api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": cfg.model,
                "temperature": 0.3,
                "max_tokens": 200,
                "messages": messages
            },
            timeout=3.0
        )

        if r.status_code != 200:
            log(f"  决策API失败: {r.status_code}")
            return None

        resp = r.json()
        if "choices" not in resp or not resp["choices"]:
            log(f"  决策API无choices")
            return None

        content = resp["choices"][0]["message"]["content"]
        decision = _parse_decision_response(content)

        if decision:
            log(f"  决策: {decision.get('action')} | {decision.get('reason', '')[:40]}")
        return decision

    except requests.Timeout:
        log(f"  决策超时(3.0s)")
        return None
    except Exception as e:
        log(f"  决策异常: {e}")
        return None
