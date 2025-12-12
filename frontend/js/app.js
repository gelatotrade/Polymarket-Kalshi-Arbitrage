/**
 * Polymarket-Kalshi Arbitrage Terminal
 * Frontend Application
 */

// ============================================
// State Management
// ============================================

const state = {
    socket: null,
    connected: false,
    wallets: {
        kalshi: { connected: false, address: null },
        polymarket: { connected: false, address: null, provider: null }
    },
    markets: [],
    opportunities: [],
    trades: [],
    stats: {
        totalPnl: 0,
        winRate: 0,
        totalTrades: 0,
        avgProfit: 0
    },
    scanning: false,
    lastUpdate: null
};

// ============================================
// Socket.IO Connection
// ============================================

function initializeSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socketUrl = `${window.location.protocol}//${window.location.host}`;

    state.socket = io(socketUrl, {
        transports: ['websocket', 'polling'],
        reconnection: true,
        reconnectionDelay: 1000,
        reconnectionAttempts: 10
    });

    // Connection events
    state.socket.on('connect', () => {
        state.connected = true;
        updateConnectionStatus('connected');
        log('Connected to server', 'success');

        // Subscribe to updates
        state.socket.emit('subscribe_markets');
        state.socket.emit('subscribe_opportunities');
        state.socket.emit('subscribe_trades');
    });

    state.socket.on('disconnect', () => {
        state.connected = false;
        updateConnectionStatus('disconnected');
        log('Disconnected from server', 'error');
    });

    state.socket.on('connect_error', (error) => {
        updateConnectionStatus('error');
        log(`Connection error: ${error.message}`, 'error');
    });

    // Data events
    state.socket.on('markets_update', (data) => {
        state.markets = data.markets || [];
        updateMarketsTable();
        document.getElementById('marketsCount').textContent = data.count || 0;
    });

    state.socket.on('opportunities_update', (data) => {
        state.opportunities = data.opportunities || [];
        state.stats = data.stats || state.stats;
        updateOpportunitiesDisplay();
        updateStats();
        document.getElementById('oppsCount').textContent = data.count || 0;
    });

    state.socket.on('trade_executed', (data) => {
        state.trades.unshift(data.trade);
        updateTradesList();
        log(`Trade executed: ${data.trade.id}`, 'trade');
    });

    state.socket.on('execution_completed', (data) => {
        updateTradesList();
        updateStats(data.stats);
        log(`Trade completed: ${data.trade.id} - Profit: $${data.trade.realized_profit.toFixed(2)}`, 'success');
    });

    state.socket.on('execution_error', (data) => {
        log(`Execution failed: ${data.error}`, 'error');
    });

    // Scan events
    state.socket.on('scan_progress', (data) => {
        updateScanProgress(data);
    });

    state.socket.on('scan_complete', (data) => {
        state.scanning = false;
        state.lastUpdate = data.last_update;
        updateScanButton(false);
        updateLastUpdate();
        log(`Scan complete: ${data.matched_markets} markets matched, ${data.opportunities} opportunities found`, 'success');
    });

    state.socket.on('scan_error', (data) => {
        state.scanning = false;
        updateScanButton(false);
        log(`Scan error: ${data.error}`, 'error');
    });

    // Wallet events
    state.socket.on('wallet_connected', (data) => {
        log(`Wallet connected: ${data.address.slice(0, 8)}...${data.address.slice(-6)} (${data.platform})`, 'success');
    });
}

// ============================================
// Connection Status
// ============================================

function updateConnectionStatus(status) {
    const indicator = document.getElementById('connectionStatus');
    const statusText = indicator.querySelector('.status-text');

    indicator.classList.remove('connected', 'error');

    switch (status) {
        case 'connected':
            indicator.classList.add('connected');
            statusText.textContent = 'Connected';
            break;
        case 'disconnected':
            statusText.textContent = 'Disconnected';
            break;
        case 'error':
            indicator.classList.add('error');
            statusText.textContent = 'Error';
            break;
        default:
            statusText.textContent = 'Connecting...';
    }
}

// ============================================
// Wallet Connection
// ============================================

async function connectWeb3Wallet() {
    if (typeof window.ethereum === 'undefined') {
        log('MetaMask not detected. Please install MetaMask.', 'error');
        alert('MetaMask not detected. Please install MetaMask to connect your Polymarket wallet.');
        return;
    }

    try {
        // Request account access
        const accounts = await window.ethereum.request({
            method: 'eth_requestAccounts'
        });

        const address = accounts[0];
        const chainId = await window.ethereum.request({ method: 'eth_chainId' });

        // Check if on Polygon network (chainId 137 = 0x89)
        if (chainId !== '0x89') {
            log('Please switch to Polygon network', 'warning');

            try {
                await window.ethereum.request({
                    method: 'wallet_switchEthereumChain',
                    params: [{ chainId: '0x89' }]
                });
            } catch (switchError) {
                // Chain not added, add it
                if (switchError.code === 4902) {
                    await window.ethereum.request({
                        method: 'wallet_addEthereumChain',
                        params: [{
                            chainId: '0x89',
                            chainName: 'Polygon Mainnet',
                            nativeCurrency: {
                                name: 'MATIC',
                                symbol: 'MATIC',
                                decimals: 18
                            },
                            rpcUrls: ['https://polygon-rpc.com/'],
                            blockExplorerUrls: ['https://polygonscan.com/']
                        }]
                    });
                }
            }
        }

        // Update state
        state.wallets.polymarket = {
            connected: true,
            address: address,
            provider: new ethers.providers.Web3Provider(window.ethereum)
        };

        // Update UI
        const btn = document.getElementById('connectPolymarket');
        btn.textContent = 'Connected';
        btn.classList.add('connected');

        const addrSpan = document.getElementById('polymarketAddress');
        addrSpan.textContent = `${address.slice(0, 6)}...${address.slice(-4)}`;

        // Notify server
        state.socket.emit('wallet_connect', {
            address: address,
            chainId: parseInt(chainId, 16),
            platform: 'polymarket'
        });

        log(`Polymarket wallet connected: ${address.slice(0, 8)}...`, 'success');

        // Listen for account changes
        window.ethereum.on('accountsChanged', (accounts) => {
            if (accounts.length === 0) {
                disconnectPolymarketWallet();
            } else {
                state.wallets.polymarket.address = accounts[0];
                addrSpan.textContent = `${accounts[0].slice(0, 6)}...${accounts[0].slice(-4)}`;
            }
        });

    } catch (error) {
        log(`Wallet connection failed: ${error.message}`, 'error');
    }
}

function disconnectPolymarketWallet() {
    const address = state.wallets.polymarket.address;

    state.wallets.polymarket = {
        connected: false,
        address: null,
        provider: null
    };

    const btn = document.getElementById('connectPolymarket');
    btn.textContent = 'Connect Wallet';
    btn.classList.remove('connected');

    document.getElementById('polymarketAddress').textContent = '';

    if (state.socket && address) {
        state.socket.emit('wallet_disconnect', { address });
    }

    log('Polymarket wallet disconnected', 'system');
}

function connectKalshiWallet() {
    // Kalshi uses API key authentication, show modal or redirect
    const modal = document.getElementById('tradeModal');
    const modalBody = document.getElementById('modalBody');

    modalBody.innerHTML = `
        <div style="text-align: center; padding: 20px;">
            <p style="margin-bottom: 16px;">Kalshi uses API key authentication.</p>
            <p style="color: var(--text-muted); font-size: 11px; margin-bottom: 16px;">
                Configure your API credentials in the .env file or enter them below:
            </p>
            <div style="display: flex; flex-direction: column; gap: 12px; max-width: 300px; margin: 0 auto;">
                <input type="text" id="kalshiApiKey" placeholder="API Key"
                    style="padding: 8px; background: var(--bg-tertiary); border: 1px solid var(--border-primary);
                           border-radius: 4px; color: var(--text-primary); font-family: var(--font-mono);">
                <input type="password" id="kalshiApiSecret" placeholder="API Secret"
                    style="padding: 8px; background: var(--bg-tertiary); border: 1px solid var(--border-primary);
                           border-radius: 4px; color: var(--text-primary); font-family: var(--font-mono);">
            </div>
        </div>
    `;

    document.getElementById('confirmTradeBtn').textContent = 'Connect';
    document.getElementById('confirmTradeBtn').onclick = () => {
        const apiKey = document.getElementById('kalshiApiKey').value;
        const apiSecret = document.getElementById('kalshiApiSecret').value;

        if (apiKey && apiSecret) {
            state.wallets.kalshi = {
                connected: true,
                address: apiKey.slice(0, 8) + '...'
            };

            const btn = document.getElementById('connectKalshi');
            btn.textContent = 'Connected';
            btn.classList.add('connected');
            document.getElementById('kalshiAddress').textContent = state.wallets.kalshi.address;

            state.socket.emit('wallet_connect', {
                address: apiKey,
                platform: 'kalshi'
            });

            log('Kalshi account connected', 'success');
            closeModal();
        }
    };

    modal.classList.add('active');
}

// ============================================
// Market Scanning
// ============================================

function startScan() {
    if (state.scanning) return;

    state.scanning = true;
    updateScanButton(true);

    log('Starting market scan...', 'system');
    state.socket.emit('start_scan');
}

function updateScanButton(scanning) {
    const btn = document.getElementById('scanBtn');

    if (scanning) {
        btn.disabled = true;
        btn.innerHTML = '<span class="btn-icon">&#x23F3;</span> Scanning...';
    } else {
        btn.disabled = false;
        btn.innerHTML = '<span class="btn-icon">&#x25B6;</span> Start Scan';
    }
}

function updateScanProgress(data) {
    const progressFill = document.getElementById('progressFill');
    const progressText = document.getElementById('progressText');

    progressFill.style.width = `${data.progress}%`;
    progressText.textContent = data.message || data.stage;
}

// ============================================
// Data Display
// ============================================

function updateMarketsTable() {
    const tbody = document.getElementById('marketsBody');

    if (state.markets.length === 0) {
        tbody.innerHTML = `
            <tr class="empty-row">
                <td colspan="4">No markets loaded. Click "Start Scan" to begin.</td>
            </tr>
        `;
        return;
    }

    tbody.innerHTML = state.markets.slice(0, 50).map(market => {
        const kalshiYes = market.kalshi?.yes_ask || 0;
        const polyYes = market.polymarket?.yes_price || 0;
        const spread = (polyYes - kalshiYes).toFixed(3);
        const spreadClass = spread > 0 ? 'price-positive' : spread < 0 ? 'price-negative' : '';

        return `
            <tr>
                <td title="${market.kalshi?.title || ''}">${truncate(market.kalshi?.title || 'Unknown', 30)}</td>
                <td>${kalshiYes.toFixed(2)}</td>
                <td>${polyYes.toFixed(2)}</td>
                <td class="${spreadClass}">${spread}</td>
            </tr>
        `;
    }).join('');
}

function updateOpportunitiesDisplay() {
    const container = document.getElementById('opportunitiesContainer');
    const emptyState = document.getElementById('emptyOpps');

    if (state.opportunities.length === 0) {
        container.innerHTML = '';
        container.appendChild(emptyState);
        emptyState.style.display = 'flex';
        return;
    }

    emptyState.style.display = 'none';

    container.innerHTML = state.opportunities.slice(0, 20).map(opp => `
        <div class="opportunity-card" data-id="${opp.id}">
            <div class="opp-header">
                <span class="opp-id">${opp.id}</span>
                <span class="opp-profit ${opp.net_profit_pct < 0 ? 'negative' : ''}">
                    ${opp.net_profit_pct >= 0 ? '+' : ''}${opp.net_profit_pct.toFixed(2)}%
                </span>
            </div>
            <div class="opp-title">${truncate(opp.kalshi_title || opp.polymarket_question, 60)}</div>
            <div class="opp-details">
                <div class="opp-side">
                    <span class="opp-side-label">BUY</span>
                    <span class="opp-platform">${opp.buy_platform.toUpperCase()}</span>
                    <span class="opp-price">${opp.buy_side.toUpperCase()} @ $${opp.buy_price.toFixed(2)}</span>
                </div>
                <div class="opp-side">
                    <span class="opp-side-label">SELL</span>
                    <span class="opp-platform">${opp.sell_platform.toUpperCase()}</span>
                    <span class="opp-price">${opp.sell_side.toUpperCase()} @ $${opp.sell_price.toFixed(2)}</span>
                </div>
            </div>
            <div class="opp-metrics">
                <span>Confidence: ${opp.confidence.toFixed(0)}%</span>
                <span>Risk: ${opp.risk_score.toFixed(0)}</span>
                <span>Spread: $${opp.spread.toFixed(3)}</span>
            </div>
            <div class="opp-actions">
                <button class="execute-btn" onclick="executeOpportunity('${opp.id}')"
                    ${!canTrade() ? 'disabled' : ''}>
                    Execute Trade
                </button>
            </div>
        </div>
    `).join('');
}

function updateTradesList() {
    const list = document.getElementById('tradesList');

    if (state.trades.length === 0) {
        list.innerHTML = '<div class="empty-state"><p>No trades executed yet.</p></div>';
        return;
    }

    list.innerHTML = state.trades.slice(0, 20).map(trade => `
        <div class="trade-item">
            <div class="trade-info">
                <span class="trade-id">${trade.id}</span>
                <span class="trade-market">${trade.status}</span>
            </div>
            <span class="trade-profit ${trade.realized_profit >= 0 ? 'positive' : 'negative'}">
                ${trade.realized_profit >= 0 ? '+' : ''}$${trade.realized_profit.toFixed(2)}
            </span>
        </div>
    `).join('');
}

function updateStats(stats = null) {
    if (stats) {
        state.stats = stats;
    }

    document.getElementById('totalPnl').textContent = `$${state.stats.total_pnl?.toFixed(2) || '0.00'}`;
    document.getElementById('totalPnl').className = `stat-value ${state.stats.total_pnl >= 0 ? 'positive' : 'negative'}`;

    document.getElementById('winRate').textContent = `${state.stats.win_rate?.toFixed(1) || '0'}%`;
    document.getElementById('totalTrades').textContent = state.stats.total_trades || 0;
    document.getElementById('avgProfit').textContent = `${state.stats.avg_profit_pct?.toFixed(2) || '0'}%`;
}

function updateLastUpdate() {
    const el = document.getElementById('lastUpdate');
    if (state.lastUpdate) {
        const date = new Date(state.lastUpdate);
        el.textContent = `Last update: ${date.toLocaleTimeString()}`;
    }
}

// ============================================
// Trading
// ============================================

function canTrade() {
    return state.wallets.kalshi.connected || state.wallets.polymarket.connected;
}

function executeOpportunity(oppId) {
    const opp = state.opportunities.find(o => o.id === oppId);
    if (!opp) return;

    // Show confirmation modal
    const modal = document.getElementById('tradeModal');
    const modalBody = document.getElementById('modalBody');

    modalBody.innerHTML = `
        <div class="trade-confirmation">
            <h4 style="margin-bottom: 16px; color: var(--text-primary);">
                ${truncate(opp.kalshi_title || opp.polymarket_question, 50)}
            </h4>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px;">
                <div style="padding: 12px; background: var(--bg-tertiary); border-radius: 8px;">
                    <div style="font-size: 10px; color: var(--text-muted); margin-bottom: 4px;">BUY</div>
                    <div style="color: var(--accent-cyan);">${opp.buy_platform.toUpperCase()}</div>
                    <div>${opp.buy_side.toUpperCase()} @ $${opp.buy_price.toFixed(2)}</div>
                </div>
                <div style="padding: 12px; background: var(--bg-tertiary); border-radius: 8px;">
                    <div style="font-size: 10px; color: var(--text-muted); margin-bottom: 4px;">SELL</div>
                    <div style="color: var(--accent-cyan);">${opp.sell_platform.toUpperCase()}</div>
                    <div>${opp.sell_side.toUpperCase()} @ $${opp.sell_price.toFixed(2)}</div>
                </div>
            </div>
            <div style="display: flex; justify-content: space-between; padding: 12px; background: var(--bg-primary); border-radius: 8px;">
                <span>Expected Profit:</span>
                <span style="color: var(--accent-green); font-weight: 600;">
                    +${opp.net_profit_pct.toFixed(2)}%
                </span>
            </div>
            <div style="margin-top: 16px;">
                <label style="display: block; margin-bottom: 8px; color: var(--text-muted); font-size: 11px;">
                    Trade Size (USD):
                </label>
                <input type="number" id="tradeSize" value="100" min="1" max="1000" step="10"
                    style="width: 100%; padding: 8px; background: var(--bg-tertiary);
                           border: 1px solid var(--border-primary); border-radius: 4px;
                           color: var(--text-primary); font-family: var(--font-mono);">
            </div>
        </div>
    `;

    document.getElementById('confirmTradeBtn').textContent = 'Execute Trade';
    document.getElementById('confirmTradeBtn').onclick = () => {
        const size = parseFloat(document.getElementById('tradeSize').value) || 100;
        confirmTrade(oppId, size);
    };

    modal.classList.add('active');
}

function confirmTrade(oppId, size) {
    closeModal();

    log(`Executing trade for opportunity ${oppId}...`, 'trade');

    state.socket.emit('execute_opportunity', {
        opportunity_id: oppId,
        size_usd: size
    });
}

function closeModal() {
    document.getElementById('tradeModal').classList.remove('active');
}

// ============================================
// Filters
// ============================================

function applyFilters() {
    const minProfit = parseFloat(document.getElementById('minProfitFilter').value) || 0;
    const maxRisk = parseFloat(document.getElementById('maxRiskFilter').value) || 100;

    const filtered = state.opportunities.filter(opp =>
        opp.net_profit_pct >= minProfit && opp.risk_score <= maxRisk
    );

    // Temporarily update display with filtered results
    const container = document.getElementById('opportunitiesContainer');
    const emptyState = document.getElementById('emptyOpps');

    if (filtered.length === 0) {
        container.innerHTML = '';
        const empty = emptyState.cloneNode(true);
        empty.querySelector('p').textContent = 'No opportunities match filters.';
        empty.style.display = 'flex';
        container.appendChild(empty);
        return;
    }

    // Re-render with filtered data (reuse the update function logic)
    const originalOpps = state.opportunities;
    state.opportunities = filtered;
    updateOpportunitiesDisplay();
    state.opportunities = originalOpps;

    document.getElementById('oppsCount').textContent = filtered.length;
}

// ============================================
// Terminal Logging
// ============================================

function log(message, type = 'system') {
    const terminal = document.getElementById('terminalOutput');
    const timestamp = new Date().toLocaleTimeString('en-US', { hour12: false });

    const line = document.createElement('div');
    line.className = `terminal-line ${type}`;
    line.innerHTML = `
        <span class="timestamp">[${timestamp}]</span>
        <span class="message">${escapeHtml(message)}</span>
    `;

    terminal.appendChild(line);
    terminal.scrollTop = terminal.scrollHeight;

    // Keep only last 100 lines
    while (terminal.children.length > 100) {
        terminal.removeChild(terminal.firstChild);
    }
}

function clearTerminal() {
    const terminal = document.getElementById('terminalOutput');
    terminal.innerHTML = `
        <div class="terminal-line system">
            <span class="timestamp">[${new Date().toLocaleTimeString('en-US', { hour12: false })}]</span>
            <span class="message">Terminal cleared.</span>
        </div>
    `;
}

// ============================================
// Utilities
// ============================================

function truncate(str, len) {
    if (!str) return '';
    return str.length > len ? str.slice(0, len) + '...' : str;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function refreshData() {
    if (!state.connected) {
        log('Not connected to server', 'error');
        return;
    }

    log('Refreshing data...', 'system');
    state.socket.emit('subscribe_markets');
    state.socket.emit('subscribe_opportunities');
    state.socket.emit('subscribe_trades');
}

function updateClock() {
    const el = document.getElementById('currentTime');
    el.textContent = new Date().toLocaleTimeString('en-US', { hour12: false });
}

// ============================================
// Initialization
// ============================================

document.addEventListener('DOMContentLoaded', () => {
    // Initialize socket connection
    initializeSocket();

    // Start clock
    updateClock();
    setInterval(updateClock, 1000);

    // Log startup
    log('Terminal initialized. Connecting to server...', 'system');

    // Check for Web3 provider
    if (typeof window.ethereum !== 'undefined') {
        log('Web3 provider detected (MetaMask)', 'system');
    } else {
        log('No Web3 provider detected. Install MetaMask for Polymarket trading.', 'warning');
    }

    // Close modal on outside click
    document.getElementById('tradeModal').addEventListener('click', (e) => {
        if (e.target.classList.contains('modal')) {
            closeModal();
        }
    });

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeModal();
        }
        if (e.ctrlKey && e.key === 's') {
            e.preventDefault();
            startScan();
        }
    });
});

// Handle page visibility
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && state.socket) {
        // Reconnect if needed
        if (!state.socket.connected) {
            state.socket.connect();
        }
    }
});

// Export for debugging
window.arbitrage = {
    state,
    log,
    startScan,
    refreshData
};
