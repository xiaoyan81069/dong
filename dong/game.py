"""
冬 · 游戏状态 — 场景、物品栏、商店
数据文件: dong_game.json
"""
import os
import threading
from datetime import datetime
from .core.state_store import StateStore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GAME_FILE = os.path.join(BASE_DIR, "dong_game.json")
_lock = threading.Lock()

# 物品栏槽位
INVENTORY_SLOTS = ["hat", "scarf", "accessory", "wall", "desk"]

# 商店目录：分类为 家具/零食/服饰/装饰/猫咪周边
SHOP_CATALOG = [
    # 服饰
    {"id": "scarf_01", "name": "红围巾", "price": 80, "slot": "scarf", "category": "服饰",
     "desc": "一条温暖的红色围巾，冬最喜欢的那条", "icon": "🧣"},
    {"id": "hat_bear", "name": "熊耳朵帽", "price": 120, "slot": "hat", "category": "服饰",
     "desc": "毛茸茸的熊耳朵帽子，戴上会变得更可爱", "icon": "🐻"},
    {"id": "scarf_checkered", "name": "格纹围巾", "price": 65, "slot": "scarf", "category": "服饰",
     "desc": "蓝白格纹，文艺少女风", "icon": "🧣"},
    # 装饰
    {"id": "snow_globe", "name": "雪花球", "price": 150, "slot": "desk", "category": "装饰",
     "desc": "摇一摇雪花就飘起来，里面的小房子和冬的小屋一模一样", "icon": "🌨️"},
    {"id": "poster_gorky", "name": "高尔基公园海报", "price": 90, "slot": "wall", "category": "装饰",
     "desc": "莫斯科高尔基公园的老海报，冬的俄语梦", "icon": "🖼️"},
    {"id": "lava_lamp", "name": "熔岩灯", "price": 110, "slot": "desk", "category": "装饰",
     "desc": "暖橙色的熔岩缓缓流动，深夜学习的最佳伴侣", "icon": "🪔"},
    # 零食
    {"id": "jelly_orange", "name": "果冻橙", "price": 8, "slot": "desk", "category": "零食",
     "desc": "冬最爱的果冻橙，一口下去甜到心里", "icon": "🍊"},
    {"id": "milk_hot", "name": "热牛奶", "price": 12, "slot": "desk", "category": "零食",
     "desc": "一杯热腾腾的牛奶，睡前喝最好", "icon": "🥛"},
    {"id": "cookie_box", "name": "曲奇礼盒", "price": 35, "slot": "desk", "category": "零食",
     "desc": "蔓越莓曲奇，配红茶刚好", "icon": "🍪"},
    # 猫咪周边
    {"id": "cat_bed_small", "name": "迷你猫窝", "price": 180, "slot": "wall", "category": "猫咪",
     "desc": "给杏仁核猫猫的小窝，放在墙角刚刚好", "icon": "🐱"},
    {"id": "cat_toy_mouse", "name": "逗猫棒", "price": 25, "slot": "accessory", "category": "猫咪",
     "desc": "带铃铛的逗猫棒，猫猫会追着跑", "icon": "🪁"},
]

DEFAULT_STATE = {
    "scene_id": "dorm_room",
    "coins": 100,
    "inventory": {
        "equipped": {"hat": None, "scarf": None, "accessory": None, "wall": None, "desk": None},
        "items": [],
    },
    "last_scene_update": "",
}


_store = StateStore(GAME_FILE)
_store.register("scene_id", "dorm_room")
_store.register("coins", 100)
_store.register("inventory", {
    "equipped": {"hat": None, "scarf": None, "accessory": None, "wall": None, "desk": None},
    "items": [],
})
_store.register("last_scene_update", "")


def load_game_state():
    return {
        "scene_id": _store.get("scene_id", "dorm_room"),
        "coins": _store.get("coins", 100),
        "inventory": _store.get("inventory", {}),
        "last_scene_update": _store.get("last_scene_update", ""),
    }


def save_game_state(data):
    with _lock:
        for k in ("scene_id", "coins", "inventory", "last_scene_update"):
            if k in data:
                _store.set(k, data[k])
        _store.flush()


def get_inventory():
    return _store.get("inventory", {"equipped": {}, "items": []})


def get_equipped():
    return _store.get("inventory", {}).get("equipped", {})


def buy_item(item_id):
    """购买物品：从商店目录找到物品，调用零花钱系统扣款，加入物品栏"""
    try:
        from .finance import get_balance, add_transaction
    except ImportError:
        from finance import get_balance, add_transaction

    item = next((i for i in SHOP_CATALOG if i["id"] == item_id), None)
    if not item:
        return {"success": False, "error": "物品不存在"}

    balance = get_balance()
    if balance < item["price"]:
        return {"success": False, "error": f"余额不足，需要{item['price']}雪花币，当前余额{balance}"}

    with _lock:
        inv = _store.get("inventory")
        existing = next((i for i in inv["items"] if i["item_id"] == item_id), None)
        if existing:
            existing["qty"] = existing.get("qty", 1) + 1
        else:
            inv["items"].append({"item_id": item_id, "qty": 1, "acquired_at": datetime.now().strftime("%Y-%m-%d %H:%M")})

        _store.set("inventory", inv)
        _store.set("last_scene_update", datetime.now().isoformat())
        _store.flush()

    # 扣款
    new_balance = add_transaction(f"买了{item['name']}", -item["price"], "expense")

    return {
        "success": True,
        "item": item,
        "new_balance": new_balance,
        "message": f"成功购买{item['name']}！余额: {new_balance}雪花币",
    }


def equip_item(item_id, slot=None):
    """穿戴/使用物品"""
    with _lock:
        inv = _store.get("inventory")

        owned = next((i for i in inv["items"] if i["item_id"] == item_id and i.get("qty", 0) > 0), None)
        if not owned:
            return {"success": False, "error": "没有这个物品"}

        item_info = next((i for i in SHOP_CATALOG if i["id"] == item_id), None)
        if not item_info or not item_info.get("slot"):
            return {"success": False, "error": "此物品无法穿戴"}

        target_slot = slot or item_info["slot"]
        old_equipped = inv["equipped"].get(target_slot)

        if old_equipped:
            old_item = next((i for i in inv["items"] if i["item_id"] == old_equipped), None)
            if old_item:
                old_item["qty"] = old_item.get("qty", 0) + 1

        inv["equipped"][target_slot] = item_id
        owned["qty"] = owned.get("qty", 1) - 1
        if owned["qty"] <= 0:
            inv["items"].remove(owned)

        _store.set("inventory", inv)
        _store.flush()

    return {
        "success": True,
        "equipped": inv["equipped"],
        "item": item_info,
        "message": f"已穿上{item_info['name']}",
    }


def get_item_detail(item_id):
    """获取场景物品的冬的吐槽/描述"""
    item = next((i for i in SHOP_CATALOG if i["id"] == item_id), None)
    if item:
        return {
            "success": True,
            "item": item,
            "detail": item.get("desc", ""),
        }

    # 场景物品（非商店物品）
    scene_items = {
        "piano": "这是冬的手风琴…不对，钢琴。她其实不太会弹，但弹《莫斯科郊外的晚上》时整个人都在发光。",
        "mailbox": "红色的小邮筒，冬每天都会去看看有没有信。虽然大多数时候是空的，但她还是坚持检查。",
        "window": "窗外是呼伦贝尔的草原。夏天能看到一望无际的绿，冬天则是一片白茫茫的雪原。",
        "desk_lamp": "暖黄色的台灯，陪伴冬度过了无数个熬夜复习的夜晚。灯罩上贴了一张便利贴：'早睡!!!'",
        "bookshelf": "书架上塞满了俄语教材和小说。普希金、陀思妥耶夫斯基、阿赫玛托娃……还有一本偷偷夹在中间的《蜡笔小新》。",
        "bed": "冬的床总是收拾得很整齐，枕头边放着一只旧旧的毛绒熊。那是她从小抱到大的。",
        "fridge": "冰箱里常备着牛奶和果冻橙。门上贴满了外卖单和课程表。",
        "calendar": "墙上挂着一本手撕日历，冬每天早起第一件事就是撕一页。重大考试的日子都用红笔圈了出来。",
        "teapot": "一只搪瓷茶壶，泡的是内蒙古砖茶。冬说这是她奶奶教的喝法——加一点盐和奶。",
        "photo_frame": "相框里是冬和室友的合照，两个人在雪地里笑得眼睛都眯成了一条缝。",
    }
    if item_id in scene_items:
        return {"success": True, "item": {"id": item_id, "name": item_id}, "detail": scene_items[item_id]}

    return {"success": False, "error": "未知物品"}


def get_game_snapshot():
    """获取游戏状态快照"""
    inv = _store.get("inventory", {})
    return {
        "scene_id": _store.get("scene_id", "dorm_room"),
        "inventory": {
            "coins": _store.get("coins", 100),
            "equipped": inv.get("equipped", {}),
            "items": inv.get("items", []),
        },
        "shop_catalog": SHOP_CATALOG,
    }
