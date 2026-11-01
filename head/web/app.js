import { Face } from "./face.js";
import { createGemini, VOICES as GEMINI_VOICES } from "./providers/gemini.js";
import { createGrok, VOICES as GROK_VOICES } from "./providers/grok.js";

const STOP_WORDS = /\b(stop|halt|freeze|emergency)\b/i;
const PROVIDERS = { grok: createGrok, gemini: createGemini };
const VOICES = { grok: GROK_VOICES, gemini: GEMINI_VOICES };

const $ = (id) => document.getElementById(id);
const face = new Face($("face"));
const caption = (t) => ($("caption").textContent = t);

let provider, head, ctx, analyser, nextPlay = 0, sources = [], micBuffer = [], open = false;
let wake, backoff = 500, pendingCalls = new Map(), stopFired = false, muted = false;
let micStream, micNode, active = false;

// ---------- audio out ----------
function playChunk(b64) {
  if (muted) return; // late deltas from a response we cancelled
  const bin = atob(b64), n = bin.length / 2;
  const buf = ctx.createBuffer(1, n, provider.outRate), d = buf.getChannelData(0);
  for (let i = 0; i < n; i++) {
    let s = bin.charCodeAt(2 * i) | (bin.charCodeAt(2 * i + 1) << 8);
    if (s & 0x8000) s -= 0x10000;
    d[i] = s / 32768;
  }
  const src = ctx.createBufferSource();
  src.buffer = buf;
  src.connect(analyser);
  nextPlay = Math.max(nextPlay, ctx.currentTime + 0.02);
  src.start(nextPlay);
  nextPlay += buf.duration;
  sources.push(src);
  src.onended = () => (sources = sources.filter((s) => s !== src));
}

function flushPlayback() {
  sources.forEach((s) => { try { s.stop(); } catch {} });
  sources = [];
  nextPlay = 0;
}

const playbackDone = () => new Promise((res) => {
  const tick = () => (!active || ctx.currentTime >= nextPlay ? res() : setTimeout(tick, 50));
  tick();
});

// level meter -> mouth, and speaking/idle state from queued audio
const levelBuf = new Uint8Array(512);
function meter() {
  if (!active) return;
  analyser.getByteTimeDomainData(levelBuf);
  let sum = 0;
  for (const v of levelBuf) sum += (v - 128) ** 2;
  const rms = Math.sqrt(sum / levelBuf.length) / 128;
  if (ctx.currentTime < nextPlay) { face.setState("speaking"); face.setLevel(rms * 4); }
  else if (face.state === "speaking") face.setState(open ? "listening" : "idle");
  requestAnimationFrame(meter);
}

// ---------- head server link ----------
function connectHead() {
  const ws = (head = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/head`));
  ws.onmessage = async (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "face") face.setState(m.state);
    if (m.type === "claude_status") caption(m.text);
    if (m.type === "claude_result") {
      const call = pendingCalls.get(m.id);
      if (!call) return;
      pendingCalls.delete(m.id);
      if (provider.ackSpeaksFirst) await playbackDone(); // don't talk over the acknowledgement
      if (active) provider.sendToolResult(call.callId, call.name, m.text);
    }
  };
  ws.onclose = () => { if (active && head === ws) setTimeout(connectHead, 1000); };
}
const headSend = (o) => head?.readyState === 1 && head.send(JSON.stringify(o));

// Halt the robot and silence the voice; the session stays up (used for the spoken stop word).
function emergencyStop() {
  muted = true; // drop audio still in flight until the user speaks again
  flushPlayback();
  provider?.interrupt?.();
  headSend({ type: "stop" });
  pendingCalls.clear();
  face.setState(open ? "listening" : "idle");
  caption("Stopped");
}

// ---------- voice provider ----------
function callbacksFor(p) {
  const live = () => active && provider === p; // ignore stragglers from a session we already ended
  return {
    onOpen() {
      if (!live()) return;
      backoff = 500;
      open = true;
      micBuffer.forEach((b) => p.sendAudio(b));
      micBuffer = [];
      face.setState("listening");
      caption("");
    },
    onClose() {
      if (!live()) return;
      open = false;
      face.setState("idle");
      setTimeout(() => connectProvider(p), backoff);
      backoff = Math.min(backoff * 2, 15000);
    },
    onAudio(b64) { if (live()) playChunk(b64); },
    onInterrupted() { muted = false; flushPlayback(); }, // barge-in: the user is speaking, so unmute
    onUserText(text, final) {
      // React to "stop" on partials too; never wait on an LLM to stop the robot.
      if (!stopFired && STOP_WORDS.test(text)) { stopFired = true; emergencyStop(); }
      if (final) {
        stopFired = false;
        caption(text);
        headSend({ type: "transcript", role: "user", text });
      }
    },
    onModelText: (text) => headSend({ type: "transcript", role: "voice", text }),
    onToolCall({ callId, name, request }) {
      if (name !== "ask_claude") return;
      const id = crypto.randomUUID();
      pendingCalls.set(id, { callId, name });
      headSend({ type: "ask_claude", id, request });
      face.setState("thinking");
    },
    onError: (msg) => { console.warn("voice error:", msg); caption(msg); },
  };
}

async function connectProvider(p) {
  if (!active || provider !== p) return;
  try {
    await p.connect();
  } catch (err) {
    if (!active || provider !== p) return;
    caption(`offline: ${err.message}`);
    setTimeout(() => connectProvider(p), backoff);
    backoff = Math.min(backoff * 2, 15000);
  }
}

// ---------- mic ----------
async function startMic() {
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
  });
  await ctx.audioWorklet.addModule("audio-worklet.js");
  micNode = new AudioWorkletNode(ctx, "pcm-capture", { processorOptions: { targetRate: provider.inRate } });
  ctx.createMediaStreamSource(micStream).connect(micNode);
  micNode.port.onmessage = (e) => {
    if (open) provider.sendAudio(e.data);
    else { micBuffer.push(e.data); if (micBuffer.length > 100) micBuffer.shift(); } // ~10 s cap
  };
}

// ---------- session lifecycle (boot needs a user gesture on iOS) ----------
async function acquireWake() { try { wake = await navigator.wakeLock?.request("screen"); } catch {} }
document.addEventListener("visibilitychange", () => { if (active && document.visibilityState === "visible") acquireWake(); });

async function boot(name, voice) {
  $("start").style.display = "none";
  $("stop").style.display = "block";
  caption("");
  active = true;
  muted = stopFired = false;
  backoff = 500;
  const cb = {}; // providers call cb.* lazily, so fill it in once the provider exists
  const p = (provider = PROVIDERS[name](cb, { voice, model: new URLSearchParams(location.search).get("model") || undefined }));
  Object.assign(cb, callbacksFor(p));
  ctx = new AudioContext();
  await ctx.resume();
  analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  analyser.connect(ctx.destination);
  acquireWake();
  meter();
  connectHead();
  try { await startMic(); } catch (err) { caption(`mic: ${err.message}`); return; }
  if (active && provider === p) connectProvider(p);
}

// STOP button: halt the robot, then end the session and return to the selection screen.
function endSession() {
  emergencyStop();
  active = false;
  open = false;
  micNode?.port && (micNode.port.onmessage = null);
  micStream?.getTracks().forEach((t) => t.stop());
  micStream = micNode = null;
  micBuffer = [];
  provider?.close();
  head?.close();
  wake?.release?.().catch?.(() => {});
  wake = null;
  ctx?.close().catch(() => {});
  face.setState("idle");
  caption("");
  $("stop").style.display = "none";
  $("start").style.display = "flex";
}

// ---------- start screen ----------
const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
};

document.querySelectorAll("select[data-voice]").forEach((sel) => {
  const name = sel.dataset.voice;
  VOICES[name].forEach((v) => sel.add(new Option(v, v)));
  const saved = store.get(`voice.${name}`);
  if (saved && VOICES[name].includes(saved)) sel.value = saved;
  sel.onchange = () => store.set(`voice.${name}`, sel.value);
});

document.querySelectorAll("[data-provider]").forEach((b) => (b.onclick = () => {
  const name = b.dataset.provider;
  boot(name, document.querySelector(`select[data-voice=${name}]`).value);
}));
const preselect = new URLSearchParams(location.search).get("provider");
if (preselect in PROVIDERS) document.querySelector(`[data-provider=${preselect}]`).classList.add("preselected");
$("stop").onclick = endSession;
