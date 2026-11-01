// Captures mic audio, resamples to 24 kHz mono PCM16, posts ~100 ms chunks to the main thread.
// Target rate comes from the voice provider (Grok 24 kHz, Gemini 16 kHz) via processorOptions.
class PcmCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const targetRate = options.processorOptions.targetRate;
    this.chunk = targetRate / 10;
    this.ratio = sampleRate / targetRate;  // input samples per output sample
    this.pos = 0;                          // fractional read position into the carried-over input
    this.carry = new Float32Array(0);
    this.out = new Int16Array(this.chunk);
    this.outLen = 0;
  }

  process(inputs) {
    const ch = inputs[0][0];
    if (!ch) return true;
    const buf = new Float32Array(this.carry.length + ch.length);
    buf.set(this.carry);
    buf.set(ch, this.carry.length);

    let pos = this.pos;
    while (pos + 1 < buf.length) {
      const i = Math.floor(pos), f = pos - i;
      const s = Math.max(-1, Math.min(1, buf[i] * (1 - f) + buf[i + 1] * f));
      this.out[this.outLen++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      if (this.outLen === this.chunk) {
        this.port.postMessage(this.out.buffer.slice(0));
        this.outLen = 0;
      }
      pos += this.ratio;
    }
    const keep = Math.floor(pos);
    this.carry = buf.slice(keep);
    this.pos = pos - keep;
    return true;
  }
}

registerProcessor("pcm-capture", PcmCapture);
