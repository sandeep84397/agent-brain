// ============================================================
// Agent Brain Office — Pixel Art Dashboard
// Real-time visualization of agent team activity
// ============================================================

// --- Configuration ---
const SCALE = 3;
const GAME_W = 320;
const GAME_H = 213;
const CANVAS_W = GAME_W * SCALE;   // 960
const CANVAS_H = GAME_H * SCALE;   // 639
const MOVE_SPEED = 1.5;
const IDLE_TIMEOUT_MS = 120000;     // 2 min without heartbeat → offline

// --- Color Palette (PICO-8 inspired) ---
const PAL = {
    black:      '#0a0a1a',
    darkBlue:   '#16213e',
    navy:       '#0f3460',
    red:        '#e94560',
    floor1:     '#22223a',
    floor2:     '#1e1e34',
    wall:       '#1a1a30',
    wallTop:    '#141428',
    wallAccent: '#2a2a4a',
    desk:       '#6b5b45',
    deskTop:    '#7d6b52',
    monitor:    '#0f3460',
    screen:     '#29ADFF',
    screenGlow: 'rgba(41,173,255,0.15)',
    plant:      '#2d6b3f',
    plantLight: '#3a8a50',
    pot:        '#8B6340',
    chair:      '#3a3a4e',
    white:      '#e0e0e0',
    shadow:     'rgba(0,0,0,0.25)',
    carpet:     '#1a1a3a',
};

// --- Role Colors ---
const ROLE_COLORS = {
    'project-manager':    { hair: '#2c1810', shirt: '#29ADFF', skin: '#FFCCAA', pants: '#2a2a5c', tag: '#29ADFF', label: 'PM' },
    'product-owner':      { hair: '#4a1530', shirt: '#FF77A8', skin: '#FFCCAA', pants: '#2a2a5c', tag: '#FF77A8', label: 'PO' },
    'principal-engineer': { hair: '#3a2010', shirt: '#e94560', skin: '#FFCCAA', pants: '#2a2a5c', tag: '#e94560', label: 'PE' },
    'backend-engineer':   { hair: '#1a1a30', shirt: '#008751', skin: '#FFCCAA', pants: '#2a2a5c', tag: '#008751', label: 'BE' },
    'frontend-engineer':  { hair: '#4a2010', shirt: '#FFA300', skin: '#FFCCAA', pants: '#2a2a5c', tag: '#FFA300', label: 'FE' },
    'qa-engineer':        { hair: '#1a1a30', shirt: '#7E2553', skin: '#FFCCAA', pants: '#2a2a5c', tag: '#7E2553', label: 'QA' },
    'unknown':            { hair: '#333',    shirt: '#555',    skin: '#FFCCAA', pants: '#2a2a5c', tag: '#666',    label: '??' },
};

// --- Desk Layout (game coords — where the desk center is) ---
const DESK_POSITIONS = [
    { role: 'project-manager',    x: 50,  y: 50,  side: 'left'  },
    { role: 'principal-engineer', x: 250, y: 50,  side: 'right' },
    { role: 'product-owner',      x: 50,  y: 100, side: 'left'  },
    { role: 'qa-engineer',        x: 250, y: 100, side: 'right' },
    { role: 'backend-engineer',   x: 50,  y: 150, side: 'left'  },
    { role: 'frontend-engineer',  x: 250, y: 150, side: 'right' },
];

const MEETING = { x: 150, y: 100 };
const COFFEE  = { x: 150, y: 170 };

// Discussion slots around meeting table
const DISC_SLOTS = [
    [{ x: -18, y: -3 }, { x: 12, y: -3 }],
    [{ x: -18, y: 10 }, { x: 12, y: 10 }],
    [{ x: -3,  y: -12}, { x: -3, y: 15 }],
];

// --- Status Colors ---
const STATUS_COLORS = {
    working:    '#00E436',
    planning:   '#FFEC27',
    reviewing:  '#FFA300',
    discussing: '#29ADFF',
    blocked:    '#FF004D',
    waiting:    '#FFEC27',
    idle:       '#555555',
    offline:    '#333333',
};

// ============================================================
// STATE
// ============================================================
let agents = {};
let messages = [];
let frameCount = 0;
let bgCanvas = null;
let connected = false;
let lastStateTime = 0;

// ============================================================
// CANVAS SETUP
// ============================================================
const canvas = document.getElementById('office');
const ctx = canvas.getContext('2d');
canvas.width = CANVAS_W;
canvas.height = CANVAS_H;
ctx.imageSmoothingEnabled = false;

// Force background redraw when pixel font loads
if (document.fonts) {
    document.fonts.ready.then(() => { bgCanvas = null; });
}

// ============================================================
// DRAWING HELPERS
// ============================================================

/** Draw rectangle in game coordinates */
function gRect(context, gx, gy, gw, gh) {
    context.fillRect(
        Math.round(gx * SCALE),
        Math.round(gy * SCALE),
        Math.round(gw * SCALE),
        Math.round(gh * SCALE)
    );
}

/** Draw rectangle on main ctx in game coords */
function fRect(gx, gy, gw, gh) {
    gRect(ctx, gx, gy, gw, gh);
}

// ============================================================
// DRAWING: Background (cached to offscreen canvas)
// ============================================================

function ensureBackground() {
    if (bgCanvas) return;

    bgCanvas = document.createElement('canvas');
    bgCanvas.width = CANVAS_W;
    bgCanvas.height = CANVAS_H;
    const bg = bgCanvas.getContext('2d');
    bg.imageSmoothingEnabled = false;

    // Floor — subtle checkerboard
    for (let gy = 0; gy < GAME_H; gy += 8) {
        for (let gx = 0; gx < GAME_W; gx += 8) {
            bg.fillStyle = ((gx / 8 + gy / 8) % 2 === 0) ? PAL.floor1 : PAL.floor2;
            gRect(bg, gx, gy, 8, 8);
        }
    }

    // Center carpet
    bg.fillStyle = PAL.carpet;
    gRect(bg, 100, 35, 120, 145);

    // Walls
    bg.fillStyle = PAL.wallTop;
    gRect(bg, 0, 0, GAME_W, 12);
    bg.fillStyle = PAL.wall;
    gRect(bg, 0, 12, GAME_W, 8);
    bg.fillStyle = PAL.wallAccent;
    gRect(bg, 0, 19, GAME_W, 1);

    // Title on wall
    bg.fillStyle = PAL.white;
    bg.font = `${7 * SCALE}px 'Press Start 2P', monospace`;
    bg.textAlign = 'center';
    bg.fillText('AGENT BRAIN', CANVAS_W / 2, 9 * SCALE);
    bg.fillStyle = PAL.red;
    bg.font = `${3 * SCALE}px 'Press Start 2P', monospace`;
    bg.fillText('OFFICE', CANVAS_W / 2, 16 * SCALE);

    // Desks
    for (const desk of DESK_POSITIONS) {
        drawDesk(bg, desk.x, desk.y, desk.role);
    }

    // Meeting table
    bg.fillStyle = '#5a4a35';
    gRect(bg, MEETING.x - 15, MEETING.y - 2, 30, 16);
    bg.fillStyle = PAL.desk;
    gRect(bg, MEETING.x - 14, MEETING.y - 1, 28, 14);
    bg.fillStyle = PAL.deskTop;
    gRect(bg, MEETING.x - 13, MEETING.y, 26, 12);
    // "MEETING" text on table
    bg.fillStyle = '#5a4a35';
    bg.font = `${2 * SCALE}px 'Press Start 2P', monospace`;
    bg.textAlign = 'center';
    bg.fillText('MEETING', MEETING.x * SCALE, (MEETING.y + 8) * SCALE);

    // Plants
    drawPlant(bg, 15, 28);
    drawPlant(bg, 285, 28);
    drawPlant(bg, 15, 180);
    drawPlant(bg, 285, 180);

    // Coffee machine
    bg.fillStyle = '#444';
    gRect(bg, COFFEE.x - 4, COFFEE.y, 8, 10);
    bg.fillStyle = '#666';
    gRect(bg, COFFEE.x - 3, COFFEE.y + 1, 6, 4);
    bg.fillStyle = PAL.red;
    gRect(bg, COFFEE.x - 1, COFFEE.y + 6, 2, 2);
    bg.fillStyle = '#555';
    bg.font = `${1.5 * SCALE}px 'Press Start 2P', monospace`;
    bg.textAlign = 'center';
    bg.fillText('COFFEE', COFFEE.x * SCALE, (COFFEE.y + 13) * SCALE);

    // Whiteboard
    bg.fillStyle = '#ddd';
    gRect(bg, 120, 22, 80, 10);
    bg.fillStyle = '#f5f5f0';
    gRect(bg, 121, 23, 78, 8);
    bg.fillStyle = '#aaa';
    bg.font = `${2 * SCALE}px 'Press Start 2P', monospace`;
    bg.textAlign = 'center';
    bg.fillText('Sprint Board', 160 * SCALE, 28 * SCALE);
}

function drawDesk(bg, gx, gy, role) {
    const colors = ROLE_COLORS[role] || ROLE_COLORS['unknown'];

    // Desk surface
    bg.fillStyle = '#5a4a35';
    gRect(bg, gx - 6, gy - 6, 20, 14);
    bg.fillStyle = PAL.desk;
    gRect(bg, gx - 5, gy - 5, 18, 12);
    bg.fillStyle = PAL.deskTop;
    gRect(bg, gx - 4, gy - 4, 16, 10);

    // Monitor
    bg.fillStyle = PAL.monitor;
    gRect(bg, gx - 1, gy - 10, 10, 7);
    bg.fillStyle = PAL.screen;
    gRect(bg, gx, gy - 9, 8, 5);

    // Screen glow
    bg.fillStyle = PAL.screenGlow;
    gRect(bg, gx - 3, gy - 12, 14, 11);

    // Monitor stand
    bg.fillStyle = '#444';
    gRect(bg, gx + 3, gy - 3, 2, 2);

    // Chair
    bg.fillStyle = PAL.chair;
    gRect(bg, gx + 1, gy + 9, 6, 5);
    bg.fillStyle = '#2a2a3e';
    gRect(bg, gx + 2, gy + 10, 4, 3);

    // Role color strip on desk edge
    bg.fillStyle = colors.tag;
    gRect(bg, gx - 6, gy + 7, 20, 1);

    // Role label
    bg.fillStyle = colors.tag;
    bg.font = `${2 * SCALE}px 'Press Start 2P', monospace`;
    bg.textAlign = 'center';
    bg.fillText(colors.label, (gx + 4) * SCALE, (gy + 18) * SCALE);
}

function drawPlant(bg, gx, gy) {
    // Pot
    bg.fillStyle = PAL.pot;
    gRect(bg, gx + 1, gy + 7, 6, 5);
    bg.fillStyle = '#7a5530';
    gRect(bg, gx, gy + 6, 8, 2);

    // Leaves
    bg.fillStyle = PAL.plant;
    gRect(bg, gx + 2, gy, 4, 7);
    gRect(bg, gx, gy + 2, 8, 3);
    bg.fillStyle = PAL.plantLight;
    gRect(bg, gx + 3, gy + 1, 2, 5);
}

// ============================================================
// DRAWING: Characters
// ============================================================

function drawCharacter(x, y, colors, anim, frame) {
    const bob = (anim === 'idle') ? Math.sin(frame * 0.08) * 0.5 : 0;
    const sy = y + bob;

    // Shadow
    ctx.fillStyle = PAL.shadow;
    fRect(x - 1, y + 16, 10, 2);

    // Legs
    ctx.fillStyle = colors.pants;
    if (anim === 'walking') {
        const step = Math.floor(frame / 8) % 4;
        const offL = [0, -1, 0, 1][step];
        const offR = [0, 1, 0, -1][step];
        fRect(x + 2, sy + 13 + offL, 2, 3);
        fRect(x + 5, sy + 13 + offR, 2, 3);
    } else {
        fRect(x + 2, sy + 13, 2, 3);
        fRect(x + 5, sy + 13, 2, 3);
    }

    // Feet
    ctx.fillStyle = '#2a2a3e';
    fRect(x + 1, sy + 15, 3, 1);
    fRect(x + 5, sy + 15, 3, 1);

    // Body / shirt
    ctx.fillStyle = colors.shirt;
    fRect(x + 1, sy + 7, 6, 6);

    // Arms
    if (anim === 'working') {
        const armBob = Math.floor(frame / 12) % 2;
        ctx.fillStyle = colors.skin;
        fRect(x - 1, sy + 8 + armBob, 2, 3);
        fRect(x + 7, sy + 9 - armBob, 2, 3);
    } else if (anim === 'discussing') {
        const wave = Math.floor(frame / 20) % 2;
        ctx.fillStyle = colors.skin;
        fRect(x - 1, sy + 7 - wave, 2, 3);
        fRect(x + 7, sy + 8, 2, 3);
    } else {
        ctx.fillStyle = colors.skin;
        fRect(x, sy + 8, 1, 4);
        fRect(x + 7, sy + 8, 1, 4);
    }

    // Head
    ctx.fillStyle = colors.skin;
    fRect(x + 1, sy + 1, 6, 6);

    // Hair
    ctx.fillStyle = colors.hair;
    fRect(x + 1, sy, 6, 2);
    fRect(x, sy + 1, 1, 2);
    fRect(x + 7, sy + 1, 1, 2);

    // Eyes
    ctx.fillStyle = '#1a1a30';
    fRect(x + 2, sy + 3, 1, 1);
    fRect(x + 5, sy + 3, 1, 1);

    // Mouth
    ctx.fillStyle = '#c08060';
    fRect(x + 3, sy + 5, 2, 1);
}

// ============================================================
// DRAWING: UI Overlays
// ============================================================

function drawStatusDot(gx, gy, status) {
    const color = STATUS_COLORS[status] || STATUS_COLORS.idle;
    const cx = (gx + 4) * SCALE;
    const cy = (gy - 2) * SCALE;

    // Glow
    ctx.fillStyle = color + '40';
    ctx.beginPath();
    ctx.arc(cx, cy, 4 * SCALE, 0, Math.PI * 2);
    ctx.fill();

    // Dot
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(cx, cy, 1.5 * SCALE, 0, Math.PI * 2);
    ctx.fill();
}

function drawNameTag(gx, gy, name, role) {
    const colors = ROLE_COLORS[role] || ROLE_COLORS['unknown'];
    ctx.fillStyle = colors.tag;
    ctx.font = `${2.5 * SCALE}px 'Press Start 2P', monospace`;
    ctx.textAlign = 'center';
    ctx.fillText(name, (gx + 4) * SCALE, (gy + 20) * SCALE);
}

function drawBubble(gx, gy, text) {
    if (!text) return;
    const s = SCALE;
    const truncated = text.length > 28 ? text.substring(0, 26) + '..' : text;

    ctx.font = `${2.5 * s}px 'Press Start 2P', monospace`;
    const tw = ctx.measureText(truncated).width;
    const pad = 4 * s;
    const bw = tw + pad * 2;
    const bh = 4 * s + pad * 2;
    const bx = (gx + 4) * s - bw / 2;
    const by = (gy - 8) * s - bh;

    // Shadow
    ctx.fillStyle = 'rgba(0,0,0,0.3)';
    ctx.fillRect(bx + 2, by + 2, bw, bh);

    // Body
    ctx.fillStyle = '#fff';
    ctx.fillRect(bx, by, bw, bh);

    // Border
    ctx.strokeStyle = '#333';
    ctx.lineWidth = 1;
    ctx.strokeRect(bx, by, bw, bh);

    // Pointer
    const px = (gx + 4) * s;
    ctx.fillStyle = '#fff';
    ctx.beginPath();
    ctx.moveTo(px - 3 * s, by + bh);
    ctx.lineTo(px, by + bh + 2.5 * s);
    ctx.lineTo(px + 3 * s, by + bh);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();

    // Text
    ctx.fillStyle = '#1a1a30';
    ctx.textAlign = 'center';
    ctx.fillText(truncated, (gx + 4) * s, by + bh - pad + s);
}

function drawTaskLabel(gx, gy, task) {
    if (!task) return;
    const s = SCALE;
    const text = task.length > 22 ? task.substring(0, 20) + '..' : task;
    ctx.fillStyle = '#666';
    ctx.font = `${1.8 * s}px 'Press Start 2P', monospace`;
    ctx.textAlign = 'center';
    ctx.fillText(text, (gx + 4) * s, (gy + 23) * s);
}

// ============================================================
// POSITION LOGIC
// ============================================================

function getDeskPosition(role) {
    const desk = DESK_POSITIONS.find(d => d.role === role);
    if (desk) return { x: desk.x, y: desk.y + 12 };
    return { x: COFFEE.x - 15, y: COFFEE.y };  // overflow: near coffee
}

function getDiscussionPosition(agentName, partnerName) {
    const pair = [agentName, partnerName].sort();
    const hash = pair.join('').split('').reduce((a, c) => a + c.charCodeAt(0), 0);
    const slot = hash % DISC_SLOTS.length;
    const isFirst = agentName === pair[0];
    const offsets = DISC_SLOTS[slot][isFirst ? 0 : 1];
    return { x: MEETING.x + offsets.x, y: MEETING.y + offsets.y };
}

function getTargetPosition(agent) {
    if (agent.status === 'discussing' && agent.talkingTo) {
        return getDiscussionPosition(agent.name, agent.talkingTo);
    }
    return getDeskPosition(agent.role);
}

// ============================================================
// SSE CONNECTION
// ============================================================

function connectSSE() {
    const evtSource = new EventSource('/events');

    evtSource.onmessage = (e) => {
        connected = true;
        document.getElementById('connection-dot').className = 'connected';
        lastStateTime = Date.now();

        try {
            const state = JSON.parse(e.data);
            processState(state);
        } catch (err) {
            console.error('State parse error:', err);
        }
    };

    evtSource.onerror = () => {
        connected = false;
        document.getElementById('connection-dot').className = 'disconnected';
        evtSource.close();
        setTimeout(connectSSE, 3000);
    };
}

function processState(state) {
    const now = Date.now();

    for (const [name, info] of Object.entries(state.agents || {})) {
        const lastSeen = new Date(info.last_seen).getTime();
        const isStale = (now - lastSeen) > IDLE_TIMEOUT_MS;
        const status = isStale ? 'offline' : (info.status || 'idle');

        if (!agents[name]) {
            // New agent: spawn at target position
            const target = getTargetPosition({
                name, role: info.role || 'unknown', status, talkingTo: info.talking_to
            });
            agents[name] = {
                name,
                role: info.role || 'unknown',
                status,
                task: info.task || '',
                talkingTo: info.talking_to || null,
                message: info.message || null,
                lastSeen: info.last_seen,
                x: target.x, y: target.y,
                targetX: target.x, targetY: target.y,
            };
        } else {
            // Update existing agent
            const a = agents[name];
            a.role = info.role || a.role;
            a.status = status;
            a.task = info.task || '';
            a.talkingTo = info.talking_to || null;
            a.message = info.message || null;
            a.lastSeen = info.last_seen;

            const target = getTargetPosition(a);
            a.targetX = target.x;
            a.targetY = target.y;
        }
    }

    // Update messages
    if (state.messages) {
        const prevLen = messages.length;
        messages = state.messages || [];
        if (messages.length > prevLen) {
            updateChatPanel();
        }
    }

    updateAgentList();
    updateStatusBar();
}

// ============================================================
// SIDEBAR UPDATES
// ============================================================

function updateChatPanel() {
    const log = document.getElementById('chat-log');
    log.innerHTML = '';

    if (messages.length === 0) {
        log.innerHTML = '<div class="empty-chat">No messages yet</div>';
        return;
    }

    for (const msg of messages.slice(-30)) {
        const div = document.createElement('div');
        div.className = 'msg';
        const time = msg.ts ? new Date(msg.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
        div.innerHTML =
            `<span class="time">${esc(time)}</span>` +
            `<span class="from">${esc(msg.from)}</span>` +
            `<span class="arrow"> → </span>` +
            `<span class="to">${esc(msg.to)}</span>` +
            `<span class="text">${esc(msg.text)}</span>`;
        log.appendChild(div);
    }
    log.scrollTop = log.scrollHeight;
}

function updateAgentList() {
    const container = document.getElementById('agent-entries');
    const sorted = Object.values(agents).sort((a, b) => {
        const order = { working: 0, planning: 1, discussing: 2, reviewing: 3, waiting: 4, idle: 5, offline: 6 };
        return (order[a.status] || 5) - (order[b.status] || 5);
    });

    if (sorted.length === 0) {
        container.innerHTML = '<div class="empty-team">Waiting for agents...</div>';
        return;
    }

    container.innerHTML = sorted.map(a => {
        const colors = ROLE_COLORS[a.role] || ROLE_COLORS['unknown'];
        const task = a.task ? `<span class="agent-task">${esc(a.task.substring(0, 40))}</span>` : '';
        return `<div class="agent-entry">
            <div class="agent-dot dot-${a.status}"></div>
            <span class="agent-name" style="color:${colors.tag}">${esc(a.name)}</span>
            <span class="agent-role">${a.status}</span>
        </div>${task}`;
    }).join('');
}

function updateStatusBar() {
    const all = Object.values(agents);
    const active = all.filter(a => ['working', 'planning', 'reviewing'].includes(a.status)).length;
    const discussing = all.filter(a => a.status === 'discussing').length;
    const idle = all.filter(a => ['idle', 'offline'].includes(a.status)).length;

    document.getElementById('agent-count').textContent =
        all.length === 0 ? 'No agents' :
        `${active} active · ${Math.floor(discussing / 2)} convos · ${idle} idle`;
    document.getElementById('last-update').textContent =
        lastStateTime ? new Date(lastStateTime).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '';
}

function esc(text) {
    const d = document.createElement('div');
    d.textContent = text || '';
    return d.innerHTML;
}

// ============================================================
// MAIN LOOP
// ============================================================

function update() {
    frameCount++;

    for (const agent of Object.values(agents)) {
        const dx = agent.targetX - agent.x;
        const dy = agent.targetY - agent.y;
        const dist = Math.sqrt(dx * dx + dy * dy);

        if (dist > 0.5) {
            const speed = Math.min(MOVE_SPEED, dist);
            agent.x += (dx / dist) * speed;
            agent.y += (dy / dist) * speed;
            agent.anim = 'walking';
        } else {
            agent.x = agent.targetX;
            agent.y = agent.targetY;
            if (agent.status === 'working' || agent.status === 'planning') {
                agent.anim = 'working';
            } else if (agent.status === 'discussing') {
                agent.anim = 'discussing';
            } else {
                agent.anim = 'idle';
            }
        }
    }
}

function render() {
    update();

    // Background (cached)
    ensureBackground();
    ctx.drawImage(bgCanvas, 0, 0);

    // Sort agents by Y for depth ordering
    const sorted = Object.values(agents).sort((a, b) => a.y - b.y);

    for (const agent of sorted) {
        const colors = ROLE_COLORS[agent.role] || ROLE_COLORS['unknown'];

        // Character
        drawCharacter(agent.x, agent.y, colors, agent.anim || 'idle', frameCount);

        // Status indicator
        drawStatusDot(agent.x, agent.y, agent.status);

        // Name
        drawNameTag(agent.x, agent.y, agent.name, agent.role);

        // Speech bubble (when discussing with a message)
        if (agent.message && (agent.status === 'discussing' || agent.status === 'reviewing')) {
            drawBubble(agent.x, agent.y, agent.message);
        }

        // Task label (when working)
        if (agent.task && (agent.status === 'working' || agent.status === 'planning') && !agent.message) {
            drawTaskLabel(agent.x, agent.y, agent.task);
        }
    }

    // Empty state
    if (Object.keys(agents).length === 0) {
        ctx.fillStyle = '#444';
        ctx.font = `${4 * SCALE}px 'Press Start 2P', monospace`;
        ctx.textAlign = 'center';
        ctx.fillText('Waiting for agents...', CANVAS_W / 2, CANVAS_H * 0.55);
        ctx.fillStyle = '#2a2a4a';
        ctx.font = `${2.5 * SCALE}px 'Press Start 2P', monospace`;
        ctx.fillText('Agents appear when they start working', CANVAS_W / 2, CANVAS_H * 0.55 + 18 * SCALE);
    }

    requestAnimationFrame(render);
}

// ============================================================
// BOOT
// ============================================================

connectSSE();
requestAnimationFrame(render);
