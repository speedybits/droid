import { Face } from "./face.js?v=8";
import { createGemini, VOICES as GEMINI_VOICES } from "./providers/gemini.js";
import { createGrok, VOICES as GROK_VOICES } from "./providers/grok.js?v=8";
import { MusicDetector } from "./music.js?v=8";
import { newSession, sessionId } from "./providers/common.js";

const STOP_WORDS = /\b(stop|halt|freeze|emergency)\b/i;
const WAKE_WORD = /\b(hey|hay|hi|okay|ok)[, ]+(gadget|gadgit|gadjet|gadgets)\b/i;
const SLEEP_WORDS = /\b(go to sleep|good ?night|sleep now)\b/i;
const IDLE_SECS = Number(new URLSearchParams(location.search).get("sleep")) || 60; // quiet this long -> sleep
const PROVIDERS = { grok: createGrok, gemini: createGemini };
const VOICES = { grok: GROK_VOICES, gemini: GEMINI_VOICES };

const $ = (id) => document.getElementById(id);
const face = new Face($("face"));
const preview = new URLSearchParams(location.search).get("face"); // ?face=<name> shows one expression
if (preview) face.setEmotion(preview, 1e9);
const caption = (t) => ($("caption").textContent = t);

let provider, head, ctx, analyser, nextPlay = 0, sources = [], micBuffer = [], open = false;
let wake, backoff = 500, pendingCalls = new Map(), stopFired = false, muted = false;
let micStream, micNode, active = false;
let sleeping = false, lastActivity = 0, sleepAt = 0, idleTimer, rec;
let dancing = false, detector; // music in the room: voice session closed (like sleep), face dances

// ---------- cost estimate (voice is billed per minute; list prices, an estimate only) ----------
const RATE_PER_MIN = { grok: 0.08, gemini: 0.02 };
let sessionName, sessionSecs = 0, costTimer;
const today = () => `cost.${new Date().toLocaleDateString("en-CA")}`; // local date (en-CA is YYYY-MM-DD); UTC rolled over mid-evening

function startCost(name) {
  sessionName = name;
  sessionSecs = 0;
  costTimer = setInterval(() => { if (open) sessionSecs++; }, 1000); // connected time only
}

function stopCost() {
  clearInterval(costTimer);
  const add = sessionSecs * (RATE_PER_MIN[sessionName] / 60);
  store.set(today(), String((Number(store.get(today())) || 0) + add));
  sessionSecs = 0;
}

// ---------- expressions: a cheap keyword read of what was said (no extra model round trip) ----------
const MOODS = [
  [/\b(love|adore|sweet|you're the best)\b/i, "love"],
  [/\b(sorry|unfortunately|can't|cannot|couldn't|unable|sad|oh no)\b/i, "sad"],
  [/\b(careful|watch out|blocked|stuck|obstacle|trouble|problem)\b/i, "nervous"],
  [/\b(haha|ha ha|lol|hilarious)\b/i, "laugh"],
  [/\b(wow|whoa|really|no way|amazing|surprise)\b/i, "surprised"],
  [/\b(good ?night|sleep|tired|nap)\b/i, "sleepy"],
  [/\b(haha|funny|joke|glad|great|thank|thanks|hello|hi there|hey|welcome|happy|nice|good)\b/i, "happy"],
  [/\b(wink|just kidding|secret)\b/i, "wink"],
  [/\b(obviously|of course|easy|told you)\b/i, "smug"],
  [/\b(on it|going|heading|moving|let's go)\b/i, "determined"],
  [/\b(hmm|wonder|what's that|how come|why)\b|\?\s*$/i, "curious"],
];
function express(text, ms) {
  if (SERIOUS.test(text)) seriousUntil = Date.now() + 90000;
  const grave = Date.now() < seriousUntil;
  const hit = MOODS.find(([re]) => re.test(text));
  if (hit && !grave) face.setEmotion(hit[1], ms);   // no gags while the topic is heavy
  feelTone(text);
}

// Heavy topics (politics, health, death, money trouble) get a serious face until ~90 s after the last mention.
const SERIOUS = /\b(politic\w*|election|president|congress|senate|vote|voting|democrat\w*|republican\w*|trump|biden|government|war|immigration|abortion|gun|guns|health|doctor|hospital|surgery|cancer|diagnos\w+|symptoms?|medication|medicine|illness|disease|pain|therapy|depress\w+|anxiety|funeral|died|passed away|death|dying|divorce|lawsuit|bankrupt\w*)\b/i;
let seriousUntil = 0;

// Conversation tone: a running valence nudged by each utterance, fading back to neutral when the
// talk goes quiet. The face rests on it between the brief per-sentence expressions above.
const GOOD = /\b(love|great|awesome|amazing|thank(s| you)|wonderful|fun|happy|glad|nice|cool|yay|haha|lol|good job|well done|perfect|beautiful)\b/i;
const BAD = /\b(sad|sorry|hate|terrible|awful|angry|annoying|upset|worried|scared|bad|stupid|wrong|broken|ugh|shut up|stop it|lonely|miss|hurts?|tired|bored)\b/i;
let valence = 0;
function feelTone(text) {
  valence = Math.max(-1, Math.min(1, valence * 0.8 + (GOOD.test(text) ? 0.4 : 0) - (BAD.test(text) ? 0.45 : 0)));
  applyTone();
}
function applyTone() {
  face.setTone(Date.now() < seriousUntil ? "serious" : valence > 0.3 ? "happy" : valence < -0.5 ? "sad" : valence < -0.25 ? "nervous" : null);
}
setInterval(() => { valence *= 0.97; if (Math.abs(valence) < 0.05) valence = 0; applyTone(); }, 3000); // ~100 s half-life

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
  if (ctx.currentTime < nextPlay) { touch(); face.setState("speaking"); face.setLevel(rms * 4); }
  else if (face.state === "speaking") face.setState(open ? "listening" : "idle");
  requestAnimationFrame(meter);
}

// ---------- sleep / wake ----------
// Asleep = voice session closed (no billing), face shows Zzz, and only a local speech recogniser
// listens for "Hey Gadget".
const touch = () => (lastActivity = Date.now());

function startIdleTimer() {
  clearInterval(idleTimer);
  idleTimer = setInterval(() => {
    if (!active || sleeping || !open || pendingCalls.size || ctx.currentTime < nextPlay) return;
    if (face.state === "moving" || face.state === "thinking") { touch(); return; } // robot is busy
    if ((sleepAt && Date.now() > sleepAt) || Date.now() - lastActivity > IDLE_SECS * 1000) goToSleep();
  }, 1000);
}

function goToSleep() {
  sleeping = true; // set first: the close handlers below must not reconnect
  sleepAt = 0;
  open = false;
  headSend({ type: "end" }); // fold the conversation into memory now
  provider?.close();
  head?.close();
  micBuffer = [];
  flushPlayback();
  face.setState("sleeping");
  caption('Say "Hey Gadget" to wake');
  listenForWake();
}

function wakeUp() {
  if (!sleeping || !active) return;
  sleeping = false;
  stopWake();
  newSession(); // a fresh conversation; the head socket carries the id, so reconnect it too
  muted = stopFired = false;
  backoff = 500;
  touch();
  face.setState("idle");
  face.setEmotion("surprised", 700);
  caption("");
  connectHead();
  connectProvider(provider);
}

// ---------- music: stop listening to the room and dance instead ----------
// Dancing is sleep with a different face: the voice session is closed (no billing, no replies to
// lyrics) and the wake-word recogniser is off, since it would only transcribe the song.
function startDancing() {
  if (!active || dancing) return;
  if (!sleeping) goToSleep();
  stopWake();
  dancing = true;
  face.setState("dancing");
  caption("");
}

function stopDancing() {
  if (!dancing) return;
  dancing = false;
  if (!active) return;
  face.setState("sleeping");
  caption('Say "Hey Gadget" to wake');
  listenForWake();
}

// Tap the face to wake from sleep. While dancing this doubles as the escape hatch for a false
// positive (no re-trigger for 2 min).
$("face").addEventListener("click", () => {
  if (!sleeping) return;
  if (dancing) {
    detector?.hold(120000);
    stopDancing();
  }
  wakeUp();
});

const MUSIC = new URLSearchParams(location.search).has("music"); // music mode is opt-in: add ?music=1
const dbg = MUSIC ? $("dbg") : null; // ...and it shows the classifier
if (dbg) dbg.style.display = "block";

function listenForWake() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return;
  stopWake();
  rec = new SR();
  rec.continuous = true;
  rec.interimResults = true;
  rec.lang = "en-US";
  rec.onresult = (e) => {
    for (let i = e.resultIndex; i < e.results.length; i++) {
      if (WAKE_WORD.test(e.results[i][0].transcript)) { wakeUp(); return; }
    }
  };
  rec.onerror = (e) => { if (e.error === "not-allowed" || e.error === "service-not-allowed") { caption("Speech recognition is blocked, so I cannot hear the wake word"); rec = null; } };
  rec.onend = () => { if (sleeping && active && rec) setTimeout(() => { try { rec.start(); } catch {} }, 300); }; // it times out on its own
  try { rec.start(); } catch {}
}

function stopWake() {
  const r = rec;
  rec = null; // clear first so onend does not restart it
  try { r?.abort(); } catch {}
}

// ---------- head server link ----------
function connectHead() {
  const ws = (head = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/head?sid=${sessionId()}`));
  ws.onmessage = async (e) => {
    const m = JSON.parse(e.data);
    if (m.type === "face" && !sleeping) face.setState(m.state);
    if (m.type === "claude_status") caption(m.text);
    if (m.type === "claude_result") {
      const call = pendingCalls.get(m.id);
      if (!call) return;
      pendingCalls.delete(m.id);
      if (provider.ackSpeaksFirst) await playbackDone(); // don't talk over the acknowledgement
      if (active) provider.sendToolResult(call.callId, call.name, m.text);
    }
  };
  ws.onclose = () => { if (active && !sleeping && head === ws) setTimeout(connectHead, 1000); };
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
  face.setEmotion("surprised", 1200);
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
      touch();
      micBuffer.forEach((b) => p.sendAudio(b));
      micBuffer = [];
      face.setState("listening");
      face.setEmotion("happy", 2000); // greet
      caption("");
    },
    onClose() {
      if (!live() || sleeping) return;
      open = false;
      face.setState("idle");
      setTimeout(() => connectProvider(p), backoff);
      backoff = Math.min(backoff * 2, 15000);
    },
    onAudio(b64) { if (live()) playChunk(b64); },
    onInterrupted() { muted = false; flushPlayback(); }, // barge-in: the user is speaking, so unmute
    onUserText(text, final) {
      // React to "stop" on partials too; never wait on an LLM to stop the robot.
      touch();
      if (!stopFired && STOP_WORDS.test(text)) { stopFired = true; emergencyStop(); }
      if (final) {
        stopFired = false;
        if (SLEEP_WORDS.test(text)) sleepAt = Date.now() + 3000; // let the goodbye finish first
        caption(text);
        express(text, 2500);
        headSend({ type: "transcript", role: "user", text });
      }
    },
    onModelText: (text) => { express(text, 4000); headSend({ type: "transcript", role: "voice", text }); },
    onToolCall({ callId, name, request, fact }) {
      touch();
      if (name === "remember") {
        headSend({ type: "remember", fact });
        p.sendToolResult(callId, name, "saved");
        return;
      }
      if (name !== "ask_claude") return;
      const id = crypto.randomUUID();
      pendingCalls.set(id, { callId, name });
      headSend({ type: "ask_claude", id, request });
      face.setState("thinking");
    },
    onError: (msg) => { console.warn("voice error:", msg); face.setEmotion("dizzy", 2500); caption(msg); },
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
  const src = ctx.createMediaStreamSource(micStream);
  src.connect(micNode);
  if (MUSIC) detector = new MusicDetector(ctx, src, {
    suppress: () => ctx.currentTime < nextPlay || pendingCalls.size > 0, // our own voice is not music
    onBeat: (s) => dancing && face.beat(s),
    onChange: (music) => (music ? startDancing() : stopDancing()),
    onDebug: dbg && ((d) => (dbg.textContent = `${d.db.toFixed(0)} dB  beat ${d.beat.toFixed(2)}  gaps ${d.gaps.toFixed(2)}  cv ${d.cv.toFixed(2)}\nscore ${d.ema.toFixed(2)}  ${d.music ? "MUSIC" : "-"}\nv5 ${dancing ? "dancing" : sleeping ? "asleep" : open ? "listening" : "connecting"}`)),
  });
  micNode.port.onmessage = (e) => {
    if (open) provider.sendAudio(e.data);
    else if (!sleeping) { micBuffer.push(e.data); if (micBuffer.length > 100) micBuffer.shift(); } // ~10 s cap
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
  newSession(); // one id per tap on a start button; reconnects within it share the same conversation
  startCost(name);
  muted = stopFired = sleeping = dancing = false;
  backoff = 500;
  touch();
  startIdleTimer();
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
  clearInterval(idleTimer);
  stopWake();
  detector?.stop();
  detector = null;
  dancing = false;
  sleeping = false;
  emergencyStop();
  headSend({ type: "end" }); // lets the server fold this conversation into memory right away
  stopCost();
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
addEventListener("pagehide", () => { if (active) { headSend({ type: "end" }); stopCost(); } }); // closing the tab still counts toward today's total
