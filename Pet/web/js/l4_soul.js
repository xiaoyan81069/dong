/**
 * L4 灵魂面板 — 冬的手写笔记本
 * 淡黄纸张、手写体、杏仁核橘猫
 */

function updateL4(state) {
    var l4 = state.l4_panel || {};
    var finance = state.finance || {};
    var mail = state.mail || {};
    var status = state.status || {};
    var overwhelm = status.overwhelm || {};

    updateHormoneNotes(l4.hormone_notes || {});
    updateAmygdalaCat(l4.amygdala_cat || 'sleepy');
    updateGrudgeList(l4.grudges || []);
    updateRecentMemories(l4.recent_memories || []);
    updateSystemHealth(l4.system_health || '');
    updateFinanceLedger(finance);
    updateFrostOverlay(overwhelm);
}

function updateHormoneNotes(notes) {
    var el = document.getElementById('l4-hormone-notes');
    if (!el) return;
    var html = '';
    for (var key in notes) {
        if (notes[key]) {
            html += '<div class="l4-note-line"><span class="l4-hormone-label">' + key + '</span> ' + notes[key] + '</div>';
        }
    }
    el.innerHTML = html || '<div class="l4-note-line">今天心情普普通通</div>';
}

function updateAmygdalaCat(state) {
    var el = document.getElementById('l4-cat');
    if (!el) return;
    var cats = {
        'sleepy': { emoji: '😸', text: '猫猫睡着了，缩成一团橘色的毛球', class: 'cat-sleepy' },
        'alert': { emoji: '🐱', text: '猫猫竖起了耳朵，警惕地四处张望', class: 'cat-alert' },
        'explode': { emoji: '🙀', text: '猫猫炸毛了！弓着背发出嘶嘶声', class: 'cat-explode' },
    };
    var c = cats[state] || cats['sleepy'];
    el.innerHTML = '<div class="l4-cat-icon ' + c.class + '">' + c.emoji + '</div>'
        + '<div class="l4-cat-text">' + c.text + '</div>';
}

function updateGrudgeList(grudges) {
    var el = document.getElementById('l4-grudges');
    if (!el) return;
    if (!grudges || grudges.length === 0) {
        el.innerHTML = '<div style="color:#9a8a7a;font-style:italic;">这页是空白的...</div>';
        return;
    }
    var html = '';
    grudges.forEach(function(g, i) {
        var aged = i > 0 && grudges.length - i > 3; // 旧的记仇发黄
        html += '<div class="l4-grudge-item' + (aged ? ' aged' : '') + '">'
            + '<div class="l4-grudge-what">' + g.what + '</div>'
            + '<div class="l4-grudge-expire">' + (g.expire || '') + '</div>'
            + '</div>';
    });
    el.innerHTML = html;
}

function updateRecentMemories(memories) {
    var el = document.getElementById('l4-memories');
    if (!el) return;
    if (!memories || memories.length === 0) {
        el.innerHTML = '<div style="color:#9a8a7a;">还没发生什么...</div>';
        return;
    }
    el.innerHTML = memories.slice(-5).map(function(m) {
        return '<div class="l4-memory-item">· ' + (typeof m === 'string' ? m : (m.q || m)) + '</div>';
    }).join('');
}

function updateSystemHealth(text) {
    var el = document.getElementById('l4-health');
    if (el) el.textContent = text || '心跳正常';
}

function updateFinanceLedger(finance) {
    var el = document.getElementById('l4-finance');
    if (!el) return;
    var balance = finance.balance || 0;
    var txs = finance.transactions || [];
    var html = '<div class="l4-balance">余额: <span class="l4-balance-num' + (balance === 0 ? ' zero' : '') + '">'
        + balance.toFixed(1) + '</span> 雪花币</div>';

    // 显示最近5笔
    var recent = txs.slice(-5).reverse();
    recent.forEach(function(tx) {
        var sign = tx.amount > 0 ? '+' : '';
        var c = tx.amount > 0 ? 'income' : 'expense';
        html += '<div class="l4-tx-line ' + c + '">'
            + tx.desc + '  ' + sign + tx.amount.toFixed(1)
            + '</div>';
    });

    el.innerHTML = html;

    // 余额为0时字迹加重
    if (balance <= 0) {
        el.classList.add('balance-zero');
    } else {
        el.classList.remove('balance-zero');
    }
}

function updateFrostOverlay(overwhelm) {
    var el = document.getElementById('l4-frost');
    if (!el) return;
    if (overwhelm.active) {
        el.style.display = 'block';
        if (overwhelm.phase === 'peak') {
            el.style.opacity = '0.6';
        } else {
            el.style.opacity = '0.25';
        }
    } else {
        el.style.display = 'none';
    }
}
