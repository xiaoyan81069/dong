/**
 * L2 外景 — 呼伦贝尔草原、雪屋、邮筒
 * 天气和雪花由后端激素数据驱动
 */

var l2State = {};

function updateL2(state) {
    var status = state.status || {};
    var weather = state.weather || {};
    var mail = state.mail || {};
    var game = state.game || {};
    var scene = state.scene || {};

    l2State = {
        mood: status.mood || 50,
        weatherType: weather.type || 'snow',
        skyTint: weather.sky_tint || '#d0c8e0',
        snowDensity: weather.snowflake_density || 1.5,
        snowSpeed: weather.snowflake_speed || 0.8,
        snowColor: weather.snow_color || '#ffffff',
        wind: weather.wind || 0.5,
        hasNewMail: mail.has_new_mail || false,
        botState: status.bot_state || '清醒',
        sleeping: status.sleeping || false,
        currentActivity: (status.schedule || {}).current_activity || '',
        overwhelmActive: (status.overwhelm || {}).active || false,
    };

    updateSkyGradient();
    updateHouseLight();
    updateMailboxFlag();
    updateWeatherLabel();
}

// ===== 天空渐变 =====
function updateSkyGradient() {
    var sky = document.getElementById('l2-sky');
    if (!sky) return;
    var tint = l2State.skyTint;
    sky.style.background = 'linear-gradient(180deg, ' + tint + ' 0%, #d8d0c8 100%)';
}

// ===== 雪屋窗户光线 =====
function updateHouseLight() {
    var window = document.getElementById('l2-house-window');
    if (!window) return;
    var mood = l2State.mood;
    if (mood > 70) {
        window.style.background = 'rgba(255, 200, 60, 0.9)';
        window.style.boxShadow = '0 0 20px rgba(255, 200, 60, 0.6)';
    } else if (mood < 30) {
        window.style.background = 'rgba(60, 80, 160, 0.7)';
        window.style.boxShadow = '0 0 8px rgba(60, 80, 160, 0.3)';
    } else {
        window.style.background = 'rgba(255, 180, 80, 0.7)';
        window.style.boxShadow = '0 0 12px rgba(255, 180, 80, 0.4)';
    }
}

// ===== 邮筒红旗 =====
function updateMailboxFlag() {
    var flag = document.getElementById('l2-mail-flag');
    if (!flag) return;
    if (l2State.hasNewMail) {
        flag.style.display = 'block';
        flag.classList.add('pulse');
    } else {
        flag.style.display = 'none';
        flag.classList.remove('pulse');
    }
}

// ===== 天气标签 =====
function updateWeatherLabel() {
    var label = document.getElementById('l2-weather-label');
    if (!label) return;
    var typeNames = {
        'sunny': '天晴了',
        'light_snow': '小雪',
        'snow': '下雪了',
        'heavy_snow': '大雪',
        'blizzard': '暴风雪',
        'storm': '狂风',
        'hijack_storm': '暴风骤雨',
    };
    label.textContent = typeNames[l2State.weatherType] || '飘雪中';
}

// ===== 雪花粒子系统 (Canvas) =====
var snowParticles = [];
var snowCanvas, snowCtx;
var snowAnimId;

function initSnowSystem() {
    snowCanvas = document.getElementById('l2-snow-canvas');
    if (!snowCanvas) return;
    snowCtx = snowCanvas.getContext('2d');
    resizeSnowCanvas();
    window.addEventListener('resize', resizeSnowCanvas);
    spawnSnowflakes();
    snowAnimId = requestAnimationFrame(animateSnow);
}

function resizeSnowCanvas() {
    if (!snowCanvas) return;
    snowCanvas.width = snowCanvas.offsetWidth;
    snowCanvas.height = snowCanvas.offsetHeight;
}

function spawnSnowflakes() {
    var maxParticles = 200;
    while (snowParticles.length < maxParticles) {
        snowParticles.push(createSnowflake(true));
    }
}

function createSnowflake(randomY) {
    var w = snowCanvas ? snowCanvas.width : 400;
    var h = snowCanvas ? snowCanvas.height : 600;
    return {
        x: Math.random() * w,
        y: randomY ? Math.random() * h : -10,
        r: 1 + Math.random() * 3,
        speed: 0.3 + Math.random() * 1.2,
        wind: (Math.random() - 0.5) * 0.8,
        wobble: Math.random() * Math.PI * 2,
        opacity: 0.3 + Math.random() * 0.7,
    };
}

function animateSnow() {
    if (!snowCtx || !snowCanvas) {
        snowAnimId = requestAnimationFrame(animateSnow);
        return;
    }

    var w = snowCanvas.width;
    var h = snowCanvas.height;

    snowCtx.clearRect(0, 0, w, h);

    var density = l2State.snowDensity || 1.0;
    var speed = l2State.snowSpeed || 0.5;
    var color = l2State.snowColor || '#ffffff';
    var wind = l2State.wind || 0;
    var maxParticles = Math.floor(density * 50);
    maxParticles = Math.max(20, Math.min(200, maxParticles));

    // 调整粒子数量
    while (snowParticles.length < maxParticles) {
        snowParticles.push(createSnowflake(true));
    }
    while (snowParticles.length > maxParticles) {
        snowParticles.pop();
    }

    for (var i = 0; i < snowParticles.length; i++) {
        var p = snowParticles[i];
        p.wobble += 0.01;
        p.y += p.speed * speed;
        p.x += (p.wind + wind * 0.5) * 0.3 + Math.sin(p.wobble) * 0.2;

        // 越界则重置到顶部
        if (p.y > h + 5) {
            p.y = -5;
            p.x = Math.random() * w;
        }
        if (p.x > w + 5) p.x = -5;
        if (p.x < -5) p.x = w + 5;

        snowCtx.beginPath();
        snowCtx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        snowCtx.fillStyle = color;
        snowCtx.globalAlpha = p.opacity;
        snowCtx.fill();
    }
    snowCtx.globalAlpha = 1;

    snowAnimId = requestAnimationFrame(animateSnow);
}

function stopSnow() {
    if (snowAnimId) {
        cancelAnimationFrame(snowAnimId);
        snowAnimId = null;
    }
}

// ===== 点击交互 =====
function onMailboxClick() {
    if (typeof navigateTo === 'function') {
        navigateTo('l4');  // 跳转到笔记本查看信件
    }
}

function onHouseDoorClick() {
    if (typeof navigateTo === 'function') {
        navigateTo('l3');  // 进入小屋内
    }
}

// 雪花系统由 main.js 在桥接成功后调用 initSnowSystem() 启动
