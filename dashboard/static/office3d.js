// =====================================================================
// Agent Brain Office — 3D dashboard (Three.js, no build step)
//
// Loaded as an ES module via the <script type="importmap"> in
// office3d.html so we can `import` Three.js + addons directly from a
// CDN with no bundler. The same SSE / state shape that the pixel-art
// dashboard consumes also drives this one — server side is unchanged.
//
// Architecture:
//   - Three.js scene with stylised low-poly characters built from
//     primitives (no external models, no licensing concerns).
//   - HTML overlay layer for name tags, status pips and speech bubbles
//     so text stays crisp at every zoom level.
//   - Live SSE stream from /events keeps agent positions, statuses and
//     messages in sync with the brain's office-state.json.
// =====================================================================

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

// ---------- Configuration ----------------------------------------------

const ROLE_COLORS = {
    'project-manager':    { tag: '#29ADFF', label: 'PM',  shirt: 0x29adff, hair: 0x2c1810 },
    'product-owner':      { tag: '#FF77A8', label: 'PO',  shirt: 0xff77a8, hair: 0x4a1530 },
    'principal-engineer': { tag: '#e94560', label: 'PE',  shirt: 0xe94560, hair: 0x3a2010 },
    'backend-engineer':   { tag: '#008751', label: 'BE',  shirt: 0x008751, hair: 0x1a1a30 },
    'frontend-engineer':  { tag: '#FFA300', label: 'FE',  shirt: 0xffa300, hair: 0x4a2010 },
    'qa-engineer':        { tag: '#7E2553', label: 'QA',  shirt: 0x7e2553, hair: 0x1a1a30 },
    'lead':               { tag: '#c0c0ff', label: 'Lead', shirt: 0xc0c0ff, hair: 0x2a2a3a },
    'unknown':            { tag: '#888888', label: '??',  shirt: 0x666666, hair: 0x333333 },
};

// Some teams scale via -2/-3 suffixed roles (peer engineers, peer POs, etc.).
// Strip the suffix so visual mapping stays consistent: a backend-engineer-2
// looks identical to a backend-engineer and sits at the same desk.
function normaliseRole(role) {
    if (!role) return 'unknown';
    const stripped = role.replace(/-\d+$/, '');
    return ROLE_COLORS[stripped] ? stripped : (ROLE_COLORS[role] ? role : 'unknown');
}

const STATUS_COLORS = {
    working: '#00E436', planning: '#FFEC27', reviewing: '#FFA300',
    discussing: '#29ADFF', blocked: '#FF004D', waiting: '#FFEC27',
    idle: '#555555', offline: '#2a2a3e',
};

// World coordinates for desks — 6 around the room periphery, meeting
// table in the centre. Y is up (Three.js convention).
const DESK_LAYOUT = [
    { role: 'project-manager',    x: -6, z: -4, facing: 0          },
    { role: 'product-owner',      x: -6, z:  0, facing: 0          },
    { role: 'principal-engineer', x:  6, z: -4, facing: Math.PI    },
    { role: 'qa-engineer',        x:  6, z:  0, facing: Math.PI    },
    { role: 'backend-engineer',   x: -6, z:  4, facing: 0          },
    { role: 'frontend-engineer',  x:  6, z:  4, facing: Math.PI    },
];

const MEETING_TABLE = { x: 0, z: 0, radius: 1.6 };
// Six chair positions around the meeting table, evenly spaced.
const MEETING_SEATS = Array.from({ length: 6 }, (_, i) => {
    const a = (i / 6) * Math.PI * 2;
    return {
        x: MEETING_TABLE.x + Math.cos(a) * (MEETING_TABLE.radius + 0.7),
        z: MEETING_TABLE.z + Math.sin(a) * (MEETING_TABLE.radius + 0.7),
        facing: a + Math.PI,  // face the table
    };
});

const IDLE_TIMEOUT_MS = 2 * 60 * 1000;

// ---------- State -----------------------------------------------------

const agents = new Map();   // name → { mesh, group, role, status, task, message, talkingTo, target, lastSeen, animPhase }
let messages = [];
let connected = false;

// ---------- Three.js bootstrap ----------------------------------------

const canvas = document.getElementById('office3d');
const overlay = document.getElementById('overlay-layer');

const renderer = new THREE.WebGLRenderer({
    canvas, antialias: true, alpha: false, powerPreference: 'high-performance',
});
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.05;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0a0a1f);
scene.fog = new THREE.Fog(0x0a0a1f, 18, 38);

const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 200);
camera.position.set(11, 9.5, 12);
camera.lookAt(0, 1, 0);

const controls = new OrbitControls(camera, canvas);
controls.target.set(0, 1, 0);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.minDistance = 8;
controls.maxDistance = 22;
controls.minPolarAngle = Math.PI * 0.18;
controls.maxPolarAngle = Math.PI * 0.48;
controls.enablePan = false;

function resize() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (canvas.width !== w || canvas.height !== h) {
        renderer.setSize(w, h, false);
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
    }
}
window.addEventListener('resize', resize);

// ---------- Lighting --------------------------------------------------

// Soft ambient base so shadows never go pitch black.
scene.add(new THREE.HemisphereLight(0xb6c4ff, 0x202040, 0.55));

// Key light — warm, casting shadows from above-front-right.
const key = new THREE.DirectionalLight(0xfff1d2, 1.2);
key.position.set(8, 14, 6);
key.castShadow = true;
key.shadow.mapSize.set(2048, 2048);
key.shadow.camera.left = -12;
key.shadow.camera.right = 12;
key.shadow.camera.top = 12;
key.shadow.camera.bottom = -12;
key.shadow.camera.near = 1;
key.shadow.camera.far = 40;
key.shadow.bias = -0.0005;
scene.add(key);

// Cool rim light from the opposite side for depth on character backs.
const rim = new THREE.DirectionalLight(0x6080ff, 0.45);
rim.position.set(-8, 6, -6);
scene.add(rim);

// Subtle accent bouncing off the floor.
const fill = new THREE.PointLight(0xff7faa, 0.4, 18, 2);
fill.position.set(0, 3, 0);
scene.add(fill);

// ---------- Office shell ---------------------------------------------

function buildOffice() {
    const group = new THREE.Group();

    // Floor — large soft-tinted plane with a subtle rug under the meeting table.
    const floorMat = new THREE.MeshStandardMaterial({
        color: 0x2a2845, roughness: 0.92, metalness: 0.0,
    });
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(40, 40), floorMat);
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    group.add(floor);

    // Rug under meeting area
    const rugMat = new THREE.MeshStandardMaterial({
        color: 0x4a2c5a, roughness: 0.95, metalness: 0.0,
    });
    const rug = new THREE.Mesh(new THREE.CircleGeometry(4.5, 48), rugMat);
    rug.rotation.x = -Math.PI / 2;
    rug.position.y = 0.01;
    rug.receiveShadow = true;
    group.add(rug);

    // Walls (low, to keep camera unobstructed). Three sides only.
    const wallMat = new THREE.MeshStandardMaterial({
        color: 0x1a1a3a, roughness: 0.85,
    });
    const wallH = 4;
    const wallT = 0.2;
    const back = new THREE.Mesh(new THREE.BoxGeometry(20, wallH, wallT), wallMat);
    back.position.set(0, wallH / 2, -10);
    back.receiveShadow = true;
    group.add(back);

    const left = new THREE.Mesh(new THREE.BoxGeometry(wallT, wallH, 20), wallMat);
    left.position.set(-10, wallH / 2, 0);
    left.receiveShadow = true;
    group.add(left);

    const right = new THREE.Mesh(new THREE.BoxGeometry(wallT, wallH, 20), wallMat);
    right.position.set(10, wallH / 2, 0);
    right.receiveShadow = true;
    group.add(right);

    // Brand strip on back wall — a soft-glowing horizontal band so the
    // room reads as branded space without needing a texture asset.
    const bandMat = new THREE.MeshBasicMaterial({ color: 0xff6b8a });
    const band = new THREE.Mesh(new THREE.PlaneGeometry(14, 0.05), bandMat);
    band.position.set(0, 2.6, -9.89);
    group.add(band);

    // Plants in the corners
    [[-9, -9], [9, -9], [-9, 9], [9, 9]].forEach(([x, z]) => {
        group.add(buildPlant(x, z));
    });

    return group;
}

function buildPlant(x, z) {
    const g = new THREE.Group();
    const potMat  = new THREE.MeshStandardMaterial({ color: 0x6b4a2b, roughness: 0.9 });
    const pot = new THREE.Mesh(new THREE.CylinderGeometry(0.35, 0.28, 0.5, 12), potMat);
    pot.position.y = 0.25;
    pot.castShadow = pot.receiveShadow = true;
    g.add(pot);

    const leafMat = new THREE.MeshStandardMaterial({
        color: 0x3a8c3a, roughness: 0.7, flatShading: true,
    });
    for (let i = 0; i < 5; i++) {
        const leaf = new THREE.Mesh(new THREE.IcosahedronGeometry(0.32, 0), leafMat);
        const a = (i / 5) * Math.PI * 2;
        leaf.position.set(
            Math.cos(a) * 0.18,
            0.7 + Math.random() * 0.4,
            Math.sin(a) * 0.18,
        );
        leaf.scale.set(1, 1.5, 1);
        leaf.castShadow = true;
        g.add(leaf);
    }
    g.position.set(x, 0, z);
    return g;
}

// ---------- Furniture -------------------------------------------------

function buildDesk(role, position) {
    const colors = ROLE_COLORS[role] || ROLE_COLORS.unknown;
    const g = new THREE.Group();

    // Desk surface
    const woodMat = new THREE.MeshStandardMaterial({
        color: 0x6b4a2b, roughness: 0.7,
    });
    const top = new THREE.Mesh(new THREE.BoxGeometry(2.2, 0.08, 1.1), woodMat);
    top.position.y = 0.78;
    top.castShadow = top.receiveShadow = true;
    g.add(top);

    // Legs
    const legMat = new THREE.MeshStandardMaterial({ color: 0x3a3a4f, roughness: 0.5 });
    const legGeom = new THREE.BoxGeometry(0.08, 0.78, 0.08);
    [[-1.0, -0.5], [1.0, -0.5], [-1.0, 0.5], [1.0, 0.5]].forEach(([x, z]) => {
        const leg = new THREE.Mesh(legGeom, legMat);
        leg.position.set(x, 0.39, z);
        leg.castShadow = true;
        g.add(leg);
    });

    // Role-coloured strip on the desk edge — visual identifier for each role.
    const stripMat = new THREE.MeshStandardMaterial({
        color: colors.shirt, roughness: 0.4, metalness: 0.1,
        emissive: colors.shirt, emissiveIntensity: 0.25,
    });
    const strip = new THREE.Mesh(new THREE.BoxGeometry(2.0, 0.04, 0.05), stripMat);
    strip.position.set(0, 0.78, 0.55);
    g.add(strip);

    // Monitor — back, screen and screen glow
    const monitorMat = new THREE.MeshStandardMaterial({ color: 0x101020, roughness: 0.3 });
    const monitor = new THREE.Mesh(new THREE.BoxGeometry(0.85, 0.55, 0.06), monitorMat);
    monitor.position.set(0, 1.32, -0.25);
    monitor.castShadow = true;
    g.add(monitor);

    const screenMat = new THREE.MeshBasicMaterial({ color: 0x29adff });
    const screen = new THREE.Mesh(new THREE.PlaneGeometry(0.78, 0.48), screenMat);
    screen.position.set(0, 1.32, -0.215);
    g.add(screen);

    // Stand
    const stand = new THREE.Mesh(
        new THREE.BoxGeometry(0.12, 0.18, 0.08),
        new THREE.MeshStandardMaterial({ color: 0x222234, roughness: 0.5 }),
    );
    stand.position.set(0, 0.95, -0.25);
    stand.castShadow = true;
    g.add(stand);
    const base = new THREE.Mesh(
        new THREE.BoxGeometry(0.4, 0.04, 0.2),
        new THREE.MeshStandardMaterial({ color: 0x222234, roughness: 0.5 }),
    );
    base.position.set(0, 0.84, -0.25);
    base.castShadow = true;
    g.add(base);

    // Chair
    const chairMat = new THREE.MeshStandardMaterial({ color: 0x222244, roughness: 0.7 });
    const seat = new THREE.Mesh(new THREE.BoxGeometry(0.6, 0.08, 0.6), chairMat);
    seat.position.set(0, 0.5, 0.85);
    seat.castShadow = true;
    g.add(seat);
    const backrest = new THREE.Mesh(new THREE.BoxGeometry(0.6, 0.7, 0.08), chairMat);
    backrest.position.set(0, 0.85, 1.15);
    backrest.castShadow = true;
    g.add(backrest);

    g.position.set(position.x, 0, position.z);
    g.rotation.y = position.facing;
    g.userData.role = role;
    g.userData.kind = 'desk';
    return g;
}

function buildMeetingTable() {
    const g = new THREE.Group();

    // Table top — round
    const topMat = new THREE.MeshStandardMaterial({ color: 0x6b4a2b, roughness: 0.7 });
    const top = new THREE.Mesh(
        new THREE.CylinderGeometry(MEETING_TABLE.radius, MEETING_TABLE.radius, 0.08, 32),
        topMat,
    );
    top.position.y = 0.75;
    top.castShadow = top.receiveShadow = true;
    g.add(top);

    // Pedestal
    const pedMat = new THREE.MeshStandardMaterial({ color: 0x3a3a4f, roughness: 0.6 });
    const ped = new THREE.Mesh(
        new THREE.CylinderGeometry(0.15, 0.3, 0.7, 16),
        pedMat,
    );
    ped.position.y = 0.4;
    ped.castShadow = true;
    g.add(ped);

    const baseT = new THREE.Mesh(
        new THREE.CylinderGeometry(0.6, 0.7, 0.08, 24),
        pedMat,
    );
    baseT.position.y = 0.04;
    baseT.castShadow = true;
    g.add(baseT);

    // Glow disk on the table to suggest "active meeting" vibe
    const glow = new THREE.Mesh(
        new THREE.CircleGeometry(MEETING_TABLE.radius * 0.85, 32),
        new THREE.MeshBasicMaterial({
            color: 0x29adff, transparent: true, opacity: 0.18,
        }),
    );
    glow.rotation.x = -Math.PI / 2;
    glow.position.y = 0.795;
    g.add(glow);
    g.userData.glow = glow;

    g.position.set(MEETING_TABLE.x, 0, MEETING_TABLE.z);
    return g;
}

// ---------- Coffee corner -------------------------------------------

// World position of the coffee station — agents in 'reviewing' state
// occasionally walk over here for visual interest.
const COFFEE_SPOT = { x: -8.5, z: 7.5 };

function buildCoffeeCorner() {
    const g = new THREE.Group();

    // Counter
    const counterMat = new THREE.MeshStandardMaterial({ color: 0x3a3a55, roughness: 0.5 });
    const counter = new THREE.Mesh(new THREE.BoxGeometry(2.5, 0.9, 0.7), counterMat);
    counter.position.y = 0.45;
    counter.castShadow = counter.receiveShadow = true;
    g.add(counter);

    const counterTopMat = new THREE.MeshStandardMaterial({
        color: 0x1a1a2a, roughness: 0.2, metalness: 0.3,
    });
    const counterTop = new THREE.Mesh(new THREE.BoxGeometry(2.6, 0.04, 0.78), counterTopMat);
    counterTop.position.y = 0.92;
    counterTop.castShadow = counterTop.receiveShadow = true;
    g.add(counterTop);

    // Espresso machine — chrome top half
    const machineBaseMat = new THREE.MeshStandardMaterial({
        color: 0x2a2a3a, roughness: 0.4, metalness: 0.5,
    });
    const machineBody = new THREE.Mesh(new THREE.BoxGeometry(0.7, 0.6, 0.5), machineBaseMat);
    machineBody.position.set(-0.6, 1.24, 0);
    machineBody.castShadow = true;
    g.add(machineBody);

    const machineChromeMat = new THREE.MeshStandardMaterial({
        color: 0xd0d0e0, roughness: 0.15, metalness: 0.8,
    });
    const machineTop = new THREE.Mesh(new THREE.BoxGeometry(0.72, 0.18, 0.52), machineChromeMat);
    machineTop.position.set(-0.6, 1.62, 0);
    machineTop.castShadow = true;
    g.add(machineTop);

    // Group spout under the body
    const spoutMat = new THREE.MeshStandardMaterial({
        color: 0x101020, roughness: 0.3, metalness: 0.4,
    });
    const spout = new THREE.Mesh(new THREE.CylinderGeometry(0.04, 0.04, 0.18, 8), spoutMat);
    spout.position.set(-0.6, 1.05, 0.22);
    spout.castShadow = true;
    g.add(spout);

    // Status LED on the machine — warm amber glow
    const led = new THREE.Mesh(
        new THREE.SphereGeometry(0.04, 12, 8),
        new THREE.MeshBasicMaterial({ color: 0xffaa44 }),
    );
    led.position.set(-0.45, 1.36, 0.26);
    g.add(led);

    // Mug on the counter
    const mugMat = new THREE.MeshStandardMaterial({
        color: 0xffffff, roughness: 0.5,
    });
    const mug = new THREE.Mesh(
        new THREE.CylinderGeometry(0.1, 0.08, 0.16, 16),
        mugMat,
    );
    mug.position.set(0.4, 1.02, 0.1);
    mug.castShadow = true;
    g.add(mug);

    // Steam — three small particles drifting up that we animate per-frame.
    const steamMat = new THREE.MeshBasicMaterial({
        color: 0xffffff, transparent: true, opacity: 0.4, depthWrite: false,
    });
    const steamParts = [];
    for (let i = 0; i < 3; i++) {
        const s = new THREE.Mesh(new THREE.SphereGeometry(0.05, 8, 6), steamMat.clone());
        s.position.set(0.4, 1.2 + i * 0.12, 0.1);
        s.userData.seed = Math.random() * 6.28;
        steamParts.push(s);
        g.add(s);
    }
    g.userData.steamParts = steamParts;

    // Sign on the wall behind the coffee corner
    const signMat = new THREE.MeshStandardMaterial({
        color: 0x6b4a2b, roughness: 0.7, emissive: 0x4a3520, emissiveIntensity: 0.3,
    });
    const sign = new THREE.Mesh(new THREE.BoxGeometry(1.2, 0.4, 0.05), signMat);
    sign.position.set(-0.4, 2.4, -0.45);
    g.add(sign);

    g.position.set(COFFEE_SPOT.x, 0, COFFEE_SPOT.z);
    g.rotation.y = -Math.PI / 4;  // angled into the room
    return g;
}

// ---------- Play area -----------------------------------------------

// Cosy corner with a couch + foosball table.
const PLAY_SPOT = { x: 8, z: 7.5 };

function buildPlayArea() {
    const g = new THREE.Group();

    // Couch — three-cushion sofa, dark teal fabric
    const couchMat = new THREE.MeshStandardMaterial({
        color: 0x2a5560, roughness: 0.85,
    });
    const couchAccentMat = new THREE.MeshStandardMaterial({
        color: 0x3a7080, roughness: 0.85,
    });
    const couchBase = new THREE.Mesh(new THREE.BoxGeometry(2.4, 0.45, 0.85), couchMat);
    couchBase.position.set(0, 0.3, 0);
    couchBase.castShadow = couchBase.receiveShadow = true;
    g.add(couchBase);

    const couchBack = new THREE.Mesh(new THREE.BoxGeometry(2.4, 0.7, 0.18), couchMat);
    couchBack.position.set(0, 0.7, -0.34);
    couchBack.castShadow = true;
    g.add(couchBack);

    // Cushions
    for (let i = -1; i <= 1; i++) {
        const cushion = new THREE.Mesh(
            new THREE.BoxGeometry(0.7, 0.15, 0.7),
            couchAccentMat,
        );
        cushion.position.set(i * 0.8, 0.6, -0.05);
        cushion.castShadow = true;
        g.add(cushion);
    }

    // Side arms
    const armMat = couchMat;
    [-1.3, 1.3].forEach(x => {
        const arm = new THREE.Mesh(new THREE.BoxGeometry(0.2, 0.65, 0.85), armMat);
        arm.position.set(x, 0.5, 0);
        arm.castShadow = true;
        g.add(arm);
    });

    // Coffee table in front of couch
    const tableTopMat = new THREE.MeshStandardMaterial({
        color: 0x3a2a1a, roughness: 0.6,
    });
    const tableTop = new THREE.Mesh(new THREE.BoxGeometry(1.2, 0.06, 0.6), tableTopMat);
    tableTop.position.set(0, 0.45, 1.1);
    tableTop.castShadow = tableTop.receiveShadow = true;
    g.add(tableTop);

    [[-0.5, 0.85], [0.5, 0.85], [-0.5, 1.35], [0.5, 1.35]].forEach(([x, z]) => {
        const leg = new THREE.Mesh(
            new THREE.BoxGeometry(0.06, 0.42, 0.06),
            new THREE.MeshStandardMaterial({ color: 0x222234, roughness: 0.5 }),
        );
        leg.position.set(x, 0.21, z);
        leg.castShadow = true;
        g.add(leg);
    });

    // Foosball table — small, just a hint of "play space"
    const foosBaseMat = new THREE.MeshStandardMaterial({
        color: 0x5a3a1a, roughness: 0.7,
    });
    const foosTop = new THREE.Mesh(new THREE.BoxGeometry(1.4, 0.06, 0.85), foosBaseMat);
    foosTop.position.set(2.6, 0.85, 0.5);
    foosTop.castShadow = foosTop.receiveShadow = true;
    g.add(foosTop);

    // Foosball play surface (green)
    const fieldMat = new THREE.MeshStandardMaterial({
        color: 0x2a6a3a, roughness: 0.85,
    });
    const field = new THREE.Mesh(new THREE.BoxGeometry(1.3, 0.01, 0.78), fieldMat);
    field.position.set(2.6, 0.885, 0.5);
    g.add(field);

    // Foosball legs
    [-0.6, 0.6].forEach(dx => {
        [-0.35, 0.35].forEach(dz => {
            const leg = new THREE.Mesh(
                new THREE.BoxGeometry(0.06, 0.85, 0.06),
                new THREE.MeshStandardMaterial({ color: 0x2a2a3a, roughness: 0.5 }),
            );
            leg.position.set(2.6 + dx, 0.42, 0.5 + dz);
            leg.castShadow = true;
            g.add(leg);
        });
    });

    // Foosball rods (chrome) — three across the table
    for (let i = 0; i < 3; i++) {
        const rod = new THREE.Mesh(
            new THREE.CylinderGeometry(0.025, 0.025, 1.05, 8),
            new THREE.MeshStandardMaterial({
                color: 0xc0c0d0, roughness: 0.2, metalness: 0.7,
            }),
        );
        rod.rotation.z = Math.PI / 2;
        rod.position.set(2.6 - 0.4 + i * 0.4, 0.95, 0.5);
        g.add(rod);

        // Tiny player figures on the rod (three per rod)
        for (let p = 0; p < 3; p++) {
            const pawn = new THREE.Mesh(
                new THREE.BoxGeometry(0.04, 0.16, 0.04),
                new THREE.MeshStandardMaterial({
                    color: i % 2 === 0 ? 0xff4040 : 0x4040ff,
                }),
            );
            pawn.position.set(2.6 - 0.4 + i * 0.4, 0.95, 0.5 - 0.3 + p * 0.3);
            g.add(pawn);
        }
    }

    g.position.set(PLAY_SPOT.x, 0, PLAY_SPOT.z);
    g.rotation.y = Math.PI / 4;  // angled into the room
    return g;
}

// ---------- Caesar the golden retriever -----------------------------

// Caesar wanders the office in a simple state machine:
//   wander → walk to random waypoint
//   sit    → idle for a few seconds
//   visit  → walk toward an idle agent and request a pet
//   pet    → sit at the agent's feet, both Caesar and agent emote
function buildCaesar() {
    const g = new THREE.Group();
    g.userData.kind = 'dog';

    const fur = new THREE.MeshStandardMaterial({
        color: 0xd9a866, roughness: 0.85, flatShading: true,
    });
    const furLight = new THREE.MeshStandardMaterial({
        color: 0xf0c98a, roughness: 0.85, flatShading: true,
    });
    const dark = new THREE.MeshStandardMaterial({
        color: 0x101020, roughness: 0.5,
    });

    // Body — a stretched capsule on its side
    const body = new THREE.Mesh(
        new THREE.CapsuleGeometry(0.18, 0.45, 6, 12),
        fur,
    );
    body.rotation.z = Math.PI / 2;
    body.position.y = 0.3;
    body.castShadow = true;
    g.add(body);

    // Belly highlight (lighter underside)
    const belly = new THREE.Mesh(
        new THREE.SphereGeometry(0.16, 12, 8, 0, Math.PI * 2, 0, Math.PI / 2),
        furLight,
    );
    belly.position.y = 0.22;
    belly.rotation.x = Math.PI;
    g.add(belly);

    // Head
    const headPivot = new THREE.Group();
    headPivot.position.set(0.32, 0.4, 0);
    g.add(headPivot);
    g.userData.headPivot = headPivot;

    const head = new THREE.Mesh(new THREE.SphereGeometry(0.16, 16, 12), fur);
    head.castShadow = true;
    headPivot.add(head);

    // Snout
    const snout = new THREE.Mesh(
        new THREE.BoxGeometry(0.18, 0.1, 0.1),
        furLight,
    );
    snout.position.set(0.16, -0.04, 0);
    headPivot.add(snout);

    // Nose
    const nose = new THREE.Mesh(
        new THREE.SphereGeometry(0.03, 10, 8),
        dark,
    );
    nose.position.set(0.26, -0.04, 0);
    headPivot.add(nose);

    // Ears
    const earGeom = new THREE.SphereGeometry(0.07, 10, 8, 0, Math.PI * 2, 0, Math.PI / 2);
    const earL = new THREE.Mesh(earGeom, fur);
    earL.position.set(-0.05, 0.12, 0.1);
    earL.scale.set(1, 1.2, 0.5);
    earL.rotation.x = Math.PI;
    headPivot.add(earL);
    const earR = new THREE.Mesh(earGeom, fur);
    earR.position.set(-0.05, 0.12, -0.1);
    earR.scale.set(1, 1.2, 0.5);
    earR.rotation.x = Math.PI;
    headPivot.add(earR);

    // Eyes
    const eyeGeom = new THREE.SphereGeometry(0.018, 8, 6);
    const eyeL = new THREE.Mesh(eyeGeom, dark);
    eyeL.position.set(0.13, 0.04, 0.06);
    headPivot.add(eyeL);
    const eyeR = new THREE.Mesh(eyeGeom, dark);
    eyeR.position.set(0.13, 0.04, -0.06);
    headPivot.add(eyeR);

    // Legs — four short capsules, animated for walking
    const legGeom = new THREE.CapsuleGeometry(0.05, 0.18, 4, 6);
    const legs = {};
    [['fl', 0.18, 0.12], ['fr', 0.18, -0.12], ['bl', -0.18, 0.12], ['br', -0.18, -0.12]].forEach(([k, x, z]) => {
        const leg = new THREE.Group();
        leg.position.set(x, 0.2, z);
        const limb = new THREE.Mesh(legGeom, fur);
        limb.position.y = -0.1;
        limb.castShadow = true;
        leg.add(limb);
        const paw = new THREE.Mesh(
            new THREE.BoxGeometry(0.09, 0.05, 0.09),
            new THREE.MeshStandardMaterial({ color: 0x2a1a10 }),
        );
        paw.position.y = -0.21;
        leg.add(paw);
        g.add(leg);
        legs[k] = leg;
    });
    g.userData.legs = legs;

    // Tail — slightly upward, will wag
    const tailPivot = new THREE.Group();
    tailPivot.position.set(-0.3, 0.36, 0);
    g.add(tailPivot);
    g.userData.tailPivot = tailPivot;

    const tail = new THREE.Mesh(
        new THREE.CapsuleGeometry(0.04, 0.22, 4, 8),
        fur,
    );
    tail.position.y = 0.1;
    tail.rotation.z = -0.4;
    tail.castShadow = true;
    tailPivot.add(tail);

    // Soft contact shadow under the dog
    const shadow = new THREE.Mesh(
        new THREE.CircleGeometry(0.45, 16),
        new THREE.MeshBasicMaterial({
            color: 0x000000, transparent: true, opacity: 0.35, depthWrite: false,
        }),
    );
    shadow.rotation.x = -Math.PI / 2;
    shadow.position.y = 0.02;
    g.add(shadow);

    // State
    g.userData.state = 'wander';      // wander | sit | visit | pet
    g.userData.stateUntil = 0;
    g.userData.targetX = 0;
    g.userData.targetZ = 0;
    g.userData.target = null;         // agent name when visiting
    g.userData.animPhase = 0;

    return g;
}

// ---------- Characters ------------------------------------------------

// Build a stylised low-poly humanoid. We deliberately keep proportions
// uniform across all roles so the "team" reads as a coherent set —
// only shirt and hair colours change per role.
function buildCharacter(role) {
    const colors = ROLE_COLORS[role] || ROLE_COLORS.unknown;
    const g = new THREE.Group();
    g.userData.kind = 'character';

    const skin = 0xf2c499;

    // Body parts grouped so we can animate them.
    const torso = new THREE.Mesh(
        new THREE.CapsuleGeometry(0.32, 0.55, 4, 12),
        new THREE.MeshStandardMaterial({
            color: colors.shirt, roughness: 0.6, flatShading: true,
        }),
    );
    torso.position.y = 0.95;
    torso.castShadow = true;
    g.add(torso);

    // Head
    const headPivot = new THREE.Group();
    headPivot.position.y = 1.55;
    const head = new THREE.Mesh(
        new THREE.SphereGeometry(0.26, 20, 16),
        new THREE.MeshStandardMaterial({
            color: skin, roughness: 0.55, flatShading: true,
        }),
    );
    head.castShadow = true;
    headPivot.add(head);
    g.add(headPivot);
    g.userData.headPivot = headPivot;

    // Hair as a half-sphere cap
    const hair = new THREE.Mesh(
        new THREE.SphereGeometry(0.27, 18, 14, 0, Math.PI * 2, 0, Math.PI * 0.55),
        new THREE.MeshStandardMaterial({
            color: colors.hair, roughness: 0.85, flatShading: true,
        }),
    );
    hair.position.y = 0.04;
    hair.castShadow = true;
    headPivot.add(hair);

    // Eyes — two tiny spheres
    const eyeMat = new THREE.MeshBasicMaterial({ color: 0x101030 });
    const eyeGeom = new THREE.SphereGeometry(0.025, 8, 6);
    const eyeL = new THREE.Mesh(eyeGeom, eyeMat);
    const eyeR = new THREE.Mesh(eyeGeom, eyeMat);
    eyeL.position.set(-0.085, 0.0, 0.235);
    eyeR.position.set( 0.085, 0.0, 0.235);
    headPivot.add(eyeL);
    headPivot.add(eyeR);

    // Arms — capsules pivoted at the shoulder so we can swing them.
    const armMat = new THREE.MeshStandardMaterial({
        color: colors.shirt, roughness: 0.6, flatShading: true,
    });
    const armGeom = new THREE.CapsuleGeometry(0.09, 0.5, 4, 8);
    const handMat = new THREE.MeshStandardMaterial({
        color: skin, roughness: 0.55, flatShading: true,
    });
    const handGeom = new THREE.SphereGeometry(0.1, 10, 8);

    const armL = new THREE.Group();
    armL.position.set(-0.42, 1.25, 0);
    const armLMesh = new THREE.Mesh(armGeom, armMat);
    armLMesh.position.y = -0.3;
    armLMesh.castShadow = true;
    armL.add(armLMesh);
    const handL = new THREE.Mesh(handGeom, handMat);
    handL.position.y = -0.6;
    armL.add(handL);
    g.add(armL);
    g.userData.armL = armL;

    const armR = new THREE.Group();
    armR.position.set(0.42, 1.25, 0);
    const armRMesh = new THREE.Mesh(armGeom, armMat);
    armRMesh.position.y = -0.3;
    armRMesh.castShadow = true;
    armR.add(armRMesh);
    const handR = new THREE.Mesh(handGeom, handMat);
    handR.position.y = -0.6;
    armR.add(handR);
    g.add(armR);
    g.userData.armR = armR;

    // Legs
    const pantsMat = new THREE.MeshStandardMaterial({
        color: 0x2a2a4a, roughness: 0.7, flatShading: true,
    });
    const legGeom = new THREE.CapsuleGeometry(0.12, 0.55, 4, 8);

    const legL = new THREE.Group();
    legL.position.set(-0.16, 0.6, 0);
    const legLMesh = new THREE.Mesh(legGeom, pantsMat);
    legLMesh.position.y = -0.3;
    legLMesh.castShadow = true;
    legL.add(legLMesh);
    const shoeL = new THREE.Mesh(
        new THREE.BoxGeometry(0.18, 0.08, 0.3),
        new THREE.MeshStandardMaterial({ color: 0x101020, roughness: 0.4 }),
    );
    shoeL.position.set(0, -0.62, 0.06);
    shoeL.castShadow = true;
    legL.add(shoeL);
    g.add(legL);
    g.userData.legL = legL;

    const legR = new THREE.Group();
    legR.position.set(0.16, 0.6, 0);
    const legRMesh = new THREE.Mesh(legGeom, pantsMat);
    legRMesh.position.y = -0.3;
    legRMesh.castShadow = true;
    legR.add(legRMesh);
    const shoeR = new THREE.Mesh(
        new THREE.BoxGeometry(0.18, 0.08, 0.3),
        new THREE.MeshStandardMaterial({ color: 0x101020, roughness: 0.4 }),
    );
    shoeR.position.set(0, -0.62, 0.06);
    shoeR.castShadow = true;
    legR.add(shoeR);
    g.add(legR);
    g.userData.legR = legR;

    // Soft contact shadow plane underneath — helps grounding even on
    // hardware where Three.js shadow-map quality is mediocre.
    const fakeShadow = new THREE.Mesh(
        new THREE.CircleGeometry(0.42, 16),
        new THREE.MeshBasicMaterial({
            color: 0x000000, transparent: true, opacity: 0.35, depthWrite: false,
        }),
    );
    fakeShadow.rotation.x = -Math.PI / 2;
    fakeShadow.position.y = 0.02;
    g.add(fakeShadow);

    g.userData.colors = colors;
    return g;
}

// ---------- Build static scene ----------------------------------------

const officeShell = buildOffice();
scene.add(officeShell);

const meetingTable = buildMeetingTable();
scene.add(meetingTable);

DESK_LAYOUT.forEach(d => scene.add(buildDesk(d.role, d)));

const coffeeCorner = buildCoffeeCorner();
scene.add(coffeeCorner);

const playArea = buildPlayArea();
scene.add(playArea);

// Caesar the office dog — runs his own little state machine on each frame.
const caesar = buildCaesar();
caesar.position.set(0, 0, 6);
caesar.userData.targetX = 0;
caesar.userData.targetZ = 6;
scene.add(caesar);

// Caesar's name tag — same component as agent name tags but with a paw icon.
const caesarTag = document.createElement('div');
caesarTag.className = 'name-tag caesar-tag';
caesarTag.innerHTML = `<span class="role-strip" style="background:#d9a866"></span>🐾 Caesar`;
overlay.appendChild(caesarTag);
caesar.userData.tag = caesarTag;

// ---------- Animation helpers ----------------------------------------

function setCharacterAnimation(agent, dt) {
    const group = agent.group;
    const phase = (agent.animPhase += dt);

    const armL = group.userData.armL;
    const armR = group.userData.armR;
    const legL = group.userData.legL;
    const legR = group.userData.legR;
    const head = group.userData.headPivot;

    // Reset all animated channels each frame so the previous status's
    // pose doesn't bleed into the next.
    armL.rotation.x = 0; armR.rotation.x = 0;
    armL.rotation.z = 0; armR.rotation.z = 0;
    legL.rotation.x = 0; legR.rotation.x = 0;
    head.rotation.x = 0; head.rotation.y = 0; head.rotation.z = 0;
    group.rotation.z = 0;

    // If Caesar is currently being petted by this agent, override the
    // normal status-driven animation with a "leaning down petting the
    // dog" pose. Right arm reaches out and down, head tilts down and
    // toward the dog. Restored automatically when _petUntil expires.
    if (agent._beingPet && performance.now() < (agent._petUntil || 0)) {
        const t = phase;
        // Reaching arm with subtle stroke motion. Negative rotation.x
        // brings the arm forward (+Z, in front of the body, where the
        // dog is sitting). Slight downward tilt baked in via offset.
        armR.rotation.x = -1.1 + Math.sin(t * 4) * 0.15;
        armR.rotation.z = -0.2;
        armL.rotation.z = 0.05;
        head.rotation.x = 0.35;     // looking down at dog
        head.rotation.y = Math.sin(t * 3) * 0.1;
        return;  // Skip the normal status-driven animation this frame
    } else if (agent._beingPet) {
        // Cleanup the flag once the timer has elapsed
        agent._beingPet = false;
    }

    // Tiny drift on body so characters never feel frozen
    group.position.y = Math.sin(phase * 1.4 + agent.bobSeed) * 0.025;

    switch (agent.status) {
        case 'walking': {
            const swing = Math.sin(phase * 8) * 0.6;
            armL.rotation.x = swing;
            armR.rotation.x = -swing;
            legL.rotation.x = -swing * 0.8;
            legR.rotation.x = swing * 0.8;
            break;
        }
        case 'working':
        case 'planning': {
            // Typing posture: arms forward & slightly inward so hands hover
            // over a virtual keyboard in front of the torso. Subtle
            // alternating tap (sin wave on each side, opposite phase) so
            // they look like they're typing, not flailing.
            //
            // Geometry note: arm capsule hangs along -Y from the shoulder
            // pivot. Right-hand rule: +rotation.x maps -Y → -Z (BEHIND the
            // character, toward viewer). We want hands in FRONT (+Z, toward
            // the monitor), so we use a NEGATIVE rotation.x.
            const tap = Math.sin(phase * 11) * 0.12;
            armL.rotation.x = -Math.PI / 2 + 0.15 - tap;
            armR.rotation.x = -Math.PI / 2 + 0.15 + tap;
            armL.rotation.z = -0.25;  // bring left hand inward toward centre
            armR.rotation.z =  0.25;  // bring right hand inward toward centre
            head.rotation.x = 0.18;   // gentle forward head tilt to look at screen
            break;
        }
        case 'discussing': {
            // Animated hand gesture out in front + head turning to "speak"
            const gesture = Math.sin(phase * 3.5) * 0.4;
            armR.rotation.x = -Math.PI / 2 + 0.2 - gesture;  // forward & gesturing
            armR.rotation.z = 0.5;
            armL.rotation.x = -0.2;
            armL.rotation.z = 0.15;
            head.rotation.y = Math.sin(phase * 1.5) * 0.3;
            head.rotation.x = -0.05;
            break;
        }
        case 'reviewing': {
            // Hand-to-chin pose + slow head turn (arm raised forward & up)
            armR.rotation.x = -Math.PI * 0.55;  // forward and bent upward
            armR.rotation.z = 0.6;              // hand swings inward to chin
            armL.rotation.x = -0.1;
            head.rotation.y = Math.sin(phase * 0.6) * 0.35;
            head.rotation.x = -0.08;
            break;
        }
        case 'blocked': {
            // Hands up in frustration
            armL.rotation.x = -2.0;
            armR.rotation.x = -2.0;
            armL.rotation.z = 0.4;
            armR.rotation.z = -0.4;
            break;
        }
        case 'idle':
        case 'waiting':
        default: {
            // Cycle through three idle gestures every ~6 seconds, picking
            // by a hash of agent name so different agents fidget at
            // different times — looks like a living office, not robots.
            const cyclePhase = phase + agent.gestureSeed * 17;
            const gestureIndex = Math.floor(cyclePhase / 6) % 3;
            const localT = (cyclePhase % 6) / 6; // 0..1 within current gesture

            switch (gestureIndex) {
                case 0: {
                    // Stretch — both arms reach overhead briefly.
                    // +π rotation about X carries -Y → +Y (straight up).
                    const peak = Math.sin(localT * Math.PI);  // 0→1→0
                    armL.rotation.x = peak * Math.PI;
                    armR.rotation.x = peak * Math.PI;
                    armL.rotation.z = peak * 0.3;
                    armR.rotation.z = -peak * 0.3;
                    head.rotation.x = -peak * 0.2;
                    break;
                }
                case 1: {
                    // Look around — head turns side to side
                    head.rotation.y = Math.sin(localT * Math.PI * 2) * 0.5;
                    head.rotation.x = Math.sin(localT * Math.PI) * 0.05;
                    // Hands rest naturally at sides — slight sway
                    armL.rotation.z = 0.05 + Math.sin(phase * 1.2) * 0.04;
                    armR.rotation.z = -0.05 - Math.sin(phase * 1.2) * 0.04;
                    break;
                }
                case 2:
                default: {
                    // Weight shift — gentle torso lean side-to-side with
                    // arms swaying naturally at the sides. No "hands on
                    // hips" pose because we don't model elbows; that pose
                    // looks odd on this geometry.
                    const shift = Math.sin(localT * Math.PI * 2) * 0.04;
                    group.rotation.z = shift;
                    armL.rotation.z = 0.05 + Math.sin(phase * 1.5) * 0.06;
                    armR.rotation.z = -0.05 - Math.sin(phase * 1.5) * 0.06;
                    head.rotation.y = Math.sin(phase * 0.6 + agent.bobSeed) * 0.15;
                    break;
                }
            }
            break;
        }
    }
}

// ---------- Position assignment --------------------------------------

// Normalize a role to its base form so suffixed/duplicate roles map to a desk:
//   "frontend-engineer-2" -> "frontend-engineer", "backend-engineer-3" -> base.
function baseRole(role) {
    return String(role || "").replace(/-\d+$/, "");
}

// Stable per-NAME overflow slots for agents whose role has no fixed desk
// (e.g. "lead", "unknown", or a 2nd person in a role whose desk is taken).
// Placed along the back wall so they sit at a spot instead of floating.
const OVERFLOW_SLOTS = [
    { x: -3, z: -7.5, facing: 0 }, { x: 0, z: -7.5, facing: 0 },
    { x: 3, z: -7.5, facing: 0 },  { x: -7.5, z: -3, facing: Math.PI / 2 },
    { x: 7.5, z: -3, facing: -Math.PI / 2 }, { x: -7.5, z: 3, facing: Math.PI / 2 },
];
const overflowAssigned = new Map();  // name -> slot index
const deskClaim = new Map();          // desk role -> agent name that holds it

function overflowDesk(name) {
    if (!overflowAssigned.has(name)) {
        overflowAssigned.set(name, overflowAssigned.size % OVERFLOW_SLOTS.length);
    }
    return OVERFLOW_SLOTS[overflowAssigned.get(name)];
}

let nextMeetingSeat = 0;
function getDeskPosition(role, name) {
    // Try exact, then base-role (strip -2/-3 suffix). A desk is held by the
    // FIRST agent to claim it; a 2nd agent of the same role overflows so they
    // don't stack on one chair.
    let desk = DESK_LAYOUT.find(d => d.role === role)
            || DESK_LAYOUT.find(d => d.role === baseRole(role));
    if (desk) {
        const holder = deskClaim.get(desk.role);
        if (holder && holder !== name) desk = null;       // taken by someone else
        else deskClaim.set(desk.role, name);              // claim it
    }
    if (!desk) {
        const slot = overflowDesk(name || role || "");
        return { x: slot.x, z: slot.z, facing: slot.facing };
    }
    // The desk's local layout: monitor at -z, chair at +z (~0.85). The
    // character stands AT the chair location and must face the monitor
    // (i.e. point toward the desk centre, which is the OPPOSITE of the
    // desk's outward facing direction).
    const chairOffset = 0.85;
    const dx = Math.sin(desk.facing) * chairOffset;
    const dz = Math.cos(desk.facing) * chairOffset;
    return {
        x: desk.x + dx,
        z: desk.z + dz,
        // Face back into the desk so the character is looking at the screen.
        facing: desk.facing + Math.PI,
    };
}

function getMeetingPosition(name, partner) {
    // Stable seat assignment per pair
    const pair = [name, partner].sort();
    const hash = pair.join('').split('').reduce((a, c) => a + c.charCodeAt(0), 0);
    const seat1 = MEETING_SEATS[hash % MEETING_SEATS.length];
    const seat2 = MEETING_SEATS[(hash + 3) % MEETING_SEATS.length];
    return name === pair[0] ? seat1 : seat2;
}

function getTargetPosition(agent) {
    if (agent.status === 'discussing' && agent.talkingTo) {
        return getMeetingPosition(agent.name, agent.talkingTo);
    }
    return getDeskPosition(agent.role, agent.name);
}

// ---------- Spawn / update agents ------------------------------------

function spawnAgent(name, info) {
    const role = normaliseRole(info.role);
    const group = buildCharacter(role);
    scene.add(group);

    const target = getTargetPosition({
        name, role, status: info.status || 'idle', talkingTo: info.talking_to,
    });

    group.position.set(target.x, 0, target.z);
    group.rotation.y = target.facing || 0;

    // Name tag, status pip, speech bubble — all HTML elements positioned
    // by projecting world coords to screen each frame.
    const nameTag = document.createElement('div');
    nameTag.className = 'name-tag';
    const colors = ROLE_COLORS[role] || ROLE_COLORS.unknown;
    nameTag.innerHTML = `<span class="role-strip" style="background:${colors.tag}"></span>${escapeHtml(name)}`;
    overlay.appendChild(nameTag);

    const pip = document.createElement('div');
    pip.className = 'status-pip';
    overlay.appendChild(pip);

    const bubble = document.createElement('div');
    bubble.className = 'speech-bubble';
    overlay.appendChild(bubble);

    const agent = {
        name, role,
        status: info.status || 'idle',
        task: info.task || '',
        talkingTo: info.talking_to || null,
        message: info.message || null,
        lastSeen: info.last_seen,
        group,
        nameTag, pip, bubble,
        targetX: target.x, targetZ: target.z, targetFacing: target.facing || 0,
        animPhase: Math.random() * 100,
        bobSeed: Math.random() * 6.28,
        // Stable per-agent hash so each agent's idle gesture cycle starts
        // at a different point in the rotation (not in lock-step).
        gestureSeed: [...name].reduce((a, c) => a + c.charCodeAt(0), 0) % 60 / 10,
    };
    agents.set(name, agent);
}

function updateAgent(agent, info) {
    agent.role = info.role ? normaliseRole(info.role) : agent.role;
    agent.status = info.status || 'idle';
    agent.task = info.task || '';
    agent.talkingTo = info.talking_to || null;
    agent.message = info.message || null;
    agent.lastSeen = info.last_seen;

    const target = getTargetPosition(agent);
    agent.targetX = target.x;
    agent.targetZ = target.z;
    agent.targetFacing = target.facing || 0;

    // Update overlay content
    const colors = ROLE_COLORS[agent.role] || ROLE_COLORS.unknown;
    agent.nameTag.innerHTML =
        `<span class="role-strip" style="background:${colors.tag}"></span>${escapeHtml(agent.name)}`;
}

function removeAgent(agent) {
    scene.remove(agent.group);
    agent.group.traverse(o => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) {
            if (Array.isArray(o.material)) o.material.forEach(m => m.dispose());
            else o.material.dispose();
        }
    });
    agent.nameTag.remove();
    agent.pip.remove();
    agent.bubble.remove();
}

// ---------- SSE wiring -----------------------------------------------

function connect() {
    const ev = new EventSource('/events');
    ev.onmessage = (e) => {
        connected = true;
        document.getElementById('connection-dot').className = 'connected';
        try {
            applyState(JSON.parse(e.data));
        } catch (err) { console.error('state parse', err); }
    };
    ev.onerror = () => {
        connected = false;
        document.getElementById('connection-dot').className = 'disconnected';
        ev.close();
        setTimeout(connect, 3000);
    };
}

function applyState(state) {
    const now = Date.now();
    const seen = new Set();

    for (const [name, info] of Object.entries(state.agents || {})) {
        seen.add(name);
        const lastSeen = new Date(info.last_seen || 0).getTime();
        const stale = (now - lastSeen) > IDLE_TIMEOUT_MS;
        const status = stale ? 'offline' : (info.status || 'idle');
        const merged = { ...info, status };

        if (!agents.has(name)) {
            // Skip spawning offline agents — they only appear once active
            if (status === 'offline') continue;
            spawnAgent(name, merged);
        } else {
            updateAgent(agents.get(name), merged);
        }
    }

    // Remove agents that disappeared from the state file entirely
    for (const [name, agent] of agents) {
        if (!seen.has(name)) {
            removeAgent(agent);
            agents.delete(name);
        }
    }

    // Hide offline agents from the scene (still listed in sidebar)
    for (const agent of agents.values()) {
        agent.group.visible = agent.status !== 'offline';
        agent.nameTag.style.display = agent.status !== 'offline' ? '' : 'none';
        agent.pip.style.display     = agent.status !== 'offline' ? '' : 'none';
        agent.bubble.style.display  = agent.status !== 'offline' ? '' : 'none';
    }

    messages = state.messages || [];
    renderSidebar(state);
}

// ---------- Sidebar rendering ----------------------------------------

function renderSidebar(state) {
    // Chat
    const log = document.getElementById('chat-log');
    log.innerHTML = '';
    if (!messages.length) {
        log.innerHTML = '<div class="empty-chat">No messages yet</div>';
    } else {
        for (const msg of messages.slice(-30)) {
            const div = document.createElement('div');
            div.className = 'msg';
            const time = msg.ts ? new Date(msg.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
            div.innerHTML =
                `<span class="time">${escapeHtml(time)}</span>` +
                `<span class="from">${escapeHtml(msg.from)}</span>` +
                `<span class="arrow"> → </span>` +
                `<span class="to">${escapeHtml(msg.to)}</span>` +
                `<span class="text">${escapeHtml(msg.text)}</span>`;
            log.appendChild(div);
        }
        log.scrollTop = log.scrollHeight;
    }

    // Team
    const list = document.getElementById('agent-entries');
    const all = [...agents.values()];
    if (!all.length) {
        list.innerHTML = '<div class="empty-team">Waiting for agents…</div>';
    } else {
        list.innerHTML = '';
        const order = { working: 0, planning: 1, discussing: 2, reviewing: 3, waiting: 4, idle: 5, offline: 6 };
        all.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9) || a.name.localeCompare(b.name));
        for (const a of all) {
            const colors = ROLE_COLORS[a.role] || ROLE_COLORS.unknown;
            const row = document.createElement('div');
            row.className = 'agent-entry';
            row.innerHTML =
                `<span class="agent-dot dot-${a.status}"></span>` +
                `<span class="agent-name">${escapeHtml(a.name)}</span>` +
                `<span class="agent-role" style="color:${colors.tag}">${colors.label}</span>`;
            if (a.task) {
                const t = document.createElement('span');
                t.className = 'agent-task';
                t.textContent = a.task;
                row.appendChild(t);
            }
            list.appendChild(row);
        }
    }

    // Status bar
    const active = all.filter(a => !['idle', 'offline'].includes(a.status)).length;
    const convos = messages.length;
    document.getElementById('agent-count').textContent =
        `${active} active · ${convos} convos · ${all.length} agents`;
    document.getElementById('last-update').textContent =
        new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

// ---------- Per-frame loop -------------------------------------------

const tmpV = new THREE.Vector3();
let prevTime = performance.now();

// ---------- Caesar state machine -------------------------------------

// Caesar wanders at random, occasionally walks over to an idle agent
// to get pets, and sits at intervals. Bounded by the room footprint
// (avoids walking through walls).
function pickWanderTarget() {
    // Stay in the open floor — outside the desk perimeter
    const x = (Math.random() - 0.5) * 8;   // -4..4
    const z = (Math.random() - 0.5) * 6;   // -3..3
    return { x, z };
}

function findPettableAgent() {
    // Look for agents who are idle/working — Caesar wouldn't disturb a
    // discussion. Prefer ones at desks (predictable position).
    const candidates = [...agents.values()].filter(a =>
        a.group.visible && (a.status === 'working' || a.status === 'planning' || a.status === 'idle')
    );
    if (!candidates.length) return null;
    return candidates[Math.floor(Math.random() * candidates.length)];
}

function updateCaesar(now, dt) {
    const data = caesar.userData;
    data.animPhase += dt;

    // Tail wag — faster when being pet, gentle wag otherwise
    const wagSpeed = data.state === 'pet' ? 14 : (data.state === 'visit' ? 10 : 4);
    data.tailPivot.rotation.y = Math.sin(data.animPhase * wagSpeed) * 0.6;

    // Head bob
    data.headPivot.rotation.y = Math.sin(data.animPhase * 1.5) * 0.15;

    // State transitions
    if (now > data.stateUntil) {
        switch (data.state) {
            case 'wander': {
                // 20% chance to visit an agent, 30% chance to sit, otherwise wander again
                const r = Math.random();
                if (r < 0.20) {
                    const target = findPettableAgent();
                    if (target) {
                        data.state = 'visit';
                        data.target = target.name;
                        data.stateUntil = now + 8000;
                        break;
                    }
                }
                if (r < 0.50) {
                    data.state = 'sit';
                    data.stateUntil = now + 3000 + Math.random() * 3000;
                } else {
                    data.state = 'wander';
                    const t = pickWanderTarget();
                    data.targetX = t.x;
                    data.targetZ = t.z;
                    data.stateUntil = now + 4000 + Math.random() * 3000;
                }
                break;
            }
            case 'sit': {
                data.state = 'wander';
                const t = pickWanderTarget();
                data.targetX = t.x;
                data.targetZ = t.z;
                data.stateUntil = now + 4000 + Math.random() * 3000;
                break;
            }
            case 'visit': {
                // Lost interest / agent moved → back to wandering
                data.state = 'wander';
                data.target = null;
                const t = pickWanderTarget();
                data.targetX = t.x;
                data.targetZ = t.z;
                data.stateUntil = now + 3000;
                break;
            }
            case 'pet': {
                // Pet session over — wander off happy
                data.state = 'wander';
                data.target = null;
                const t = pickWanderTarget();
                data.targetX = t.x;
                data.targetZ = t.z;
                data.stateUntil = now + 3000;
                break;
            }
        }
    }

    // Visit: re-target to the agent's current position (they may have moved)
    if (data.state === 'visit' && data.target) {
        const agent = agents.get(data.target);
        if (!agent || !agent.group.visible) {
            // Agent gone — give up
            data.state = 'wander';
            data.target = null;
        } else {
            // Stand a bit in front of the agent so we can be petted
            const ag = agent.group;
            const facing = ag.rotation.y;
            const offset = 0.9;
            data.targetX = ag.position.x + Math.sin(facing) * offset;
            data.targetZ = ag.position.z + Math.cos(facing) * offset;

            // Arrived?
            const dx = data.targetX - caesar.position.x;
            const dz = data.targetZ - caesar.position.z;
            if (Math.hypot(dx, dz) < 0.25) {
                data.state = 'pet';
                data.stateUntil = now + 4000 + Math.random() * 2000;
                // Notify the agent so we can play a "pet" gesture for them
                agent._beingPet = true;
                agent._petUntil = data.stateUntil;
            }
        }
    }

    // Movement
    const moving = (data.state === 'wander' || data.state === 'visit');
    if (moving) {
        const dx = data.targetX - caesar.position.x;
        const dz = data.targetZ - caesar.position.z;
        const dist = Math.hypot(dx, dz);
        if (dist > 0.05) {
            const speed = data.state === 'visit' ? 2.6 : 1.6;  // excited speed when visiting
            const step = Math.min(speed * dt, dist);
            caesar.position.x += (dx / dist) * step;
            caesar.position.z += (dz / dist) * step;
            // Face direction of travel
            caesar.rotation.y = Math.atan2(dx, dz);
        }
    }

    // Leg animation: trot when moving, still when sitting/being pet
    const legs = data.legs;
    if (moving) {
        const swing = Math.sin(data.animPhase * 12) * 0.5;
        legs.fl.rotation.x = swing;
        legs.fr.rotation.x = -swing;
        legs.bl.rotation.x = -swing;
        legs.br.rotation.x = swing;
        // Body bob with the trot
        caesar.position.y = Math.abs(Math.sin(data.animPhase * 12)) * 0.04;
    } else if (data.state === 'sit') {
        // Sit pose: front legs straight, back haunches lowered (cheap fake)
        legs.fl.rotation.x = 0;
        legs.fr.rotation.x = 0;
        legs.bl.rotation.x = -0.6;
        legs.br.rotation.x = -0.6;
        caesar.position.y = -0.08;
    } else if (data.state === 'pet') {
        legs.fl.rotation.x = 0;
        legs.fr.rotation.x = 0;
        legs.bl.rotation.x = -0.6;
        legs.br.rotation.x = -0.6;
        caesar.position.y = -0.08;
        // Look up at the petter
        data.headPivot.rotation.x = -0.4;
    } else {
        legs.fl.rotation.x = 0;
        legs.fr.rotation.x = 0;
        legs.bl.rotation.x = 0;
        legs.br.rotation.x = 0;
        caesar.position.y = 0;
    }
}

function tick() {
    requestAnimationFrame(tick);

    const now = performance.now();
    const dt = Math.min((now - prevTime) / 1000, 0.05);
    prevTime = now;

    resize();

    // Move each agent toward its target position smoothly.
    for (const agent of agents.values()) {
        if (!agent.group.visible) continue;

        const g = agent.group;
        const dx = agent.targetX - g.position.x;
        const dz = agent.targetZ - g.position.z;
        const dist = Math.hypot(dx, dz);

        if (dist > 0.05) {
            const speed = 2.5;  // m/s
            const step = Math.min(speed * dt, dist);
            g.position.x += (dx / dist) * step;
            g.position.z += (dz / dist) * step;
            // Face direction of travel
            g.rotation.y = Math.atan2(dx, dz);
            agent._wasWalking = true;
        } else if (agent._wasWalking) {
            // Snap to facing on arrival
            g.rotation.y = agent.targetFacing;
            agent._wasWalking = false;
        }

        const animStatus = (dist > 0.1) ? 'walking' : agent.status;
        const stash = agent.status;
        agent.status = animStatus;
        setCharacterAnimation(agent, dt);
        agent.status = stash;
    }

    // Pulse the meeting-table glow when at least one agent is discussing
    const anyDiscussing = [...agents.values()].some(a => a.status === 'discussing');
    if (meetingTable.userData.glow) {
        const targetOpacity = anyDiscussing ? 0.32 + Math.sin(now / 400) * 0.08 : 0.08;
        meetingTable.userData.glow.material.opacity +=
            (targetOpacity - meetingTable.userData.glow.material.opacity) * 0.1;
    }

    // Animate steam particles drifting up off the coffee mug.
    if (coffeeCorner.userData.steamParts) {
        for (const part of coffeeCorner.userData.steamParts) {
            const t = (now / 1000 + part.userData.seed) % 2.0; // 2s loop
            const rise = t * 0.6;
            part.position.y = 1.2 + rise;
            part.position.x = 0.4 + Math.sin(t * 4 + part.userData.seed) * 0.04;
            part.material.opacity = Math.max(0, 0.5 - t * 0.25);
            const s = 1 + t * 0.6;
            part.scale.set(s, s, s);
        }
    }

    updateCaesar(now, dt);

    controls.update();
    renderer.render(scene, camera);

    // Update HTML overlays — project world position to screen pixels.
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    for (const agent of agents.values()) {
        if (!agent.group.visible) {
            agent.nameTag.classList.remove('visible');
            agent.pip.classList.remove('visible');
            agent.bubble.classList.remove('visible');
            continue;
        }

        // Name tag — at the character's hip-ish height so it floats above feet
        tmpV.set(agent.group.position.x, 2.1, agent.group.position.z);
        tmpV.project(camera);
        const sx = (tmpV.x * 0.5 + 0.5) * w;
        const sy = (-tmpV.y * 0.5 + 0.5) * h;
        const onscreen = tmpV.z < 1;
        if (onscreen) {
            agent.nameTag.style.left = sx + 'px';
            agent.nameTag.style.top  = sy + 'px';
            agent.nameTag.classList.add('visible');
        } else {
            agent.nameTag.classList.remove('visible');
        }

        // Status pip — above head
        tmpV.set(agent.group.position.x, 2.5, agent.group.position.z);
        tmpV.project(camera);
        const px = (tmpV.x * 0.5 + 0.5) * w;
        const py = (-tmpV.y * 0.5 + 0.5) * h;
        if (onscreen) {
            agent.pip.style.left = px + 'px';
            agent.pip.style.top  = py + 'px';
            agent.pip.className = `status-pip pip-${agent.status} visible`;
        } else {
            agent.pip.classList.remove('visible');
        }

        // Speech bubble — above head, only when there's a message and the agent is interactive
        if (agent.message && (agent.status === 'discussing' || agent.status === 'reviewing')) {
            tmpV.set(agent.group.position.x, 3.0, agent.group.position.z);
            tmpV.project(camera);
            const bx = (tmpV.x * 0.5 + 0.5) * w;
            const by = (-tmpV.y * 0.5 + 0.5) * h;
            if (onscreen) {
                agent.bubble.textContent = agent.message;
                agent.bubble.style.left = bx + 'px';
                agent.bubble.style.top  = by + 'px';
                agent.bubble.classList.add('visible');
            } else {
                agent.bubble.classList.remove('visible');
            }
        } else {
            agent.bubble.classList.remove('visible');
        }
    }

    // Caesar's name tag — float just above his back
    if (caesar.userData.tag) {
        tmpV.set(caesar.position.x, 1.0, caesar.position.z);
        tmpV.project(camera);
        const cx = (tmpV.x * 0.5 + 0.5) * w;
        const cy = (-tmpV.y * 0.5 + 0.5) * h;
        if (tmpV.z < 1) {
            caesar.userData.tag.style.left = cx + 'px';
            caesar.userData.tag.style.top  = cy + 'px';
            caesar.userData.tag.classList.add('visible');
        } else {
            caesar.userData.tag.classList.remove('visible');
        }
    }
}

// ---------- Utilities ------------------------------------------------

function escapeHtml(s) {
    return String(s ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}

// ---------- Boot -----------------------------------------------------

resize();
connect();
requestAnimationFrame(tick);
