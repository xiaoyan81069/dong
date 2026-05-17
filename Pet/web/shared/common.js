/**
 * 冬桌面宠物 · 桥接辅助函数
 * 提供便捷的 bridge 调用封装
 */

/**
 * 调用 bridge 槽方法（返回 JSON）
 * @param {string} method - 方法名
 * @param {Array} args - 参数列表
 * @param {Function} callback - 可选回调，接收解析后的结果对象
 */
function callBridge(method, args, callback) {
    if (!bridge) {
        console.warn('[winter_pet] bridge not connected');
        if (callback) callback({ error: true, message: 'bridge未连接' });
        return;
    }
    try {
        var resultJson = bridge[method].apply(bridge, args);
        var result = JSON.parse(resultJson || '{}');
        if (callback) callback(result);
        return result;
    } catch (e) {
        console.error('[winter_pet] callBridge error:', method, e);
        if (callback) callback({ error: true, message: e.toString() });
    }
}

// 便捷方法
function buyItem(itemId, cb)       { return callBridge('buy_item', [itemId], cb); }
function equipItem(itemId, cb)    { return callBridge('equip_item', [itemId], cb); }
function sendLetter(to, content, cb) { return callBridge('send_letter', [to, content], cb); }
function recharge(amount, cb)     { return callBridge('recharge', [amount], cb); }
function itemDetail(itemId, cb)   { return callBridge('trigger_item_detail', [itemId], cb); }

/**
 * Toast 通知
 * @param {string} msg
 * @param {string} type - 'success'|'error'|'info'
 * @param {number} duration - ms
 */
function showToast(msg, type, duration) {
    type = type || 'info';
    duration = duration || 2000;
    var container = document.getElementById('toast-container');
    if (!container) return;
    var toast = document.createElement('div');
    toast.className = 'toast-item ' + type;
    toast.textContent = msg;
    container.appendChild(toast);
    toast.offsetHeight;
    toast.classList.add('show');
    setTimeout(function () {
        toast.classList.remove('show');
        setTimeout(function () {
            if (toast.parentElement) toast.remove();
        }, 300);
    }, duration);
}
