// Listens to the mic and decides whether music is playing, so Gadget can stop sending audio to the
// voice model and dance instead. Everything runs locally; nothing here touches the network.
//
// Three cheap cues over a 6 s window of 25 ms hops:
//   beat: the loudness envelope repeats at 80-200 BPM (autocorrelation at 300-750 ms lags)
//   gaps: speech pauses between phrases; music rarely drops far below its own loud level
//   swing: speech loudness swings widely (coefficient of variation); music is steadier
// A smoothed score with hysteresis (several seconds to start, longer to stop) keeps a spoken
// sentence over a song from flipping the mode. Separately, bass onsets fire onBeat for the face.
const HOP_MS = 25, WIN = 240, EVAL_EVERY = 8;   // evaluate every 200 ms
const LAG_MIN = 12, LAG_MAX = 30;               // 300-750 ms in hops
const LO_HZ = 200, HI_HZ = 8000;
const QUIET_DB = -65;                           // total spectrum power below this = silence
const ENTER = 0.75, EXIT = 0.4, ENTER_SECS = 10, EXIT_SECS = 8;
const MIN_BEAT = 0.3, MAX_GAPS = 0.08;           // hard gates: no clear beat or any real pauses = not music
const BEAT_GAP_MS = 280, BEAT_RATIO = 1.4;

const clamp01 = (x) => Math.max(0, Math.min(1, x));

function beatStrength(x) {
  const n = x.length;
  let mean = 0;
  for (const v of x) mean += v;
  mean /= n;
  let r0 = 0;
  for (const v of x) r0 += (v - mean) ** 2;
  if (r0 < 1e-18) return 0;
  let best = 0;
  for (let lag = LAG_MIN; lag <= LAG_MAX; lag++) {
    let r = 0;
    for (let i = lag; i < n; i++) r += (x[i] - mean) * (x[i - lag] - mean);
    best = Math.max(best, (r / r0) * (n / (n - lag)));
  }
  return best;
}

// env / bass: loudness (amplitude) per hop, full band and below LO_HZ.
export function features(env, bass) {
  const n = env.length;
  const sorted = [...env].sort((a, b) => a - b);
  const p90 = sorted[Math.floor(n * 0.9)];
  const gaps = env.filter((v) => v < 0.12 * p90).length / n;
  let mean = 0;
  for (const v of env) mean += v;
  mean /= n;
  let varsum = 0;
  for (const v of env) varsum += (v - mean) ** 2;
  const cv = mean > 0 ? Math.sqrt(varsum / n) / mean : 0;
  return { beat: Math.max(beatStrength(bass), beatStrength(env)), gaps, cv };
}

export function score({ beat, gaps, cv }) {
  if (beat < MIN_BEAT || gaps > MAX_GAPS) return 0;   // conversation never reaches music from steadiness alone
  return 0.4 * clamp01((beat - 0.15) / 0.35) + 0.3 * clamp01(1 - gaps / 0.25) + 0.3 * clamp01((1 - cv) / 0.6);
}

export class MusicDetector {
  // cb: onChange(isMusic), onBeat(strength 0..1), suppress() -> true while Gadget itself is talking,
  // onDebug(info) for the ?music=1 overlay.
  constructor(ctx, source, cb) {
    this.cb = cb;
    this.analyser = ctx.createAnalyser();
    this.analyser.fftSize = 2048;
    this.analyser.smoothingTimeConstant = 0;
    source.connect(this.analyser);   // tap only; not routed to the speakers
    this.freq = new Float32Array(this.analyser.frequencyBinCount);
    const bw = ctx.sampleRate / this.analyser.fftSize;
    this.loBin = Math.max(2, Math.round(LO_HZ / bw));
    this.hiBin = Math.min(this.freq.length - 1, Math.round(HI_HZ / bw));
    this.env = [];
    this.bass = [];
    this.bAvg = 0;
    this.lastBeat = 0;
    this.hops = 0;
    this.ema = 0;
    this.music = false;
    this.holdoff = 0; // ignore the room until this time (the user just told us it's not music)
    this.since = 0;   // when the score first crossed the threshold for the current switch
    this.timer = setInterval(() => this.hop(), HOP_MS);
  }

  // The user overrode us (tapped to wake): don't re-enter dancing for a while.
  hold(ms) {
    this.holdoff = performance.now() + ms;
    this.ema = 0;
    this.since = 0;
  }

  stop() {
    clearInterval(this.timer);
    try { this.analyser.disconnect(); } catch {}
  }

  hop() {
    const { freq, loBin, hiBin, env, bass } = this;
    this.analyser.getFloatFrequencyData(freq);
    let total = 0, low = 0;
    for (let i = 1; i <= hiBin; i++) {
      const p = 10 ** (freq[i] / 10);
      total += p;
      if (i <= loBin) low += p;
    }
    const db = 10 * Math.log10(total + 1e-12), b = Math.sqrt(low), now = performance.now();
    env.push(Math.sqrt(total));
    bass.push(b);
    if (env.length > WIN) { env.shift(); bass.shift(); }

    if (db > QUIET_DB && b > BEAT_RATIO * this.bAvg && now - this.lastBeat > BEAT_GAP_MS) {
      this.lastBeat = now;
      this.cb.onBeat?.(clamp01((b / Math.max(this.bAvg, 1e-9) - BEAT_RATIO) / 1.5 + 0.4));
    }
    this.bAvg += (b - this.bAvg) * 0.05;

    if (++this.hops % EVAL_EVERY) return;
    const quiet = db < QUIET_DB || env.length < WIN;
    const f = quiet ? { beat: 0, gaps: 1, cv: 1 } : features(env, bass);
    this.ema += (score(f) - this.ema) * 0.15;
    this.update(now);
    this.cb.onDebug?.({ db, ...f, ema: this.ema, music: this.music });
  }

  update(now) {
    if (now < this.holdoff || this.cb.suppress?.()) { this.since = 0; return; }   // our own voice is not the room's music
    const crossing = this.music ? this.ema < EXIT : this.ema > ENTER;
    if (!crossing) { this.since = 0; return; }
    if (!this.since) this.since = now;
    if (now - this.since > (this.music ? EXIT_SECS : ENTER_SECS) * 1000) {
      this.music = !this.music;
      this.since = 0;
      this.cb.onChange?.(this.music);
    }
  }
}
