import { ASK_CLAUDE, REMEMBER, b64, buildInstructions, fetchToken, parseMessage } from "./common.js";

// Field names below follow the Live API's WebSocket protocol; the setup fields (voice, VAD,
// transcription, compression, resumption) are not covered by the docs pages I read, so watch the
// console for a close reason if the session fails to start.
const HOST = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContentConstrained";
const DEFAULT_MODEL = "gemini-3.8-live";
export const VOICES = ["Puck", "Charon", "Kore", "Fenrir", "Aoede"];

// Gemini's Schema type names are upper case.
const upper = (s) => Array.isArray(s) ? s.map(upper)
  : s && typeof s === "object" ? Object.fromEntries(Object.entries(s).map(([k, v]) => [k, k === "type" ? String(v).toUpperCase() : upper(v)]))
  : s;

export function createGemini(cb, { model = DEFAULT_MODEL, voice = VOICES[0] } = {}) {
  let ws;
  let resumeHandle = null;   // survives reconnects so ~10 min connection limits don't end the conversation
  let userBuf = "", modelBuf = "";
  const send = (o) => ws?.readyState === 1 && ws.send(JSON.stringify(o));

  function flushUser() {
    if (userBuf.trim()) cb.onUserText(userBuf.trim(), true);
    userBuf = "";
  }
  function flushModel() {
    if (modelBuf.trim()) cb.onModelText(modelBuf.trim());
    modelBuf = "";
  }

  function onMessage(m) {
    if (m.setupComplete) cb.onOpen();
    if (m.sessionResumptionUpdate?.resumable && m.sessionResumptionUpdate.newHandle) {
      resumeHandle = m.sessionResumptionUpdate.newHandle;
    }
    if (m.goAway) ws?.close(); // reconnect with the stored handle before the server drops us
    if (m.toolCall) {
      for (const f of m.toolCall.functionCalls || []) {
        cb.onToolCall({ callId: f.id, name: f.name, ...f.args });
      }
    }
    const sc = m.serverContent;
    if (!sc) return;
    if (sc.interrupted) { flushModel(); cb.onInterrupted(); }
    if (sc.inputTranscription?.text) {
      userBuf += sc.inputTranscription.text;
      cb.onUserText(userBuf.trim(), false); // partial; lets the app react to "stop" early
    }
    for (const p of sc.modelTurn?.parts || []) {
      if (p.inlineData?.data) { flushUser(); cb.onAudio(p.inlineData.data); }
    }
    if (sc.outputTranscription?.text) { flushUser(); modelBuf += sc.outputTranscription.text; }
    if (sc.turnComplete) { flushUser(); flushModel(); }
  }

  return {
    name: "gemini",
    inRate: 16000,
    outRate: 24000,
    ackSpeaksFirst: false, // 3.1 Flash Live only supports synchronous tool calls: the model waits for our result

    async connect() {
      const [token, instructions] = await Promise.all([fetchToken("gemini"), buildInstructions()]);
      ws = new WebSocket(`${HOST}?access_token=${encodeURIComponent(token)}`);
      ws.onmessage = async (e) => onMessage(await parseMessage(e.data));
      ws.onclose = (e) => { if (e.reason) console.warn("gemini closed:", e.code, e.reason); cb.onClose(); };
      await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("gemini socket failed")); });
      send({
        setup: {
          model: `models/${model}`,
          generationConfig: {
            responseModalities: ["AUDIO"],
            speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: voice } } },
          },
          systemInstruction: { parts: [{ text: instructions }] },
          tools: [{ functionDeclarations: [ASK_CLAUDE, REMEMBER].map((t) => ({ ...t, parameters: upper(t.parameters) })) }],
          inputAudioTranscription: {},
          outputAudioTranscription: {},
          contextWindowCompression: { slidingWindow: {} },
          sessionResumption: resumeHandle ? { handle: resumeHandle } : {},
        },
      });
      // cb.onOpen() fires on setupComplete (below): audio must not be sent before the server accepts setup.
    },

    sendAudio: (buf) => send({ realtimeInput: { audio: { data: b64.fromBuffer(buf), mimeType: "audio/pcm;rate=16000" } } }),

    sendToolResult(callId, name, text) {
      send({ toolResponse: { functionResponses: [{ id: callId, name, response: { result: text } }] } });
    },

    close: () => ws?.close(),
  };
}
