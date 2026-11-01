// Shared by all voice providers.
//
// A provider is created with callbacks and exposes:
//   inRate / outRate   PCM16 sample rates for mic upload and audio playback
//   ackSpeaksFirst     true if the model speaks an acknowledgement before the tool result arrives
//                      (so the app should let it finish before sending the result)
//   connect()          resolves once the session is configured; rejects if it cannot start
//   sendAudio(buf)     one chunk of mic PCM16 at inRate
//   sendToolResult(callId, name, text)
//   close()
// Callbacks: onOpen, onClose, onAudio(base64), onInterrupted(), onUserText(text, final),
//            onModelText(text), onToolCall({ callId, name, request }), onError(msg)

export const INSTRUCTIONS = `You are Droid, a friendly little wheeled robot with a camera-less head and a LIDAR. Keep replies short and warm.
You cannot move yourself. For ANY request to move, turn, go somewhere, look around, or describe surroundings, call the
ask_claude function with the person's request in plain words, say one brief acknowledgement while you wait ("on it"),
then speak the result you get back. Never claim to have moved until ask_claude returns. If the person says stop, say "stopping".`;

export const ASK_CLAUDE = {
  name: "ask_claude",
  description: "Hand a physical or spatial request (move, turn, go to, look around, what's near) to Claude, who controls the robot's wheels and LIDAR. Returns what happened.",
  parameters: {
    type: "object",
    properties: { request: { type: "string", description: "The request in plain words" } },
    required: ["request"],
  },
};

export async function fetchToken(provider) {
  const r = await fetch(`/session?provider=${encodeURIComponent(provider)}`, { method: "POST" });
  if (!r.ok) throw new Error(`token request failed (${r.status})`);
  return (await r.json()).token;
}

export const b64 = {
  fromBuffer(arrayBuf) {
    const bytes = new Uint8Array(arrayBuf);
    let s = "";
    for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(s);
  },
};

// WebSocket message data may arrive as a string or a Blob (Gemini sends JSON in binary frames).
export async function parseMessage(data) {
  return JSON.parse(typeof data === "string" ? data : await data.text());
}
