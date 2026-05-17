/**
 * L3 内景 — 5个微缩场景：卧室/书桌/食堂/琴房/窗边
 * 场景由后端 game.scene_id 驱动切换
 */

var l3CurrentScene = '';

// 场景名称映射
var SCENE_NAMES = {
    'sleeping': 'bedroom',
    'bedroom': 'bedroom',
    'night_bedroom': 'bedroom',
    'classroom': 'desk',
    'studying': 'desk',
    'desk': 'desk',
    'canteen': 'canteen',
    'eating': 'canteen',
    'piano': 'piano',
    'window_seat': 'window',
    'free_time': 'window',
    'dorm_room': 'desk',
    'default': 'desk',
};

var SCENE_META = {
    'bedroom': { name: '卧室', icon: '🛏️', desc: '冬的小床，被子叠得很整齐' },
    'desk': { name: '书桌', icon: '📚', desc: '台灯下摊着一本俄语教材' },
    'canteen': { name: '食堂', icon: '🍜', desc: '食堂新开的窗口排了长队' },
    'piano': { name: '琴房', icon: '🎹', desc: '角落里立着一台旧钢琴' },
    'window': { name: '窗边', icon: '🪟', desc: '窗外是白茫茫的草原' },
};

var currentSceneName = '';

function updateL3(state) {
    var game = state.game || {};
    var sceneId = game.scene_id || 'dorm_room';
    var status = state.status || {};
    var schedule = status.schedule || {};

    // 如果休眠，强制卧室
    if (status.sleeping) {
        sceneId = 'sleeping';
    }

    var mapped = SCENE_NAMES[sceneId] || 'desk';
    if (mapped !== currentSceneName) {
        switchToScene(mapped);
    }

    // 更新当前活动文本
    var activityEl = document.getElementById('l3-activity');
    if (activityEl) {
        activityEl.textContent = schedule.current_activity || '';
    }

    // 更新装备物品显示
    var equipped = (game.inventory || {}).equipped || {};
    updateEquippedDisplay(equipped);
}

function switchToScene(sceneName) {
    var room = document.getElementById('l3-room');
    if (!room) return;

    // 淡出再淡入
    room.style.opacity = '0';
    room.style.transition = 'opacity 0.4s ease';

    setTimeout(function() {
        currentSceneName = sceneName;
        var meta = SCENE_META[sceneName] || SCENE_META['desk'];
        room.innerHTML = buildSceneHTML(sceneName, meta);
        room.style.opacity = '1';

        // 更新场景标签
        var label = document.getElementById('l3-scene-label');
        if (label) label.textContent = meta.icon + ' ' + meta.name;
    }, 400);
}

function buildSceneHTML(sceneName, meta) {
    var html = '';

    if (sceneName === 'bedroom') {
        html = '<div style="font-size:40px;margin-bottom:10px;">🛏️</div>'
            + '<div style="font-size:13px;color:#6a5a4a;">' + meta.desc + '</div>'
            + '<div style="font-size:11px;color:#9a8a7a;margin-top:4px;">枕头边放着一只旧毛绒熊</div>';
    } else if (sceneName === 'desk') {
        html = '<div style="font-size:40px;margin-bottom:10px;">📚</div>'
            + '<div style="font-size:13px;color:#6a5a4a;">' + meta.desc + '</div>'
            + '<div style="font-size:11px;color:#9a8a7a;margin-top:4px;">灯罩上贴了一张便利贴：「早睡!!!」</div>'
            + '<div class="l3-hotspot" onclick="clickSceneItem(\'desk_lamp\')" style="position:absolute;top:60%;left:40%;width:30px;height:30px;cursor:pointer;" title="台灯"></div>'
            + '<div class="l3-hotspot" onclick="clickSceneItem(\'bookshelf\')" style="position:absolute;top:20%;right:15%;width:40px;height:50px;cursor:pointer;" title="书架 → 商店"></div>';
    } else if (sceneName === 'canteen') {
        html = '<div style="font-size:40px;margin-bottom:10px;">🍜</div>'
            + '<div style="font-size:13px;color:#6a5a4a;">' + meta.desc + '</div>'
            + '<div style="font-size:11px;color:#9a8a7a;margin-top:4px;">冬在纠结吃盖浇饭还是番茄鸡蛋面</div>'
            + '<div class="l3-hotspot" onclick="clickSceneItem(\'fridge\')" style="position:absolute;top:50%;right:20%;width:24px;height:36px;cursor:pointer;" title="冰箱"></div>';
    } else if (sceneName === 'piano') {
        html = '<div style="font-size:40px;margin-bottom:10px;">🎹</div>'
            + '<div style="font-size:13px;color:#6a5a4a;">' + meta.desc + '</div>'
            + '<div style="font-size:11px;color:#9a8a7a;margin-top:4px;">琴盖上刻着几个俄文字母</div>'
            + '<div class="l3-hotspot" onclick="clickSceneItem(\'piano\')" style="position:absolute;top:45%;left:20%;width:60px;height:30px;cursor:pointer;" title="钢琴"></div>';
    } else if (sceneName === 'window') {
        html = '<div style="font-size:40px;margin-bottom:10px;">🪟</div>'
            + '<div style="font-size:13px;color:#6a5a4a;">' + meta.desc + '</div>'
            + '<div style="font-size:11px;color:#9a8a7a;margin-top:4px;">窗台上有一只搪瓷茶壶冒着热气</div>'
            + '<div class="l3-hotspot" onclick="clickSceneItem(\'window\')" style="position:absolute;top:15%;left:30%;width:50px;height:40px;cursor:pointer;" title="窗户"></div>'
            + '<div class="l3-hotspot" onclick="clickSceneItem(\'teapot\')" style="position:absolute;top:60%;right:30%;width:20px;height:20px;cursor:pointer;" title="茶壶"></div>';
    }

    return html;
}

function updateEquippedDisplay(equipped) {
    var el = document.getElementById('l3-equipped');
    if (!el) return;
    var items = [];
    for (var slot in equipped) {
        if (equipped[slot]) items.push(equipped[slot]);
    }
    el.textContent = items.length > 0 ? '装备: ' + items.join(', ') : '';
}

function openShop() {
    var overlay = document.getElementById('l3-shop-overlay');
    if (!overlay) return;
    var list = document.getElementById('l3-shop-list');
    var game = ((window.currentState || {}).game || {});
    var catalog = game.shop_catalog || [];
    var html = '';
    if (catalog.length === 0) {
        html = '<div style="text-align:center;padding:20px;">货架空空如也...</div>';
    } else {
        catalog.forEach(function (item) {
            html += '<div class="l3-item-card" onclick="handleBuy(\'' + item.id + '\')">'
                + '<div class="l3-item-icon">' + (item.icon || '🎁') + '</div>'
                + '<div class="l3-item-name">' + item.name + '</div>'
                + '<div class="l3-item-price">' + item.price + ' ⭐</div>'
                + '</div>';
        });
    }
    list.innerHTML = html;
    overlay.classList.remove('hidden');
}

function openInventory() {
    var overlay = document.getElementById('l3-inventory-overlay');
    if (!overlay) return;
    var list = document.getElementById('l3-inv-list');
    var inv = ((window.currentState || {}).game || {}).inventory || {};
    var items = inv.items || [];
    var html = '';
    if (items.length === 0) {
        html = '<div style="text-align:center;padding:20px;">背包里什么都没有...</div>';
    } else {
        items.forEach(function (i) {
            html += '<div class="l3-item-card" onclick="handleEquip(\'' + i.item_id + '\')">'
                + '<div class="l3-item-icon">🎁</div>'
                + '<div class="l3-item-name">' + i.item_id + ' (x' + i.qty + ')</div>'
                + '<div class="l3-item-price">点击装备</div>'
                + '</div>';
        });
    }
    list.innerHTML = html;
    overlay.classList.remove('hidden');
}

function closeL3Modal() {
    var shop = document.getElementById('l3-shop-overlay');
    var inv = document.getElementById('l3-inventory-overlay');
    if (shop) shop.classList.add('hidden');
    if (inv) inv.classList.add('hidden');
}

function handleBuy(itemId) {
    buyItem(itemId, function (res) {
        if (res.success) {
            showToast('购买成功！', 'success');
            closeL3Modal();
        } else {
            showToast(res.error || res.message || '余额不足', 'error');
        }
    });
}

function handleEquip(itemId) {
    equipItem(itemId, function (res) {
        if (res.success) {
            showToast('已装备！', 'success');
            closeL3Modal();
        } else {
            showToast(res.error || '装备失败', 'error');
        }
    });
}

function clickSceneItem(itemId) {
    if (itemId === 'bookshelf') {
        openShop();
    } else if (itemId === 'bed') {
        openInventory();
    } else {
        itemDetail(itemId, function (result) {
            if (result && result.success) {
                showItemDetailPopup(result.item, result.detail);
            }
        });
    }
}

function showItemDetailPopup(item, detail) {
    // 移除旧弹窗
    var old = document.getElementById('l3-detail-popup');
    if (old) old.remove();

    var popup = document.createElement('div');
    popup.id = 'l3-detail-popup';
    popup.style.cssText = 'position:absolute;top:15%;left:10%;right:10%;'
        + 'background:rgba(255,250,240,0.95);padding:16px;border-radius:8px;'
        + 'box-shadow:0 4px 20px rgba(0,0,0,0.3);z-index:60;font-size:13px;'
        + 'color:#4a3a2a;border:1px solid #d8c8a8;';
    popup.innerHTML = '<div style="font-weight:bold;margin-bottom:6px;">' + (item.name || item.id) + '</div>'
        + '<div style="margin-bottom:10px;">' + detail + '</div>'
        + '<button onclick="this.parentElement.remove()" style="background:#8b6f4e;color:#fff;border:none;'
        + 'padding:4px 12px;border-radius:4px;cursor:pointer;font-size:11px;">关闭</button>';

    var view = document.getElementById('view-l3');
    if (view) view.appendChild(popup);

    // 3秒后自动消失
    setTimeout(function() { if (popup.parentElement) popup.remove(); }, 5000);
}
