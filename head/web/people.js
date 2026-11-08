// People view for the simulator: nimbus's map, droid, a 1 m hearing ring, people and speech bubbles.
// Pure display; the server owns all state (GET /sim/world once, GET /sim/people polled).

const cv = document.getElementById("c"), ctx = cv.getContext("2d");
const $ = (id) => document.getElementById(id);
let world = null, wallCanvas = null, snap = null;
const trail = [];
const ROOM_HUES = { "living room": 210, bedroom: 280, office: 40, kitchen: 150 };

function fit() {
  const r = cv.getBoundingClientRect(), d = devicePixelRatio || 1;
  cv.width = r.width * d; cv.height = r.height * d;
}
addEventListener("resize", fit);

function buildWalls() {
  wallCanvas = document.createElement("canvas");
  wallCanvas.width = world.width; wallCanvas.height = world.height;
  const w = wallCanvas.getContext("2d");
  w.fillStyle = "#8b93a7";
  world.rows.forEach((row, gy) => {
    for (let gx = 0; gx < row.length; gx++) if (row[gx] === "#") w.fillRect(gx, world.height - 1 - gy, 1, 1);
  });
}

function view() {
  const mw = world.width * world.resolution, mh = world.height * world.resolution;
  const top = 44 * (devicePixelRatio || 1);
  const s = Math.min(cv.width / mw, (cv.height - top) / mh) * 0.96;
  const ox = (cv.width - mw * s) / 2, oy = top + (cv.height - top - mh * s) / 2;
  return { s, ox, oy, mw, mh,
    px: (x) => ox + (x + world.origin_x) * s,
    py: (y) => oy + (mh - (y + world.origin_y)) * s };
}

function wrap(text, max) {
  const out = []; let line = "";
  for (const w of text.split(" ")) {
    if ((line + " " + w).trim().length > max) { out.push(line); line = w; } else line = (line + " " + w).trim();
  }
  if (line) out.push(line);
  return out;
}

function bubble(v, x, y, text, fill, stroke, lift) {
  const d = devicePixelRatio || 1, fs = 12 * d, lines = wrap(text, 26);
  ctx.font = `${fs}px system-ui, sans-serif`;
  const w = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 14 * d, h = lines.length * fs * 1.25 + 10 * d;
  const bx = Math.min(Math.max(x - w / 2, 4), cv.width - w - 4), by = y - lift * d - h;
  ctx.fillStyle = fill; ctx.strokeStyle = stroke; ctx.lineWidth = 1.5 * d;
  ctx.beginPath(); ctx.roundRect(bx, by, w, h, 8 * d); ctx.fill(); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(x - 5 * d, by + h); ctx.lineTo(x, y - (lift - 8) * d); ctx.lineTo(x + 5 * d, by + h); ctx.fill();
  ctx.fillStyle = "#111";
  lines.forEach((l, i) => ctx.fillText(l, bx + 7 * d, by + 5 * d + fs * (1.05 + i * 1.25)));
}

function draw() {
  ctx.fillStyle = "#10131a"; ctx.fillRect(0, 0, cv.width, cv.height);
  if (!world) return;
  const v = view(), d = devicePixelRatio || 1;
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(wallCanvas, v.ox, v.oy, v.mw * v.s, v.mh * v.s);
  if (!snap) return;

  ctx.font = `${13 * d}px system-ui, sans-serif`; ctx.textAlign = "center";
  for (const r of snap.rooms) { ctx.fillStyle = "rgba(200,220,255,.25)"; ctx.fillText(r.name, v.px(r.x), v.py(r.y) + 4 * d); }

  if (trail.length > 1) {
    ctx.strokeStyle = "rgba(90,200,250,.45)"; ctx.lineWidth = 2 * d; ctx.beginPath();
    trail.forEach((p, i) => (i ? ctx.lineTo(v.px(p[0]), v.py(p[1])) : ctx.moveTo(v.px(p[0]), v.py(p[1]))));
    ctx.stroke();
  }
  const dr = snap.droid, pose = snap.pose;
  if (dr) { // hearing range
    ctx.strokeStyle = "rgba(52,199,89,.6)"; ctx.setLineDash([6 * d, 5 * d]); ctx.lineWidth = 1.5 * d;
    ctx.beginPath(); ctx.arc(v.px(dr.x), v.py(dr.y), snap.hear_range * v.s, 0, 7); ctx.stroke(); ctx.setLineDash([]);
  }
  for (const p of snap.people) {
    const hue = ROOM_HUES[p.room] ?? 200, x = v.px(p.x), y = v.py(p.y), r = 0.14 * v.s;
    ctx.fillStyle = `hsl(${hue} 70% ${p.speaking ? 70 : 52}%)`;
    ctx.strokeStyle = p.solo ? "#ffd60a" : "#0008"; ctx.lineWidth = 2 * d;
    ctx.beginPath(); ctx.arc(x, y, r, 0, 7); ctx.fill(); ctx.stroke();
    ctx.fillStyle = "#9aa4b8"; ctx.font = `${10 * d}px system-ui`; ctx.fillText(p.name, x, y + r + 11 * d);
  }
  if (dr && pose) {
    const x = v.px(dr.x), y = v.py(dr.y), r = 0.15 * v.s;
    ctx.fillStyle = "#34c759"; ctx.beginPath(); ctx.arc(x, y, r, 0, 7); ctx.fill();
    ctx.strokeStyle = "#0b3d1a"; ctx.lineWidth = 3 * d; ctx.beginPath();
    ctx.moveTo(x, y); ctx.lineTo(x + r * 1.4 * Math.cos(-pose.theta), y + r * 1.4 * Math.sin(-pose.theta)); ctx.stroke();
  }
  ctx.textAlign = "left";
  const reveal = $("reveal").checked;
  for (const b of snap.bubbles) { // droid hears only within the ring; out of range the bubble says so
    const text = b.heard || reveal ? b.text : "<too far away to hear>";
    bubble(v, v.px(b.x), v.py(b.y), text, b.heard ? "#caf7d4" : "#f1f3f8", b.heard ? "#34c759" : "#8b93a7", 14);
  }
}

// Chrome's built-in voices read the people's lines aloud (no API, no cost). Each person gets a stable voice/pitch
// from their name. Needs one user gesture, so it's behind a checkbox.
const spoken = new Set();
let armed = false; // Chrome only speaks after one click on the page
addEventListener("pointerdown", () => { armed = true; $("hint").textContent = ""; speechSynthesis.getVoices(); }, { once: true });
function sayAloud(b) {
  if (!b.heard) return; // only people within droid's hearing range
  const key = `${b.gid}|${b.name}|${b.text}`;
  if (spoken.has(key)) return;
  spoken.add(key); if (spoken.size > 200) spoken.delete(spoken.values().next().value);
  if (!$("speak").checked || !("speechSynthesis" in window)) return;
  if (!armed) { $("hint").textContent = "click anywhere so Chrome will let me speak"; return; }
  if (speechSynthesis.pending) return; // don't build up a backlog of stale lines
  let h = 0; for (const c of b.name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const voices = speechSynthesis.getVoices().filter((v) => v.lang.startsWith("en"));
  const u = new SpeechSynthesisUtterance(b.text);
  if (voices.length) u.voice = voices[h % voices.length];
  u.pitch = 0.8 + ((h >> 3) % 9) * 0.05; u.rate = 1.05;
  speechSynthesis.speak(u);
}
try { $("speak").checked = localStorage.getItem("people.speak") !== "0"; } catch { $("speak").checked = true; }
$("speak").addEventListener("change", () => {
  try { localStorage.setItem("people.speak", $("speak").checked ? "1" : "0"); } catch {}
  if (!$("speak").checked) speechSynthesis.cancel(); else speechSynthesis.getVoices();
});

async function poll() {
  try {
    const r = await fetch("/sim/people");
    if (!r.ok) { $("state").textContent = r.status === 404 ? "seeker is off (run with SEEKER=1 against nimbus --sim)" : `error ${r.status}`; return; }
    snap = await r.json();
    for (const b of snap.bubbles) sayAloud(b);
    const dr = snap.droid;
    if (dr && (!trail.length || Math.hypot(dr.x - trail.at(-1)[0], dr.y - trail.at(-1)[1]) > 0.03)) {
      trail.push([dr.x, dr.y]); if (trail.length > 1500) trail.shift();
    }
    const sk = snap.seeker;
    $("state").textContent = `droid: ${sk.paused ? "paused" : sk.state}`;
    $("room").textContent = dr?.room ? `in the ${dr.room}` : "";
    $("err").textContent = snap.voice_error ? `voice error: ${snap.voice_error}` : "";
    $("tok").textContent = `tokens  sim ${snap.tokens.sim}  droid ${snap.tokens.droid}`;
  } catch { $("state").textContent = "head server unreachable"; }
}

(async () => {
  fit();
  for (;;) {
    try { world = await (await fetch("/sim/world")).json(); buildWalls(); break; }
    catch { $("state").textContent = "waiting for nimbus --sim..."; await new Promise((r) => setTimeout(r, 1500)); }
  }
  setInterval(poll, 200);
  (function loop() { draw(); requestAnimationFrame(loop); })();
})();
