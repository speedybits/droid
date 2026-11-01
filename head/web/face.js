// Animated face. States: idle | listening | thinking | speaking | moving.
// `mouth` (0..1) is driven by output-audio loudness while speaking.
export class Face {
  constructor(canvas) {
    this.c = canvas;
    this.g = canvas.getContext("2d");
    this.state = "idle";
    this.mouth = 0;          // smoothed
    this.mouthTarget = 0;
    this.blink = 0;          // 0 open .. 1 closed
    this.nextBlink = performance.now() + 2500;
    this.gaze = { x: 0, y: 0, tx: 0, ty: 0, next: 0 };
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
  setLevel(v) { this.mouthTarget = Math.min(1, v); }

  frame(now) {
    const { g, c } = this;
    const w = c.width, h = c.height, u = Math.min(w, h);
    const t = (now - this.t0) / 1000;

    // blinking
    if (now > this.nextBlink) { this.blink = 1; this.nextBlink = now + 2000 + Math.random() * 3500; }
    this.blink = Math.max(0, this.blink - 0.12);

    // gaze drift (look toward the side while "moving", up while thinking)
    if (now > this.gaze.next) {
      const m = this.state === "moving" ? 0.5 : 0.2;
      this.gaze.tx = (Math.random() - 0.5) * 2 * m;
      this.gaze.ty = this.state === "thinking" ? -0.4 : (Math.random() - 0.5) * 0.3;
      this.gaze.next = now + 800 + Math.random() * 1800;
    }
    this.gaze.x += (this.gaze.tx - this.gaze.x) * 0.08;
    this.gaze.y += (this.gaze.ty - this.gaze.y) * 0.08;

    this.mouthTarget = this.state === "speaking" ? this.mouthTarget : 0;
    this.mouth += (this.mouthTarget - this.mouth) * 0.35;

    g.fillStyle = "#000";
    g.fillRect(0, 0, w, h);
    const cx = w / 2, cy = h / 2;
    const color = { idle: "#7fd", listening: "#8f8", thinking: "#fc6", speaking: "#9ef", moving: "#f9a" }[this.state] || "#7fd";
    g.fillStyle = color;
    g.strokeStyle = color;
    g.shadowColor = color;
    g.shadowBlur = u * 0.04;

    // eyes
    const ex = u * 0.2, ey = cy - u * 0.12;
    const eyeW = u * 0.13, eyeH = u * 0.17 * (1 - this.blink * 0.92) * (this.state === "listening" ? 1.1 : 1);
    for (const s of [-1, 1]) {
      const x = cx + s * ex + this.gaze.x * u * 0.05;
      const y = ey + this.gaze.y * u * 0.05 + Math.sin(t * 1.3) * u * 0.004;
      g.beginPath();
      g.ellipse(x, y, eyeW / 2, Math.max(2, eyeH / 2), 0, 0, Math.PI * 2);
      g.fill();
    }

    // mouth
    const my = cy + u * 0.2;
    const mw = u * 0.22;
    g.lineWidth = u * 0.025;
    g.lineCap = "round";
    if (this.state === "speaking" || this.mouth > 0.03) {
      const mh = u * (0.012 + 0.12 * this.mouth);
      g.beginPath();
      g.ellipse(cx, my, mw * (0.7 + 0.3 * this.mouth), mh, 0, 0, Math.PI * 2);
      g.fill();
    } else if (this.state === "thinking") {
      g.beginPath();
      g.moveTo(cx - mw * 0.4, my);
      g.lineTo(cx + mw * 0.4, my);
      g.stroke();
    } else {
      g.beginPath(); // gentle smile
      g.arc(cx, my - u * 0.06, mw * 0.9, Math.PI * 0.2, Math.PI * 0.8);
      g.stroke();
    }
    g.shadowBlur = 0;
    requestAnimationFrame((n) => this.frame(n));
  }
}
