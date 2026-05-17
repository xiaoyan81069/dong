/**
 * 冬桌面宠物 · 主入口 (前端大脑)
 * 负责 QWebChannel 连接、状态分发、视图路由
 */

var currentView = 'l2';

window.addEventListener('DOMContentLoaded', function () {

    // ===== 默认显示 L2 =====
    navigateTo('l2');

    // ===== QWebChannel 初始化 =====
    if (typeof QWebChannel !== 'undefined') {
        new QWebChannel(qt.webChannelTransport, function (channel) {
            window.bridge = channel.objects.bridge;

            // 启动雪花粒子系统
            if (typeof initSnowSystem === 'function') {
                initSnowSystem();
            }

            // 监听后端状态推送（每秒）
            bridge.state_updated.connect(function (jsonStr) {
                try {
                    var state = JSON.parse(jsonStr);
                    onStateUpdate(state);
                } catch (e) {
                    console.error('[winter_pet] 状态解析失败:', e);
                }
            });

            // 监听连接状态变化
            bridge.connection_changed.connect(function (connected) {
                setConnected(connected);
            });

            // 加载缓存状态（避免白屏）
            var cachedJson = bridge.get_cached_state();
            if (cachedJson && cachedJson !== '{}') {
                try {
                    onStateUpdate(JSON.parse(cachedJson));
                } catch (e) {}
            }
        });
    } else {
        // 非 PyQt 环境降级
        console.warn('[winter_pet] QWebChannel 未找到，请在 PyQt5 环境中运行');
        document.getElementById('offline-overlay').classList.add('show');
    }

    // ===== 导航栏点击 =====
    document.querySelectorAll('.nav-tab').forEach(function (tab) {
        tab.addEventListener('click', function () {
            navigateTo(this.dataset.view);
        });
    });

    // ===== 最小化按钮 =====
    var btnMin = document.getElementById('btn-minimize');
    if (btnMin) {
        btnMin.addEventListener('click', function () {
            document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
        });
    }
});

// ===== 状态分发 =====
function onStateUpdate(state) {
    setConnected(true);
    window.currentState = state; // 缓存供其他模块读取

    if (typeof updateL2 === 'function') updateL2(state);
    if (typeof updateL3 === 'function') updateL3(state);
    if (typeof updateL4 === 'function') updateL4(state);
}

// ===== 视图路由 =====
function navigateTo(viewId) {
    currentView = viewId;

    // 切换视图容器
    document.querySelectorAll('.view').forEach(function (el) {
        if (el.id === 'view-' + viewId) {
            el.classList.add('active');
        } else {
            el.classList.remove('active');
        }
    });

    // 切换导航栏高亮
    document.querySelectorAll('.nav-tab').forEach(function (tab) {
        if (tab.getAttribute('data-view') === viewId) {
            tab.classList.add('active');
        } else {
            tab.classList.remove('active');
        }
    });
}

// ===== 连接状态 =====
function setConnected(connected) {
    var dot = document.getElementById('connection-status');
    var overlay = document.getElementById('offline-overlay');

    if (connected) {
        if (dot) dot.classList.add('connected');
        if (overlay) overlay.classList.remove('show');
    } else {
        if (dot) dot.classList.remove('connected');
        if (overlay) overlay.classList.add('show');
    }
}
