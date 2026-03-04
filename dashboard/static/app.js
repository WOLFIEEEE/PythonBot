/**
 * Trading Bot Dashboard — Real-time Client
 *
 * Uses Socket.IO for live updates, falls back to REST polling.
 * Updates all panels: P&L cards, positions, trades, signals,
 * adaptive engine, and log stream.
 */

// ═══════════════════════════════════════════════════════════════════════
//  Socket.IO Connection
// ═══════════════════════════════════════════════════════════════════════
const socket = io({ reconnection: true, reconnectionDelay: 2000 });

socket.on("connect", () => {
    console.log("Dashboard connected");
    document.getElementById("feed-status").classList.add("connected");
    document.getElementById("feed-status").classList.remove("disconnected");
});

socket.on("disconnect", () => {
    console.log("Dashboard disconnected");
    document.getElementById("feed-status").classList.remove("connected");
    document.getElementById("feed-status").classList.add("disconnected");
});

// Main state update handler
socket.on("state_update", (data) => {
    updatePnlCards(data);
    updateRiskMeter(data.risk || {});
    updatePositions(data.open_positions || []);
    updateTrades(data.closed_trades || []);
    updateSignals(data.signals || {});
    updateAdaptive(data.adaptive || {});
    updateBotStatus(data.bot_running, data.feed || {});
});

// Log line push
socket.on("log_line", (data) => {
    appendLogLine(data.timestamp, data.level, data.message);
});

// Trade event toast notification
socket.on("trade_event", (data) => {
    showTradeToast(data);
});


// ═══════════════════════════════════════════════════════════════════════
//  Clock
// ═══════════════════════════════════════════════════════════════════════
function updateClock() {
    const now = new Date();
    const h = String(now.getHours()).padStart(2, "0");
    const m = String(now.getMinutes()).padStart(2, "0");
    const s = String(now.getSeconds()).padStart(2, "0");
    document.getElementById("clock").textContent = `${h}:${m}:${s}`;
}
setInterval(updateClock, 1000);
updateClock();


// ═══════════════════════════════════════════════════════════════════════
//  P&L Cards
// ═══════════════════════════════════════════════════════════════════════
function updatePnlCards(data) {
    const pnl = data.pnl || {};
    const trades = data.trades || {};

    setPnlValue("realized-pnl", pnl.realized);
    setPnlValue("unrealized-pnl", pnl.unrealized);
    setPnlValue("total-pnl", pnl.total);

    const tradesEl = document.getElementById("trades-today");
    tradesEl.textContent = `${trades.total || 0}`;
    tradesEl.className = "card-value neutral";

    const wrEl = document.getElementById("win-rate");
    const wr = trades.win_rate || 0;
    wrEl.textContent = trades.total > 0 ? `${wr}%` : "--";
    wrEl.className = "card-value " + (wr >= 50 ? "positive" : wr > 0 ? "negative" : "neutral");

    const openEl = document.getElementById("open-count");
    openEl.textContent = data.open_count || 0;
    openEl.className = "card-value " + (data.open_count > 0 ? "neutral" : "neutral");
}

function setPnlValue(id, value) {
    const el = document.getElementById(id);
    if (value === undefined || value === null) {
        el.textContent = "--";
        el.className = "card-value neutral";
        return;
    }
    const sign = value >= 0 ? "+" : "";
    el.textContent = `${sign}${formatCurrency(value)}`;
    el.className = "card-value " + (value > 0 ? "positive" : value < 0 ? "negative" : "neutral");
}

function formatCurrency(v) {
    return new Intl.NumberFormat("en-IN", {
        style: "currency",
        currency: "INR",
        minimumFractionDigits: 0,
        maximumFractionDigits: 0,
    }).format(v);
}


// ═══════════════════════════════════════════════════════════════════════
//  Risk Meter
// ═══════════════════════════════════════════════════════════════════════
function updateRiskMeter(risk) {
    const bar = document.getElementById("risk-bar");
    const text = document.getElementById("risk-text");
    const pct = risk.loss_pct_used || 0;

    bar.style.width = Math.min(pct, 100) + "%";
    bar.className = "risk-bar-fill" +
        (pct > 75 ? " danger" : pct > 40 ? " warning" : "");
    text.textContent = `${pct.toFixed(1)}% used`;

    document.getElementById("risk-trades").textContent =
        `Trades: ${risk.trades_today || 0}/${risk.max_trades || 0}`;
    document.getElementById("risk-consec").textContent =
        `Consec. Losses: ${risk.consecutive_losses || 0}`;
    document.getElementById("risk-capital").textContent =
        `Capital: ${risk.capital ? formatCurrency(risk.capital) : "--"}`;
}


// ═══════════════════════════════════════════════════════════════════════
//  Bot Status
// ═══════════════════════════════════════════════════════════════════════
function updateBotStatus(running, feed) {
    const badge = document.getElementById("bot-status");
    if (running) {
        badge.textContent = "RUNNING";
        badge.className = "status-badge status-running";
    } else {
        badge.textContent = "OFFLINE";
        badge.className = "status-badge status-offline";
    }

    const feedBadge = document.getElementById("feed-status");
    if (feed.connected) {
        feedBadge.classList.add("connected");
        feedBadge.classList.remove("disconnected");
    } else {
        feedBadge.classList.remove("connected");
        feedBadge.classList.add("disconnected");
    }
}


// ═══════════════════════════════════════════════════════════════════════
//  Positions Table
// ═══════════════════════════════════════════════════════════════════════
function updatePositions(positions) {
    const tbody = document.getElementById("positions-body");
    if (!positions.length) {
        tbody.innerHTML = '<tr class="empty-row"><td colspan="10">No open positions</td></tr>';
        return;
    }
    tbody.innerHTML = positions.map(p => `
        <tr>
            <td><strong>${p.instrument}</strong></td>
            <td class="${p.direction === 'BUY' ? 'direction-buy' : 'direction-sell'}">${p.direction}</td>
            <td>${p.entry_price.toFixed(2)}</td>
            <td>${p.quantity}</td>
            <td>${p.sl_price.toFixed(2)}</td>
            <td>${p.trailing_sl.toFixed(2)}</td>
            <td>${p.target_price.toFixed(2)}</td>
            <td class="${p.pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}">${p.pnl >= 0 ? '+' : ''}${formatCurrency(p.pnl)}</td>
            <td>${p.strategy}</td>
            <td>${p.entry_time}</td>
        </tr>
    `).join("");
}


// ═══════════════════════════════════════════════════════════════════════
//  Trades Table
// ═══════════════════════════════════════════════════════════════════════
function updateTrades(trades) {
    const tbody = document.getElementById("trades-body");
    if (!trades.length) {
        tbody.innerHTML = '<tr class="empty-row"><td colspan="10">No trades yet</td></tr>';
        return;
    }
    tbody.innerHTML = trades.map(t => `
        <tr>
            <td>${t.exit_time}</td>
            <td><strong>${t.instrument}</strong></td>
            <td class="${t.direction === 'BUY' ? 'direction-buy' : 'direction-sell'}">${t.direction}</td>
            <td>${t.entry_price.toFixed(2)}</td>
            <td>${t.exit_price.toFixed(2)}</td>
            <td>${t.quantity}</td>
            <td class="${t.net_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}">${t.net_pnl >= 0 ? '+' : ''}${formatCurrency(t.net_pnl)}</td>
            <td>${t.exit_reason}</td>
            <td>${t.strategy}</td>
            <td>${t.duration_min}m</td>
        </tr>
    `).join("");
}


// ═══════════════════════════════════════════════════════════════════════
//  Strategy Signals
// ═══════════════════════════════════════════════════════════════════════
function updateSignals(signals) {
    const container = document.getElementById("signals-container");
    const keys = Object.keys(signals);
    if (!keys.length) {
        container.innerHTML = '<p class="empty-text">Waiting for signals...</p>';
        return;
    }

    container.innerHTML = keys.map(sym => {
        const s = signals[sym];
        const details = s.details || [];
        return `
        <div class="signal-card">
            <div class="signal-header">
                <span class="signal-symbol">${sym}</span>
                <span class="signal-badge ${s.signal}">${s.signal}</span>
            </div>
            <div class="signal-meta">
                <span class="signal-meta-label">Regime</span>
                <span class="signal-meta-value"><span class="regime-badge regime-${s.regime}">${s.regime}</span></span>
                <span class="signal-meta-label">Micro</span>
                <span class="signal-meta-value">${s.micro_regime || '--'}</span>
                <span class="signal-meta-label">Special Day</span>
                <span class="signal-meta-value">${s.special_day || 'NORMAL'}</span>
                <span class="signal-meta-label">Trend Str.</span>
                <span class="signal-meta-value">${s.trend_strength !== undefined ? s.trend_strength : '--'}</span>
                <span class="signal-meta-label">Vol. %ile</span>
                <span class="signal-meta-value">${s.volatility_percentile !== undefined ? (s.volatility_percentile * 100).toFixed(0) + '%' : '--'}</span>
                <span class="signal-meta-label">Buy Score</span>
                <span class="signal-meta-value">${s.buy_score} (${s.buy_count})</span>
                <span class="signal-meta-label">Sell Score</span>
                <span class="signal-meta-value">${s.sell_score} (${s.sell_count})</span>
                <span class="signal-meta-label">Threshold</span>
                <span class="signal-meta-value">${s.threshold}</span>
            </div>
            ${s.should_reduce_size ? '<div style="color: var(--orange); font-size: 11px; margin-top: 6px;">&#9888; Size reduced (volatile/expiry)</div>' : ''}
            ${s.should_widen_sl ? '<div style="color: var(--orange); font-size: 11px;">&#9888; SL widened</div>' : ''}
            <div class="signal-strategies">
                ${details.map(d => `
                    <div class="signal-strat-row">
                        <span class="signal-strat-name">${d.name}</span>
                        <span class="${d.direction === 'BUY' ? 'direction-buy' : d.direction === 'SELL' ? 'direction-sell' : ''}">${d.direction} (${d.strength})</span>
                    </div>
                `).join("")}
            </div>
        </div>`;
    }).join("");
}


// ═══════════════════════════════════════════════════════════════════════
//  Adaptive Engine
// ═══════════════════════════════════════════════════════════════════════
function updateAdaptive(data) {
    document.getElementById("evo-count").textContent = data.evolution_count || 0;
    document.getElementById("evo-last").textContent = data.last_evolved || "--";
    document.getElementById("evo-trades").textContent = data.trades_today || 0;
    document.getElementById("evo-regime-count").textContent = data.regime_memory_entries || 0;

    // Weights
    const weightsEl = document.getElementById("evo-weights");
    const weights = data.current_weights || {};
    const wKeys = Object.keys(weights);
    if (!wKeys.length) {
        weightsEl.innerHTML = '<p class="empty-text">No evolved weights yet</p>';
    } else {
        const maxW = 2.0;
        weightsEl.innerHTML = wKeys.map(k => {
            const w = weights[k];
            const pct = Math.min((w / maxW) * 100, 100);
            return `
            <div class="weight-bar-row">
                <span class="weight-name">${k}</span>
                <div class="weight-bar-track">
                    <div class="weight-bar-fill" style="width: ${pct}%"></div>
                </div>
                <span class="weight-value">${w.toFixed(2)}</span>
            </div>`;
        }).join("");
    }

    // Params
    const paramsEl = document.getElementById("evo-params");
    const params = data.param_overrides || {};
    const pKeys = Object.keys(params);
    if (!pKeys.length) {
        paramsEl.innerHTML = '<p class="empty-text">No parameter overrides</p>';
    } else {
        paramsEl.innerHTML = pKeys.map(k => `
            <div class="param-row">
                <span>${k}</span>
                <span>${typeof params[k] === 'number' ? params[k].toFixed(2) : params[k]}</span>
            </div>
        `).join("");
    }
}


// ═══════════════════════════════════════════════════════════════════════
//  Bot Logs
// ═══════════════════════════════════════════════════════════════════════
function appendLogLine(timestamp, level, message) {
    const container = document.getElementById("log-container");
    const div = document.createElement("div");
    div.className = `log-line ${level}`;
    div.textContent = `${timestamp} | ${level.padEnd(8)} | ${message}`;
    container.appendChild(div);

    // Keep only last 500 lines
    while (container.children.length > 500) {
        container.removeChild(container.firstChild);
    }

    if (document.getElementById("log-autoscroll").checked) {
        container.scrollTop = container.scrollHeight;
    }
}

document.getElementById("log-refresh").addEventListener("click", async () => {
    try {
        const resp = await fetch("/api/logs");
        const data = await resp.json();
        const container = document.getElementById("log-container");
        container.innerHTML = "";
        (data.lines || []).forEach(line => {
            const div = document.createElement("div");
            let level = "INFO";
            if (line.includes("WARNING")) level = "WARNING";
            else if (line.includes("ERROR")) level = "ERROR";
            else if (line.includes("DEBUG")) level = "DEBUG";
            div.className = `log-line ${level}`;
            div.textContent = line;
            container.appendChild(div);
        });
        container.scrollTop = container.scrollHeight;
    } catch (e) {
        console.error("Failed to load logs:", e);
    }
});


// ═══════════════════════════════════════════════════════════════════════
//  History
// ═══════════════════════════════════════════════════════════════════════
async function loadHistory() {
    try {
        const resp = await fetch("/api/history");
        const data = await resp.json();
        const tbody = document.getElementById("history-body");

        if (!data.length) {
            tbody.innerHTML = '<tr class="empty-row"><td colspan="7">No historical data</td></tr>';
            return;
        }

        tbody.innerHTML = data.map(r => {
            const wr = r.total_trades > 0 ? ((r.wins / r.total_trades) * 100).toFixed(0) + "%" : "--";
            return `
            <tr>
                <td>${r.date}</td>
                <td>${r.total_trades}</td>
                <td style="color: var(--green)">${r.wins}</td>
                <td style="color: var(--red)">${r.losses}</td>
                <td class="${r.net_pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}">${r.net_pnl >= 0 ? '+' : ''}${formatCurrency(r.net_pnl)}</td>
                <td style="color: var(--red)">${formatCurrency(r.max_drawdown)}</td>
                <td>${wr}</td>
            </tr>`;
        }).join("");

        // Draw simple cumulative P&L chart
        drawPnlChart(data);
    } catch (e) {
        console.error("Failed to load history:", e);
    }
}

function drawPnlChart(data) {
    const canvas = document.getElementById("pnl-chart");
    if (!canvas || !data.length) return;
    const ctx = canvas.getContext("2d");
    const w = canvas.width = canvas.parentElement.clientWidth - 32;
    const h = canvas.height = 200;

    ctx.clearRect(0, 0, w, h);

    // Compute cumulative P&L
    let cum = 0;
    const points = data.map(d => { cum += d.net_pnl; return cum; });
    const max = Math.max(...points, 0);
    const min = Math.min(...points, 0);
    const range = max - min || 1;
    const pad = 40;

    // Draw zero line
    const zeroY = pad + ((max - 0) / range) * (h - 2 * pad);
    ctx.strokeStyle = "#30363d";
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(pad, zeroY);
    ctx.lineTo(w - pad, zeroY);
    ctx.stroke();
    ctx.setLineDash([]);

    // Draw line
    ctx.strokeStyle = points[points.length - 1] >= 0 ? "#3fb950" : "#f85149";
    ctx.lineWidth = 2;
    ctx.beginPath();
    points.forEach((p, i) => {
        const x = pad + (i / (points.length - 1 || 1)) * (w - 2 * pad);
        const y = pad + ((max - p) / range) * (h - 2 * pad);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();

    // Fill area
    const lastX = pad + ((points.length - 1) / (points.length - 1 || 1)) * (w - 2 * pad);
    ctx.lineTo(lastX, zeroY);
    ctx.lineTo(pad, zeroY);
    ctx.closePath();
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    if (points[points.length - 1] >= 0) {
        grad.addColorStop(0, "rgba(63,185,80,0.15)");
        grad.addColorStop(1, "rgba(63,185,80,0)");
    } else {
        grad.addColorStop(0, "rgba(248,81,73,0)");
        grad.addColorStop(1, "rgba(248,81,73,0.15)");
    }
    ctx.fillStyle = grad;
    ctx.fill();

    // Labels
    ctx.fillStyle = "#8b949e";
    ctx.font = "11px -apple-system, sans-serif";
    ctx.textAlign = "right";
    ctx.fillText(formatCurrency(max), pad - 6, pad + 4);
    ctx.fillText(formatCurrency(min), pad - 6, h - pad + 4);
    ctx.fillText("0", pad - 6, zeroY + 4);

    // Date labels
    ctx.textAlign = "center";
    if (data.length > 0) {
        ctx.fillText(data[0].date, pad, h - 8);
        ctx.fillText(data[data.length - 1].date, w - pad, h - 8);
    }
}


// ═══════════════════════════════════════════════════════════════════════
//  Toast Notifications
// ═══════════════════════════════════════════════════════════════════════
let toastContainer = null;

function showTradeToast(data) {
    if (!toastContainer) {
        toastContainer = document.createElement("div");
        toastContainer.className = "toast-container";
        document.body.appendChild(toastContainer);
    }

    const toast = document.createElement("div");
    const trade = data.trade || {};
    const isEntry = data.type === "entry";
    toast.className = `toast ${isEntry ? 'trade-entry' : 'trade-exit'}`;

    if (isEntry) {
        toast.innerHTML = `<strong>${trade.direction} ${trade.instrument}</strong><br>
            Entry: ${trade.entry_price} | Qty: ${trade.quantity} | Strategy: ${trade.strategy}`;
    } else {
        toast.innerHTML = `<strong>Closed ${trade.instrument}</strong><br>
            P&L: ${trade.net_pnl >= 0 ? '+' : ''}${formatCurrency(trade.net_pnl)} | Reason: ${trade.exit_reason}`;
    }

    toastContainer.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = "0";
        toast.style.transition = "opacity 0.5s";
        setTimeout(() => toast.remove(), 500);
    }, 5000);
}


// ═══════════════════════════════════════════════════════════════════════
//  Emergency Square-Off
// ═══════════════════════════════════════════════════════════════════════
async function emergencySquareOff() {
    if (!confirm("EMERGENCY SQUARE-OFF\n\nThis will immediately close ALL open positions.\n\nAre you sure?")) {
        return;
    }
    try {
        const resp = await fetch("/api/emergency-squareoff", { method: "POST" });
        const data = await resp.json();
        alert(data.message || "Square-off triggered");
    } catch (e) {
        alert("Failed to trigger square-off: " + e);
    }
}


// ═══════════════════════════════════════════════════════════════════════
//  Tab Navigation
// ═══════════════════════════════════════════════════════════════════════
document.querySelectorAll(".tab").forEach(tab => {
    tab.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
        document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
        tab.classList.add("active");
        document.getElementById("tab-" + tab.dataset.tab).classList.add("active");

        // Load history data when switching to history tab
        if (tab.dataset.tab === "history") loadHistory();
        // Load logs when switching to logs tab
        if (tab.dataset.tab === "logs") document.getElementById("log-refresh").click();
    });
});


// ═══════════════════════════════════════════════════════════════════════
//  Polling Fallback (in case SocketIO disconnects)
// ═══════════════════════════════════════════════════════════════════════
setInterval(async () => {
    if (!socket.connected) {
        try {
            const resp = await fetch("/api/state");
            const data = await resp.json();
            updatePnlCards(data);
            updateRiskMeter(data.risk || {});
            updatePositions(data.open_positions || []);
            updateTrades(data.closed_trades || []);
            updateSignals(data.signals || {});
            updateAdaptive(data.adaptive || {});
            updateBotStatus(data.bot_running, data.feed || {});
        } catch (e) {
            // Server might be down
        }
    }
}, 5000);

// Request initial update
socket.emit("request_update");
