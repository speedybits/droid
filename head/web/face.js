// Animated face, modeled on the reference sheet: glowing rounded-rect eyes whose shape, size and
// lids change with the mood, plus a small mouth. States: idle | listening | thinking | speaking | moving | sleeping | dancing.
// Emotions are a separate layer: setEmotion("happy", 3000) shows one briefly, then the face falls
// back to the mood of the current state. `mouth` (0..1) is driven by output-audio loudness.
//
// Eye spec (fractions of the screen's short side u):
//   kind: rect | arc (^) | dome (closed) | dash | chev (> <) | heart | swirl | x
//   w, h: size.  lid: how much of the top is covered (0..1).  tilt: top-lid slant, >0 inner end down
//   (angry), <0 inner end up (sad).  `R` overrides fields for the right eye (odd-sized eyes).
const E = (o) => ({ kind: "rect", w: 0.13, h: 0.17, lid: 0, tilt: 0, ...o });

const EMOTIONS = {
  neutral:    { eye: E({ w: 0.12, h: 0.15 }), mouth: "smile" },
  happy:      { eye: E({ kind: "arc" }), mouth: "grin" },
  laugh:      { eye: E({ kind: "chev" }), mouth: "bigGrin" },
  love:       { eye: E({ kind: "heart" }), mouth: "grin" },
  wink:       { eye: E({}), R: { kind: "arc" }, mouth: "smile" },
  sad:        { eye: E({ h: 0.16, tilt: -0.9, lid: 0.15 }), mouth: "frown" },
  nervous:    { eye: E({ h: 0.15, lid: 0.35 }), mouth: "wavy", sweat: true },
  angry:      { eye: E({ h: 0.14, tilt: 0.9, lid: 0.1 }), mouth: "frown" },
  determined: { eye: E({ h: 0.15, tilt: 0.5 }), mouth: "flat" },
  curious:    { eye: E({ w: 0.11, h: 0.13 }), R: { w: 0.14, h: 0.2 }, mouth: "tiny" },
  surprised:  { eye: E({ w: 0.15, h: 0.23 }), mouth: "o" },
  smug:       { eye: E({ h: 0.15, lid: 0.45 }), mouth: "smirk" },
  serious:    { eye: E({ h: 0.15, lid: 0.18 }), mouth: "flat" },
  deadpan:    { eye: E({ kind: "dash" }), mouth: "flat" },
  sleepy:     { eye: E({ kind: "dome" }), mouth: "tiny" },
  asleep:     { eye: E({ kind: "dome" }), mouth: "tiny" },
  dizzy:      { eye: E({ kind: "swirl", w: 0.14, h: 0.14 }), mouth: "wavy" },
  ko:         { eye: E({ kind: "x", w: 0.12, h: 0.12 }), mouth: "tiny" },
};
// Eyebrows: floating bars above the eyes. y = height above the eye (u), t = inner-end drop (u; negative
// raises the inner end: sad/worried). `R` overrides the right brow so one can rise (curious, smug).
const BROWS = {
  neutral: { y: 0.075, t: 0 },        happy: { y: 0.11, t: -0.008 },     laugh: { y: 0.115, t: -0.012 },
  love: { y: 0.11, t: -0.01 },        wink: { y: 0.075, t: 0, R: { y: 0.1 } },
  sad: { y: 0.08, t: -0.035 },        nervous: { y: 0.09, t: -0.028 },   angry: { y: 0.045, t: 0.045 },
  determined: { y: 0.06, t: 0.025 },  curious: { y: 0.065, t: 0.005, R: { y: 0.125, t: -0.012 } },
  surprised: { y: 0.14, t: -0.005 },  smug: { y: 0.06, t: 0.01, R: { y: 0.095, t: -0.005 } },
  deadpan: { y: 0.07, t: 0 },         serious: { y: 0.058, t: 0.018 },         sleepy: { y: 0.05, t: -0.01 },     asleep: { y: 0.03, t: 0 },     dizzy: { y: 0.09, t: -0.02 },
  ko: { y: 0.08, t: 0 },
};
// Resting mouth shapes (w = width, c = how far the middle sags: + smile, - frown, k = right corner lift), in u.
const MOUTHS = {
  smile: { w: 0.4, c: 0.05, k: 0 },  tiny: { w: 0.24, c: 0.03, k: 0 },  flat: { w: 0.4, c: 0, k: 0 },
  frown: { w: 0.36, c: -0.05, k: 0 }, smirk: { w: 0.4, c: 0.03, k: 0.035 },
};
// What the face shows when nothing more specific was asked for.
const MOOD = { idle: "neutral", listening: "neutral", thinking: "curious", speaking: "neutral", moving: "determined", sleeping: "asleep", dancing: "happy" };
const TONED = new Set(["idle", "listening", "speaking"]);
// While dancing the face cycles through these, one step every two beats.
const DANCE = ["happy", "laugh", "happy", "wink", "happy", "love", "laugh", "happy"];
const COLORS = { idle: "#8de", listening: "#8ee", thinking: "#fc6", speaking: "#9ef", moving: "#9df", sleeping: "#58a" };

export const EXPRESSIONS = Object.keys(EMOTIONS);

function roundRect(g, x, y, w, h, r) {
  g.moveTo(x + r, y);
  g.arcTo(x + w, y, x + w, y + h, r);
  g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r);
  g.arcTo(x, y, x + w, y, r);
  g.closePath();
}

export class Face {
  constructor(canvas) {
    this.c = canvas;
    this.g = canvas.getContext("2d");
    this.state = "idle";
    this.emotion = null;     // transient override
    this.tone = null;        // lingering conversation mood; replaces neutral for idle/listening/speaking
    this.emotionUntil = 0;
    this.mouth = 0;          // smoothed
    this.mouthTarget = 0;
    this.blink = 0;          // 0 open .. 1 closed
    this.nextBlink = performance.now() + 2500;
    this.gaze = { x: 0, y: 0, tx: 0, ty: 0, next: 0, vx: 0, vy: 0, px: 0, py: 0, jx: 0, jy: 0, nextJit: 0 };
    this.speed = 0;          // smoothed gaze speed, squashes the eyes during a glance
    this.mp = { w: 0.4, c: 0.05, k: 0 };   // current mouth shape (smoothed)
    this.md = { w: 0, c: 0, k: 0 };        // idle drift of the mouth
    this.nextMouth = 0;
    this.bp = { L: { y: 0.075, t: 0 }, R: { y: 0.075, t: 0 } }; // current brows (smoothed)
    this.pulse = 0;          // dance: jumps to ~1 on each beat, decays
    this.beats = 0;
    this.sway = 0;           // dance: smoothed left/right lean, flips each beat
    this.lastBeat = 0;
    this.look = null;
    this.nextLook = performance.now() + 2500;
    this.t0 = performance.now();
    addEventListener("resize", () => this.resize());
    this.resize();
    requestAnimationFrame((t) => this.frame(t));
  }

  resize() {
    const dpr = devicePixelRatio || 1;
    this.c.width = innerWidth * dpr;
    this.c.height = innerHeight * dpr;
    this.dpr = dpr;
  }

  setState(s) { this.state = s; }
  beat(strength = 1) {
    this.pulse = Math.max(this.pulse, 0.5 + 0.5 * strength);
    this.beats++;
    this.lastBeat = performance.now();
  }
  setTone(name) { this.tone = EMOTIONS[name] ? name : null; }
  setLevel(v) { this.mouthTarget = Math.min(1, v); }
  setEmotion(name, ms = 3500) {
    if (!EMOTIONS[name]) return;
    this.emotion = name;
    this.emotionUntil = performance.now() + ms;
  }

  // One eye. s is -1 for the left eye, +1 for the right; inner points toward the nose.
  drawEye(x, y, s, spec, u, t) {
    const { g } = this;
    // The eyeball changes shape as it looks around: the eye on the side being looked toward turns
    // away (narrower), the other turns toward us (wider); up/down changes height and lid; a fast
    // glance squashes the eye briefly.
    const gxn = Math.max(-1, Math.min(1, this.gaze.x / 2));
    const gyn = Math.max(-1, Math.min(1, this.gaze.y / 1.5));
    const squash = Math.min(0.25, this.speed * 0.8);
    const r = (spec.w * u / 2) * (1 - 0.16 * gxn * s) * (1 - squash * 0.6);
    const inner = -s;
    g.lineWidth = u * 0.022;
    if (spec.kind === "rect") {
      const hh = Math.max(u * 0.01, (spec.h * u / 2) * (1 - this.blink * 0.92) * (1 - gyn * 0.1) * (1 - 0.1 * Math.abs(gxn) * s * gxn) * (1 + squash * 0.5));
      spec = { ...spec, lid: Math.min(0.8, spec.lid + Math.max(0, gyn) * 0.2) }; // looking down lowers the lid
      const lidded = spec.lid > 0 || spec.tilt !== 0;
      g.beginPath();
      if (lidded) {
        // keep only the part of the eye below the (slanted) top lid
        const flat = y - hh + spec.lid * 2 * hh, drop = spec.tilt * hh * 0.8, ext = r * 1.3;
        g.save();
        g.shadowBlur = 0;
        roundRect(g, x - r, y - hh, 2 * r, 2 * hh, Math.min(r, hh) * 0.6);
        g.clip();
        g.beginPath();
        g.moveTo(x - inner * ext, flat - drop);
        g.lineTo(x + inner * ext, flat + drop);
        g.lineTo(x + inner * ext, y + hh + 2);
        g.lineTo(x - inner * ext, y + hh + 2);
        g.closePath();
        g.fill();
        g.restore();
      } else {
        roundRect(g, x - r, y - hh, 2 * r, 2 * hh, Math.min(r, hh) * 0.6);
        g.fill();
      }
    } else if (spec.kind === "arc") {           // happy ^
      g.beginPath();
      g.arc(x, y + r * 0.5, r * 1.05, Math.PI * 1.1, Math.PI * 1.9);
      g.stroke();
    } else if (spec.kind === "dome") {          // peacefully closed
      g.beginPath();
      g.arc(x, y - r * 0.4, r, Math.PI * 0.12, Math.PI * 0.88);
      g.stroke();
    } else if (spec.kind === "dash") {
      g.lineWidth = u * 0.03;
      g.beginPath();
      g.moveTo(x - r, y);
      g.lineTo(x + r, y);
      g.stroke();
    } else if (spec.kind === "chev") {          // > <  (laughing)
      const h = r * 0.9;
      g.beginPath();
      g.moveTo(x - inner * r, y - h);
      g.lineTo(x + inner * r * 0.8, y);
      g.lineTo(x - inner * r, y + h);
      g.stroke();
    } else if (spec.kind === "x") {
      g.beginPath();
      g.moveTo(x - r * 0.8, y - r * 0.8); g.lineTo(x + r * 0.8, y + r * 0.8);
      g.moveTo(x + r * 0.8, y - r * 0.8); g.lineTo(x - r * 0.8, y + r * 0.8);
      g.stroke();
    } else if (spec.kind === "heart") {
      const k = r * 1.1 * (1 + 0.08 * Math.sin(t * 6)); // beating
      g.beginPath();
      g.moveTo(x, y + k * 0.9);
      g.bezierCurveTo(x - k * 1.6, y - k * 0.1, x - k * 0.7, y - k * 1.2, x, y - k * 0.35);
      g.bezierCurveTo(x + k * 0.7, y - k * 1.2, x + k * 1.6, y - k * 0.1, x, y + k * 0.9);
      g.fill();
    } else if (spec.kind === "swirl") {
      g.lineWidth = u * 0.012;
      g.beginPath();
      for (let a = 0; a < Math.PI * 6; a += 0.2) {
        const rr = (a / (Math.PI * 6)) * r * 1.1, th = a + t * 5 * -s;
        a ? g.lineTo(x + Math.cos(th) * rr, y + Math.sin(th) * rr) : g.moveTo(x, y);
      }
      g.stroke();
    }
  }

  drawDream(bx, by, u, p, t) {
    const { g } = this;
    const fade = Math.min(1, Math.sin(p * Math.PI) * 3);
    g.save();
    g.shadowBlur = 0;
    g.globalAlpha = fade * 0.55;
    g.lineWidth = u * 0.008;
    for (const [dx, dy, r] of [[0.1, 0.27, 0.012], [0.06, 0.2, 0.02]]) { // trail of little bubbles toward the eyes
      g.beginPath(); g.arc(bx + dx * u, by + dy * u, r * u, 0, Math.PI * 2); g.stroke();
    }
    g.beginPath(); g.ellipse(bx, by, u * 0.2, u * 0.13, 0, 0, Math.PI * 2); g.stroke();
    g.beginPath(); g.ellipse(bx, by, u * 0.2, u * 0.13, 0, 0, Math.PI * 2); g.clip();
    // sheep hops left to right through the bubble
    const k = u * 0.011, hop = Math.abs(Math.sin(p * Math.PI * 6)) * 1.3;
    g.translate(bx + (p - 0.5) * u * 0.28 - u * 0.02, by - u * 0.005 - hop * k);
    g.globalAlpha = fade * 0.9;
    const cream = "#fed", navy = "#5a6cc0";
    const blob = (x, y, rx, ry, rot = 0) => { g.beginPath(); g.ellipse(x * k, y * k, rx * k, ry * k, rot, 0, Math.PI * 2); g.fill(); };
    g.lineCap = "round";
    g.strokeStyle = navy;                                    // long thin legs, trotting in pairs as it hops
    g.lineWidth = k * 0.4;
    for (const [lx, ph] of [[-2, 0], [-1.3, 1], [1.3, 0], [2, 1]]) {
      g.beginPath(); g.moveTo(lx * k, 1 * k); g.lineTo(lx * k, (3.2 - (hop > 0.6 ? 0.6 * ph : 0.6 * (1 - ph))) * k); g.stroke();
    }
    g.fillStyle = cream;                                     // woolly body and a big fluffy tail
    blob(0, 0, 2.9, 1.6);
    for (const [x, y] of [[-2.6, -0.4], [-1.8, -1.4], [-0.6, -1.7], [0.7, -1.7], [1.9, -1.2], [-2.5, 0.8], [-1.5, 1.4], [0, 1.6], [1.4, 1.4]]) blob(x, y, 0.8, 0.8);
    blob(-3.3, 0.3, 1.2, 1.4);
    g.fillStyle = navy;                                      // ear sticking out, then the dark face
    blob(2.1, -0.2, 1.2, 0.45, -0.35);
    blob(3.3, 0.5, 1.2, 1.5, 0.15);
    g.fillStyle = cream;                                     // wool tuft on top of the head
    blob(2.7, -1.3, 0.7, 0.6); blob(3.4, -1.45, 0.6, 0.55); blob(2.2, -1.0, 0.5, 0.5);
    g.fillStyle = "#fff";                                    // goggly eyes
    blob(3.0, 0, 0.5, 0.5); blob(3.9, -0.05, 0.5, 0.5);
    g.fillStyle = "#000";
    blob(3.12, 0.02, 0.2, 0.2); blob(4.02, -0.03, 0.2, 0.2);
    blob(4.2, 1.3, 0.3, 0.2);                                // nose
    g.strokeStyle = cream;                                   // antenna with a spark on the end
    g.lineWidth = k * 0.15;
    g.beginPath(); g.moveTo(3.0 * k, -1.7 * k); g.lineTo(3.3 * k, -2.7 * k); g.stroke();
    g.globalAlpha = fade * (0.5 + 0.5 * Math.sin(t * 12));
    g.fillStyle = cream; blob(3.3, -2.8, 0.3, 0.3);
    g.globalAlpha = fade * (0.6 + 0.4 * Math.sin(t * 14));   // lightning bolt on its side
    g.fillStyle = navy;
    g.beginPath();
    g.moveTo(0.3 * k, -1.1 * k); g.lineTo(-0.6 * k, 0.2 * k); g.lineTo(0 * k, 0.2 * k); g.lineTo(-0.5 * k, 1.2 * k); g.lineTo(0.8 * k, -0.2 * k); g.lineTo(0.1 * k, -0.2 * k);
    g.closePath(); g.fill();
    g.restore();
  }

  frame(now) {
    const { g, c } = this;
    const w = c.width, h = c.height, u = Math.min(w, h);
    const t = (now - this.t0) / 1000;
    const dancing = this.state === "dancing";
    const name = this.emotion && now < this.emotionUntil ? this.emotion
      : dancing ? DANCE[(this.beats >> 1) % DANCE.length] : (this.tone && TONED.has(this.state) ? this.tone : MOOD[this.state] || "neutral");
    const e = EMOTIONS[name];

    // blinking (only visible on rect eyes)
    if (now > this.nextBlink) { this.blink = 1; this.nextBlink = now + 2000 + Math.random() * 3500; }
    this.blink = Math.max(0, this.blink - 0.12);

    const asleep = this.state === "sleeping";
    if (asleep) { this.gaze.tx = 0; this.gaze.ty = 0.5; this.gaze.next = now + 500; } // head down, still

    // gaze drift (look toward the side while "moving", up while thinking)
    if (now > this.gaze.next) {
      const m = this.state === "moving" ? 0.5 : 0.2;
      this.gaze.tx = (Math.random() - 0.5) * 2 * m;
      this.gaze.ty = this.state === "thinking" ? -0.4 : (Math.random() - 0.5) * 0.3;
      this.gaze.next = now + 800 + Math.random() * 1800;
    }
    // idle: now and then look around properly (a side, up, or a slow sweep), hold, then settle back
    if (this.state === "idle" || this.state === "listening") {
      if (!this.look && now > this.nextLook) {
        const spots = [[-2.2, 0], [2.2, 0], [-1.8, -1.2], [1.8, -1.2], [0, -1.6], [-2, 0.8], [2, 0.8]];
        const first = spots[Math.floor(Math.random() * spots.length)];
        const sweep = Math.random() < 0.4 ? [-first[0], first[1] * 0.5] : null; // glance across to the other side
        this.look = { steps: sweep ? [first, sweep] : [first], i: 0, until: now + 900 + Math.random() * 700 };
      }
      if (this.look) {
        const [lx, ly] = this.look.steps[this.look.i];
        this.gaze.tx = lx;
        this.gaze.ty = ly;
        this.gaze.next = now + 300; // hold the drift off while looking
        if (now > this.look.until) {
          if (++this.look.i < this.look.steps.length) this.look.until = now + 900 + Math.random() * 600;
          else { this.look = null; this.nextLook = now + 3500 + Math.random() * 5000; this.gaze.next = 0; }
        }
      }
    } else if (this.look) {
      this.look = null;
    }
    // Eyes snap to a target like real saccades (a quick spring with a hint of overshoot), then hold
    // with tiny involuntary jitter. Big shifts often come with a blink.
    const gz = this.gaze;
    if (Math.hypot(gz.tx - gz.px, gz.ty - gz.py) > 1.2 && Math.random() < 0.6) this.blink = 1;
    gz.px = gz.tx; gz.py = gz.ty;
    if (now > gz.nextJit) {
      gz.jx = (Math.random() - 0.5) * 0.14; gz.jy = (Math.random() - 0.5) * 0.1;
      gz.nextJit = now + 150 + Math.random() * 350;
    }
    gz.vx = (gz.vx + (gz.tx + gz.jx - gz.x) * 0.2) * 0.6;
    gz.vy = (gz.vy + (gz.ty + gz.jy - gz.y) * 0.2) * 0.6;
    gz.x += gz.vx; gz.y += gz.vy;
    this.speed = this.speed * 0.8 + Math.hypot(gz.vx, gz.vy) * 0.2;

    this.mouthTarget = this.state === "speaking" ? this.mouthTarget : 0;
    this.mouth += (this.mouthTarget - this.mouth) * 0.35;

    g.fillStyle = "#000";
    g.fillRect(0, 0, w, h);
    const cx = w / 2, cy = h / 2;
    // Dancing: the whole face bobs, leans to alternating sides on the beat and swells slightly. With
    // no recent beat it keeps a gentle groove of its own.
    g.save();
    if (dancing) {
      this.pulse *= 0.9;
      const beating = now - this.lastBeat < 1200;
      this.sway += ((beating ? (this.beats & 1 ? 1 : -1) : Math.sin(t * 2.5)) - this.sway) * 0.18;
      const bob = beating ? this.pulse : 0.2 + 0.2 * Math.sin(t * 5);
      g.translate(this.sway * u * 0.03, -bob * u * 0.04);
      g.translate(cx, cy);
      g.rotate(this.sway * 0.07);
      g.scale(1 + this.pulse * 0.05, 1 + this.pulse * 0.05);
      g.translate(-cx, -cy);
    }
    const color = dancing ? `hsl(${(t * 40 + this.beats * 25) % 360} 85% 72%)` : name === "love" ? "#f9b" : name === "sad" || name === "nervous" ? "#9cf" : COLORS[this.state] || "#8de";
    g.fillStyle = color;
    g.strokeStyle = color;
    g.shadowColor = color;
    g.shadowBlur = u * 0.04;
    g.lineWidth = u * 0.025;
    g.lineCap = "round";
    g.lineJoin = "round";

    // eyes (the right one can differ in size, like the curious face)
    const ex = u * 0.2, ey = cy - u * 0.1;
    for (const s of [-1, 1]) {
      const spec = s === 1 && e.R ? { ...e.eye, ...e.R } : e.eye;
      const x = cx + s * ex + this.gaze.x * u * 0.05;
      const y = ey + this.gaze.y * u * 0.05 + Math.sin(t * (asleep ? 0.6 : 1.3)) * u * (asleep ? 0.012 : 0.004); // slow breathing when asleep
      this.drawEye(x, y, s, spec, u, t);
    }

    // eyebrows ease toward the mood's pose; they ride up when the eyes look up and with the eyes' glances
    const bt = BROWS[name] || BROWS.neutral;
    const lift = this.state === "listening" ? 0.008 : 0;
    const gyn = Math.max(-1, Math.min(1, this.gaze.y / 1.5));
    for (const s of [-1, 1]) {
      const tgt = s === 1 && bt.R ? { ...bt, ...bt.R } : bt;
      const cur = s === 1 ? this.bp.R : this.bp.L;
      cur.y += ((tgt.y + lift - gyn * 0.015) - cur.y) * 0.15;
      cur.t += (tgt.t - cur.t) * 0.15;
      const x = cx + s * ex + this.gaze.x * u * 0.05, by = ey + this.gaze.y * u * 0.04 - u * (0.06 + cur.y);
      const half = u * 0.055, inner = -s;
      g.globalAlpha = 0.8;
      g.lineWidth = u * 0.018;
      g.beginPath();
      g.moveTo(x - inner * half, by - cur.t * u * 0.5);   // outer end
      g.lineTo(x + inner * half, by + cur.t * u);          // inner end
      g.stroke();
      g.globalAlpha = 1;
    }

    // sweat drop (nervous)
    if (e.sweat) {
      const sx = cx + ex * 1.55, sy = ey - u * 0.1 + (Math.sin(t * 2) + 1) * u * 0.008;
      g.beginPath();
      g.moveTo(sx, sy - u * 0.04);
      g.bezierCurveTo(sx + u * 0.035, sy, sx + u * 0.03, sy + u * 0.045, sx, sy + u * 0.045);
      g.bezierCurveTo(sx - u * 0.03, sy + u * 0.045, sx - u * 0.035, sy, sx, sy - u * 0.04);
      g.fill();
    }

    // Zzz drifting up and to the side, three letters in staggered phases
    if (asleep) {
      g.shadowBlur = 0;
      g.textAlign = "center";
      for (let i = 0; i < 3; i++) {
        const ph = ((t * 0.25 + i / 3) % 1);
        g.globalAlpha = Math.sin(ph * Math.PI) * 0.8;
        g.font = `bold ${u * (0.05 + 0.04 * ph)}px sans-serif`;
        g.fillText("z", cx + ex * 1.3 + ph * u * 0.12, ey - u * 0.08 - ph * u * 0.22);
      }
      g.globalAlpha = 1;
      g.shadowBlur = u * 0.04;
    }

    // Now and then it dreams of electric sheep: a thought bubble with a wired-up sheep hopping across
    if (asleep) {
      if (!this.nextDream) this.nextDream = now + 5000 + Math.random() * 6000;
      if (!this.dreamAt && now > this.nextDream) this.dreamAt = now;
      if (this.dreamAt) {
        const p = (now - this.dreamAt) / 7000;
        if (p >= 1) { this.dreamAt = 0; this.nextDream = now + 12000 + Math.random() * 18000; }
        else this.drawDream(cx - u * 0.14, ey - u * 0.34, u, p, t);
      }
    } else this.dreamAt = 0;

    // mouth (follows the glance a little, as if the head turned)
    g.save();
    g.translate(this.gaze.x * u * 0.02, this.gaze.y * u * 0.01);
    const my = cy + u * 0.17;
    const mw = u * 0.2;
    if (this.state === "speaking" || this.mouth > 0.03) {
      const mh = u * (0.012 + 0.11 * this.mouth);
      g.beginPath();
      g.ellipse(cx, my, mw * (0.6 + 0.3 * this.mouth), mh, 0, 0, Math.PI * 2);
      g.fill();
    } else if (MOUTHS[e.mouth]) {
      // Resting mouths never sit still: they breathe, and every few seconds drift to a slightly
      // different smile (wider, flatter, a little crooked) while the robot is idle or listening.
      const base = MOUTHS[e.mouth], calm = this.state === "idle" || this.state === "listening";
      if (now > this.nextMouth) {
        this.md = { w: (Math.random() - 0.5) * 0.14, c: (Math.random() - 0.35) * 0.05, k: (Math.random() - 0.5) * 0.03 };
        this.nextMouth = now + 1500 + Math.random() * 3000;
      }
      const d = calm ? this.md : { w: 0, c: 0, k: 0 };
      const want = { w: base.w + d.w, c: base.c + d.c + Math.sin(t * 1.6) * 0.006 + (this.state === "listening" ? 0.012 : 0), k: base.k + d.k };
      for (const key of ["w", "c", "k"]) this.mp[key] += (want[key] - this.mp[key]) * 0.06;
      const { w: mw2, c, k } = this.mp, x0 = cx - mw2 * u / 2, x1 = cx + mw2 * u / 2;
      g.beginPath();
      g.moveTo(x0, my + k * u * 0.5);
      g.quadraticCurveTo(cx, my + c * u * 2, x1, my - k * u);
      g.stroke();
    } else {
      g.beginPath();
      switch (e.mouth) {
        case "grin":
          g.arc(cx, my - u * 0.04, mw * 0.55, 0, Math.PI);
          g.closePath();
          g.fill();
          break;
        case "bigGrin":
          g.arc(cx, my - u * 0.06, mw * 0.8, 0, Math.PI);
          g.closePath();
          g.fill();
          break;
        case "wavy":
          g.moveTo(cx - mw * 0.55, my);
          g.bezierCurveTo(cx - mw * 0.3, my - u * 0.025, cx - mw * 0.1, my + u * 0.025, cx, my);
          g.bezierCurveTo(cx + mw * 0.1, my - u * 0.025, cx + mw * 0.3, my + u * 0.025, cx + mw * 0.55, my);
          g.stroke();
          break;
        case "o":
          g.ellipse(cx, my, u * 0.035, u * 0.05, 0, 0, Math.PI * 2);
          g.fill();
          break;
      }
    }
    g.restore();
    g.restore(); // the dance transform
    g.shadowBlur = 0;
    requestAnimationFrame((n) => this.frame(n));
  }
}
