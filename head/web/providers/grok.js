import { ASK_CLAUDE, INSTRUCTIONS, b64, fetchToken, parseMessage } from "./common.js";

const URL = "wss://api.x.ai/v1/realtime?model=grok-voice-latest";
const RATE = 24000;

export const VOICES = ["eve", "ara", "rex", "sal", "leo"];

export function createGrok(cb, { voice = "eve" } = {}) {
  let ws;
  const send = (o) => ws?.readyState === 1 && ws.send(JSON.stringify(o));

  function onEvent(ev) {
    switch (ev.type) {
      case "response.output_audio.delta":
      case "response.audio.delta":
        cb.onAudio(ev.delta);
        break;
      case "input_audio_buffer.speech_started":
        cb.onInterrupted(); // barge-in
        break;
      case "conversation.item.input_audio_transcription.completed":
        if (ev.transcript?.trim()) cb.onUserText(ev.transcript, true);
        break;
      case "response.output_audio_transcript.done":
        if (ev.transcript?.trim()) cb.onModelText(ev.transcript);
        break;
      case "response.function_call_arguments.done":
        cb.onToolCall({ callId: ev.call_id, name: ev.name, request: JSON.parse(ev.arguments).request });
        break;
      case "error": {
        const msg = ev.error?.message || "grok error";
        // STOP after generation finished: nothing to cancel, buffered audio is already flushed locally.
        if (/no active response/i.test(msg)) break;
        cb.onError(msg);
        break;
      }
    }
  }

  return {
    name: "grok",
    inRate: RATE,
    outRate: RATE,
    ackSpeaksFirst: true,

    async connect() {
      const token = await fetchToken("grok");
      ws = new WebSocket(URL, [`xai-client-secret.${token}`]);
      ws.onmessage = async (e) => onEvent(await parseMessage(e.data));
      ws.onclose = () => cb.onClose();
      await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("grok socket failed")); });
      send({
        type: "session.update",
        session: {
          instructions: INSTRUCTIONS,
          voice,
          turn_detection: { type: "server_vad" },
          audio: {
            input: { format: { type: "audio/pcm", rate: RATE }, transcription: { model: "grok-transcribe" } },
            output: { format: { type: "audio/pcm", rate: RATE } },
          },
          tools: [{ type: "function", ...ASK_CLAUDE }],
        },
      });
      cb.onOpen();
    },

    sendAudio: (buf) => send({ type: "input_audio_buffer.append", audio: b64.fromBuffer(buf) }),

    sendToolResult(callId, _name, text) {
      send({ type: "conversation.item.create", item: { type: "function_call_output", call_id: callId, output: text } });
      send({ type: "response.create" });
    },

    // Stop generation server-side; flushing local playback alone lets in-flight deltas keep arriving.
    interrupt: () => send({ type: "response.cancel" }),

    close: () => ws?.close(),
  };
}
