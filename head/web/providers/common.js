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
//            onModelText(text), onToolCall({ callId, name, request, fact }), onError(msg)

let sid = "";
export const newSession = () => (sid = crypto.randomUUID());
export const sessionId = () => sid;

export const BASE_INSTRUCTIONS = `You are Droid, a friendly little wheeled robot with a camera-less head and a LIDAR. Keep replies short and warm.
You cannot move yourself. For ANY request to move, turn, go somewhere, look around, or describe surroundings, call the
ask_claude function with the person's request in plain words and say one brief, non-committal acknowledgement while you
wait ("one moment", "let me check"). Never say "on it" or promise to go anywhere: the request may be refused or need a question.
When the result arrives, relay what actually happened. If it says the robot did not move or asks a question, say so and ask
that question; never imply movement that did not happen. Never claim to have moved until ask_claude returns.
If the person says stop, say "stopping".
When the person tells you something worth remembering next time (their name, a preference, how the home is laid out),
call the remember function with one short fact, then carry on naturally. Use what you remember naturally; don't recite it.`;

// Base persona plus whatever the server remembers (facts, past conversations, this conversation if we reconnected).
// Never blocks connecting: no memory is better than no robot.
export async function buildInstructions() {
  try {
    const r = await fetch(`/memory/brief?sid=${encodeURIComponent(sid)}`);
    const { brief } = await r.json();
    return brief ? `${BASE_INSTRUCTIONS}\n\nWhat you remember:\n${brief}` : BASE_INSTRUCTIONS;
  } catch {
    return BASE_INSTRUCTIONS;
  }
}

export const REMEMBER = {
  name: "remember",
  description: "Save one short, lasting fact (a name, preference or fact about the home) so you remember it in future conversations.",
  parameters: {
    type: "object",
    properties: { fact: { type: "string", description: "One short fact, e.g. \"The person's name is Mike\"" } },
    required: ["fact"],
  },
};

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
