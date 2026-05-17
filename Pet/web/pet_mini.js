/**
 * L1 桌宠 · 换装逻辑
 * 通过 QWebChannel 监听后端状态，切换图层和表情
 */

var l1Bridge;
var isOffline = false;

window.addEventListener('DOMContentLoaded', function () {
    if (typeof QWebChannel !== 'undefined') {
        new QWebChannel(qt.webChannelTransport, function (channel) {
            l1Bridge = channel.objects.l1Bridge;

            l1Bridge.state_updated.connect(function (jsonStr) {
                try {
                    updatePetVisual(JSON.parse(jsonStr));
                } catch (e) {}
            });

            l1Bridge.connection_changed.connect(function (connected) {
                isOffline = !connected;
                document.getElementById('offline-mask').style.display = connected ? 'none' : 'block';
            });

            var cached = l1Bridge.get_cached_state();
            if (cached && cached !== '{}') {
                try { updatePetVisual(JSON.parse(cached)); } catch (e) {}
            }
        });
    } else {
        document.getElementById('offline-mask').style.display = 'block';
    }

    // CSS fallback 默认可见（图片 onerror 时也算）
    setTimeout(function () {
        var headImg = document.getElementById('head-layer');
        if (headImg && !headImg.complete) {
            headImg.onerror = function () {
                document.getElementById('css-fallback').classList.remove('hidden');
            };
        }
    }, 100);
});

function updatePetVisual(state) {
    var status = state.status || {};
    var game = state.game || {};
    var equipped = (game.inventory || {}).equipped || {};
    var mood = status.mood || 50;
    var sleeping = status.sleeping || false;
    var mail = state.mail || {};

    // 1. 头部表情（有图片则用图片，无则调 CSS SVG）
    var headImg = document.getElementById('head-layer');
    var fallback = document.getElementById('css-fallback');

    if (isOffline || sleeping) {
        if (headImg) headImg.src = 'assets/pet/head_sleep.png';
        if (fallback) fallback.classList.remove('hidden');
    } else if (mood > 75) {
        if (headImg) headImg.src = 'assets/pet/head_happy.png';
    } else if (mood < 35) {
        if (headImg) headImg.src = 'assets/pet/head_sad.png';
    } else {
        if (headImg) headImg.src = 'assets/pet/head_normal.png';
    }

    // 更新 CSS 回退的 SVG 表情
    updateSvgExpression(mood, sleeping, isOffline);

    // 2. 衣服换装
    var clothes = document.getElementById('clothes-layer');
    if (clothes) {
        if (equipped.body === 'pajama_01') {
            clothes.src = 'assets/pet/clothes_pajama.png';
        } else if (equipped.scarf === 'scarf_01' || equipped.scarf === 'scarf_checkered') {
            clothes.src = 'assets/pet/clothes_scarf.png';
        } else if (equipped.neck === 'scarf_01') {
            clothes.src = 'assets/pet/clothes_scarf.png';
        } else {
            clothes.src = 'assets/pet/clothes_empty.png';
        }
    }

    // 3. 伴随物
    var comp = document.getElementById('companion-layer');
    if (comp) {
        if (equipped.companion === 'cat_tree' || equipped.companion === 'cat_toy_mouse') {
            comp.src = 'assets/pet/companion_cat.png';
        } else {
            comp.src = 'assets/pet/companion_empty.png';
        }
    }

    // 4. 心情气泡
    var bubble = document.getElementById('mood-bubble');
    if (bubble) {
        if (sleeping) {
            bubble.textContent = '💤';
            bubble.style.opacity = '1';
        } else if (mail.has_new_mail) {
            bubble.textContent = '✉️';
            bubble.style.opacity = '1';
        } else if (mood > 80) {
            bubble.textContent = '☀️';
            bubble.style.opacity = '1';
        } else if (mood < 30) {
            bubble.textContent = '❄️';
            bubble.style.opacity = '1';
        } else {
            bubble.style.opacity = '0';
        }
    }
}

function updateSvgExpression(mood, sleeping, offline) {
    var eyeL = document.getElementById('css-eye-l');
    var eyeR = document.getElementById('css-eye-r');
    var mouth = document.getElementById('css-mouth');
    if (!eyeL || !eyeR || !mouth) return;

    if (offline || sleeping) {
        // 闭眼 = 横线
        eyeL.setAttribute('cy', '45');
        eyeL.setAttribute('rx', '5');
        eyeL.setAttribute('ry', '1');
        eyeR.setAttribute('cy', '45');
        eyeR.setAttribute('rx', '5');
        eyeR.setAttribute('ry', '1');
        mouth.setAttribute('d', '');
    } else if (mood > 75) {
        // 开心 = 弯眼
        eyeL.setAttribute('cy', '44');
        eyeL.setAttribute('rx', '4');
        eyeL.setAttribute('ry', '5');
        eyeR.setAttribute('cy', '44');
        eyeR.setAttribute('rx', '4');
        eyeR.setAttribute('ry', '5');
        mouth.setAttribute('d', 'M52 54 Q60 60 68 54');
    } else if (mood < 35) {
        // 低落 = 垂眼
        mouth.setAttribute('d', 'M54 58 Q60 54 66 58');
    } else {
        // 正常
        eyeL.setAttribute('cy', '45');
        eyeL.setAttribute('rx', '4');
        eyeL.setAttribute('ry', '5');
        eyeR.setAttribute('cy', '45');
        eyeR.setAttribute('rx', '4');
        eyeR.setAttribute('ry', '5');
        mouth.setAttribute('d', 'M54 56 Q60 62 66 56');
    }
}
