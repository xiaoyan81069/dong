"""
冬 · 技能记忆 — Agent Loop 操作流程复用
成功执行一次操作流程后存为技能（关键词倒排索引 + 步骤序列），
下次同任务直接回放，跳过规划/VLM/解析。
匹配：jieba 分词 + 集合交集，不用向量数据库。
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
logger = logging.getLogger("dong.core.skill_memory")
__all__ = ["Skill", "SkillMemory", "skill_memory", "replay_skill"]
# ════════════════════════════════════════════
# 分词
# ════════════════════════════════════════════
_STOP_WORDS = {
    "的", "了", "在", "是", "我", "你", "他", "她", "它",
    "这", "那", "把", "被", "让", "给", "从", "到", "用",
    "一", "个", "下", "上", "里", "中", "去", "来", "得",
    "地", "着", "过", "会", "能", "要", "就", "都", "而",
    "及", "与", "或", "但", "不", "也", "还", "又", "再",
    "吗", "吧", "呢", "啊", "呀", "哦", "嗯", "哈", "嘛",
    "什么", "怎么", "哪", "谁", "几", "多", "少",
}
_jieba_available: Optional[bool] = None
def _segment(text: str) -> List[str]:
    """中文分词：优先 jieba，fallback 简单切分。"""
    global _jieba_available
    if _jieba_available is None:
        try:
            import jieba  # noqa
            _jieba_available = True
        except ImportError:
            _jieba_available = False
    if _jieba_available:
        import jieba
        words = list(jieba.cut(text))
    else:
        words = _simple_segment(text)
    # 过滤：去停用词 + 去单字标点 + 去 1 字词（除非在白名单）
    short_whitelist = {"写", "打", "看", "发", "存", "删", "改", "查", "找",
                       "关", "开", "停", "截", "录", "装", "下", "传"}
    return [
        w.strip() for w in words
        if w.strip()
        and w not in _STOP_WORDS
        and (len(w) >= 2 or w in short_whitelist)
    ]
def _simple_segment(text: str) -> List[str]:
    """简单分词：2-4 字滑动窗口 + 关键词匹配。"""
    words = set()
    # 滑动窗口
    for n in (2, 3, 4):
        for i in range(len(text) - n + 1):
            chunk = text[i:i + n]
            if any('\u4e00' <= c <= '\u9fff' for c in chunk):
                words.add(chunk)
    # 英文单词
    import re
    for m in re.finditer(r'[a-zA-Z]{2,}', text):
        words.add(m.group().lower())
    return list(words)
# ════════════════════════════════════════════
# 技能
# ════════════════════════════════════════════
@dataclass
class Skill:
    id: str
    description: str
    keywords: List[str]
    steps: List[str]          # 子任务序列（直接喂给 _execute）
    success_count: int = 1
    last_used: float = 0.0
    created_at: float = 0.0
    @property
    def keyword_set(self) -> Set[str]:
        return set(self.keywords)
# ════════════════════════════════════════════
# 技能记忆系统
# ════════════════════════════════════════════
_SKILL_FILE = Path(__file__).resolve().parent.parent / "dong_skills.json"
_MATCH_THRESHOLD = 0.35      # Jaccard 相似度阈值
_MAX_SKILLS = 200            # 最大技能数
_SUCCESS_BONUS = 0.05        # 每次成功加分（提升匹配优先级）
class SkillMemory:
    """
    技能记忆：倒排索引 + 集合交集匹配。
    - store(): 成功执行后存储技能
    - query(): 查询匹配技能
    - replay_skill(): 回放技能步骤
    """
    def __init__(self, path: str = ""):
        self._path = Path(path) if path else _SKILL_FILE
        self._skills: Dict[str, Skill] = {}          # id → Skill
        self._index: Dict[str, Set[str]] = defaultdict(set)  # keyword → {skill_id}
        self._lock = __import__('threading').RLock()
        self._load()
    # ════════════ 存储 ════════════
    def store(self, task_desc: str, steps: List[str]) -> Optional[str]:
        """
        成功执行后存储技能。
        如果已有高度相似技能则合并/更新，否则新建。
        """
        with self._lock:
            kws = _segment(task_desc)
            if not kws or not steps:
                return None
            # 检查是否已有高度相似技能（锁内调用，避免递归死锁用RLock）
            existing = self._query_locked(task_desc, threshold=0.6)
            if existing:
                sid = existing["id"]
                skill = self._skills[sid]
                skill.success_count += 1
                skill.last_used = time.time()
                if skill.steps != steps:
                    skill.steps = steps
                    logger.info("技能更新: %s (steps 变更)", task_desc[:20])
                self._save()
                return sid
            # 新建技能
            sid = f"sk_{int(time.time() * 1000)}_{id(self)}_{len(self._skills)}"
            skill = Skill(
                id=sid,
                description=task_desc,
                keywords=kws,
                steps=steps,
                success_count=1,
                last_used=time.time(),
                created_at=time.time(),
            )
            self._skills[sid] = skill
            for kw in kws:
                self._index[kw].add(sid)
            if len(self._skills) > _MAX_SKILLS:
                self._evict()
            self._save()
            logger.info("技能存储: %s [%s] %d步", sid, task_desc[:20], len(steps))
            return sid
    # ════════════ 查询 ════════════
    def update_usage(self, skill_id: str):
        """更新技能使用次数和时间（锁内操作）。"""
        with self._lock:
            if skill_id in self._skills:
                self._skills[skill_id].success_count += 1
                self._skills[skill_id].last_used = time.time()
                self._save()

    def query(self, task_desc: str, threshold: float = None) -> Optional[Dict]:
        """查询最匹配的技能（加锁）。"""
        with self._lock:
            return self._query_locked(task_desc, threshold)

    def _query_locked(self, task_desc: str, threshold: float = None) -> Optional[Dict]:
        """
        查询最匹配的技能（调用方须持有 _lock）。
        匹配算法：Jaccard 相似度 + 成功次数加权。
        """
        threshold = threshold or _MATCH_THRESHOLD
        query_kws = set(_segment(task_desc))
        if not query_kws:
            return None
        # 候选收集：任一关键词命中
        candidates: Set[str] = set()
        for kw in query_kws:
            candidates.update(self._index.get(kw, set()))
        if not candidates:
            return None
        # 打分：Jaccard + 成功次数加权
        best_id = None
        best_score = 0.0
        for sid in candidates:
            skill = self._skills.get(sid)
            if not skill:
                continue
            sk_kws = skill.keyword_set
            intersection = len(query_kws & sk_kws)
            union = len(query_kws | sk_kws)
            jaccard = intersection / union if union > 0 else 0
            weight = 1.0 + min(skill.success_count * _SUCCESS_BONUS, 0.5)
            score = jaccard * weight
            if score > best_score:
                best_score = score
                best_id = sid
        if best_score < threshold or best_id is None:
            return None
        skill = self._skills[best_id]
        return {
            "id": skill.id,
            "description": skill.description,
            "steps": list(skill.steps),
            "score": round(best_score, 3),
            "success_count": skill.success_count,
        }
    # ════════════ 统计 ════════════
    def stats(self) -> Dict[str, Any]:
        return {
            "total": len(self._skills),
            "keywords": len(self._index),
            "top_skills": sorted(
                self._skills.values(),
                key=lambda s: s.success_count, reverse=True
            )[:5],
        }
    # ════════════ 持久化 ════════════
    def _load(self):
        if not self._path.exists():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                for item in data.get("skills", []):
                    skill = Skill(
                        id=item["id"],
                        description=item["description"],
                        keywords=item.get("keywords", []),
                        steps=item.get("steps", []),
                        success_count=item.get("success_count", 1),
                        last_used=item.get("last_used", 0),
                        created_at=item.get("created_at", 0),
                    )
                    self._skills[skill.id] = skill
                    for kw in skill.keywords:
                        self._index[kw].add(skill.id)
            logger.info("技能记忆加载: %d 条", len(self._skills))
        except Exception as e:
            logger.warning("技能记忆加载失败: %s", e)
    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "skills": [
                    {
                        "id": s.id,
                        "description": s.description,
                        "keywords": s.keywords,
                        "steps": s.steps,
                        "success_count": s.success_count,
                        "last_used": s.last_used,
                        "created_at": s.created_at,
                    }
                    for s in self._skills.values()
                ]
            }
            tmp = self._path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(str(tmp), str(self._path))
        except Exception as e:
            logger.warning("技能记忆保存失败: %s", e)
    def _evict(self):
        """淘汰最旧且最少成功的技能。"""
        sorted_skills = sorted(
            self._skills.values(),
            key=lambda s: (s.success_count, s.last_used),
        )
        to_remove = sorted_skills[:len(sorted_skills) - _MAX_SKILLS + 10]
        for skill in to_remove:
            del self._skills[skill.id]
            for kw in skill.keywords:
                self._index[kw].discard(skill.id)
                if not self._index[kw]:
                    del self._index[kw]
# ════════════════════════════════════════════
# 回放入口
# ════════════════════════════════════════════
async def replay_skill(matched: Dict, uid: int) -> Dict:
    """
    回放匹配到的技能步骤。
    直接调用 agent_loop._execute，跳过规划阶段。
    """
    try:
        from dong import agent_loop as _al
        steps = matched["steps"]
        history = [f"技能回放: {matched['description']} | 步骤: {' → '.join(steps)}"]
        result = await _al._execute(
            subs=list(steps),
            history=history,
            task=matched["description"],
            uid=uid,
            start=0,
        )
        if result.get("type") == "done":
            skill_memory.update_usage(matched["id"])
        return result
    except Exception as e:
        logger.exception("技能回放异常: %s", e)
        return {"type": "error", "message": f"技能回放失败: {e}"}
# 全局单例（懒加载，避免导入时立即触发文件IO）
_skill_memory: Optional[SkillMemory] = None

def get_skill_memory() -> SkillMemory:
    global _skill_memory
    if _skill_memory is None:
        _skill_memory = SkillMemory()
    return _skill_memory

# 向后兼容：首次访问时自动初始化
skill_memory = get_skill_memory()