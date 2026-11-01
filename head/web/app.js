import { Face } from "./face.js";
import { createGemini } from "./providers/gemini.js";
import { createGrok } from "./providers/grok.js";

const STOP_WORDS = /\b(stop|halt|freeze|emergency)\b/i;
const PROVIDERS = { grok: createGrok, gemini: createGemini };

const $ = (id) => document.getElementById(id);
const face = new Face($("face"));
const caption = (t) => ($("caption").textContent = t);

let provider, head, ctx, analyser, nextPlay = 0, sources = [], micBuffer = [], open = false;
let wake, backoff = 500, pendingCalls = new Map(), stopFired = false;

// ---------- audio out ----------
function playChunk(b64) {
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
  const tick = () => (ctx.currentTime >= nextPlay ? res() : setTimeout(tick, 50));
  tick();
});

// level meter -> mouth, and speaking/idle state from queued audio
const levelBuf = new Uint8Array(512);
function meter() {
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
  head = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/head`);
  head.onmessage = async (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "face") face.setState(m.state);
    if (m.type === "claude_status") caption(m.text);
    if (m.type === "claude_result") {
      const call = pendingCalls.get(m.id);
      if (!call) return;
      pendingCalls.delete(m.id);
      if (provider.ackSpeaksFirst) await playbackDone(); // don't talk over the acknowledgement
      provider.sendToolResult(call.callId, call.name, m.text);
    }
  };
  head.onclose = () => setTimeout(connectHead, 1000);
}
const headSend = (o) => head?.readyState === 1 && head.send(JSON.stringify(o));

function emergencyStop() {
  flushPlayback();
  headSend({ type: "stop" });
  pendingCalls.clear();
}

// ---------- voice provider ----------
const callbacks = {
  onOpen() {
    backoff = 500;
    open = true;
    micBuffer.forEach((b) => provider.sendAudio(b));
    micBuffer = [];
    face.setState("listening");
    caption("");
  },
  onClose() {
    open = false;
    face.setState("idle");
    setTimeout(connectProvider, backoff);
    backoff = Math.min(backoff * 2, 15000);
  },
  onAudio: playChunk,
  onInterrupted: flushPlayback, // barge-in
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

async function connectProvider() {
  try {
    await provider.connect();
  } catch (err) {
    caption(`offline: ${err.message}`);
    setTimeout(connectProvider, backoff);
    backoff = Math.min(backoff * 2, 15000);
  }
}

// ---------- mic ----------
async function startMic() {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
  });
  await ctx.audioWorklet.addModule("audio-worklet.js");
  const node = new AudioWorkletNode(ctx, "pcm-capture", { processorOptions: { targetRate: provider.inRate } });
  ctx.createMediaStreamSource(stream).connect(node);
  node.port.onmessage = (e) => {
    if (open) provider.sendAudio(e.data);
    else { micBuffer.push(e.data); if (micBuffer.length > 100) micBuffer.shift(); } // ~10 s cap
  };
}

// ---------- boot (needs a user gesture on iOS) ----------
async function boot(name) {
  document.querySelector("#start").remove();
  $("stop").style.display = "block";
  provider = PROVIDERS[name](callbacks, { model: new URLSearchParams(location.search).get("model") || undefined });
  ctx = new AudioContext();
  await ctx.resume();
  analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  analyser.connect(ctx.destination);
  try { wake = await navigator.wakeLock?.request("screen"); } catch {}
  document.addEventListener("visibilitychange", async () => {
    if (document.visibilityState === "visible") { try { wake = await navigator.wakeLock?.request("screen"); } catch {} }
  });
  meter();
  connectHead();
  try { await startMic(); } catch (err) { caption(`mic: ${err.message}`); return; }
  connectProvider();
}

document.querySelectorAll("[data-provider]").forEach((b) => (b.onclick = () => boot(b.dataset.provider)));
const preselect = new URLSearchParams(location.search).get("provider");
if (preselect in PROVIDERS) document.querySelector(`[data-provider=${preselect}]`).classList.add("preselected");
$("stop").onclick = emergencyStop;
