#!/usr/bin/env node
// Local Deepgram-compatible realtime transcription bridge for OpenClaw hold-to-talk.
// Speaks the Deepgram WS protocol (/listen), decodes G.711 mulaw 8 kHz audio the
// gateway relay sends, runs an energy VAD, and transcribes each utterance with
// the local Speaches Whisper endpoint. Fully local; no cloud calls.
//
// Env: PORT (default 8790), SPEECHES_URL (default http://127.0.0.1:8001/v1),
//      WHISPER_MODEL (default whisper-1), BRIDGE_VERBOSE=1 for per-frame logs.

import http from "node:http";
import { WebSocketServer } from "ws";

const PORT = Number(process.env.PORT || 8790);
const SPEECHES_URL = process.env.SPEECHES_URL || "http://127.0.0.1:8001/v1";
const WHISPER_MODEL = process.env.WHISPER_MODEL || "whisper-1";
const VERBOSE = process.env.BRIDGE_VERBOSE === "1";

const SAMPLE_RATE = 8000;
const FRAME_MS = 20;
const FRAME_SAMPLES = (SAMPLE_RATE * FRAME_MS) / 1000;
const PREROLL_MS = 400;
const PARTIAL_MIN_MS = 1200;
const PARTIAL_EVERY_MS = 2000;
const UTTERANCE_MAX_MS = 30000;

// --- G.711 mulaw decode (ITU-T G.711) ---
const MULAW_TABLE = (() => {
  const table = new Int16Array(256);
  for (let i = 0; i < 256; i++) {
    const t = ~i;
    const sign = t & 0x80;
    const exp = (t >> 4) & 0x07;
    const mant = t & 0x0f;
    let val = ((mant << 3) + 0x84) << exp;
    val -= 0x84;
    table[i] = sign ? -val : val;
  }
  return table;
})();

function mulawToPcm16(buf) {
  const out = new Int16Array(buf.length);
  for (let i = 0; i < buf.length; i++) out[i] = MULAW_TABLE[buf[i]];
  return out;
}

function pcm16ToWav(samples, sampleRate) {
  const header = Buffer.alloc(44);
  header.write("RIFF", 0);
  header.writeUInt32LE(36 + samples.length * 2, 4);
  header.write("WAVE", 8);
  header.write("fmt ", 12);
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20); // PCM
  header.writeUInt16LE(1, 22); // mono
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(sampleRate * 2, 28);
  header.writeUInt16LE(2, 32);
  header.writeUInt16LE(16, 34);
  header.write("data", 36);
  header.writeUInt32LE(samples.length * 2, 40);
  return Buffer.concat([header, Buffer.from(samples.buffer, samples.byteOffset, samples.byteLength)]);
}

async function transcribe(samples, language) {
  if (samples.length < SAMPLE_RATE * 0.2) return "";
  const wav = pcm16ToWav(samples, SAMPLE_RATE);
  const form = new FormData();
  form.append("file", new Blob([wav], { type: "audio/wav" }), "utterance.wav");
  form.append("model", WHISPER_MODEL);
  form.append("response_format", "json");
  if (language && language !== "en") form.append("language", language);
  const res = await fetch(`${SPEECHES_URL}/audio/transcriptions`, {
    method: "POST",
    body: form,
    headers: { Authorization: "***" },
  });
  if (!res.ok) throw new Error(`Speaches transcribe HTTP ${res.status}: ${(await res.text()).slice(0, 200)}`);
  const data = await res.json();
  return (data.text || "").trim();
}

// --- session state machine ---
class BridgeSession {
  constructor(socket, params, log) {
    this.socket = socket;
    this.log = log;
    this.endpointingMs = Math.min(3000, Math.max(300, Number(params.get("endpointing") || 800)));
    this.language = params.get("language") || "";
    this.interim = params.get("interim_results") !== "false";
    this.pcm = []; // pending utterance Int16Array chunks
    this.pcmLen = 0;
    this.preroll = [];
    this.prerollLen = 0;
    this.frame = new Int16Array(0);
    this.inSpeech = false;
    this.speechMs = 0;
    this.silenceFrames = 0;
    this.lastPartialAt = 0;
    this.transcribing = false;
    this.queueTail = Promise.resolve();
    this.pendingOps = 0;
    this.finalPending = false;
    this.partialText = "";
    this.partialMark = 0;
    this.pendingFinalizeAck = false;
  }

  send(obj) {
    if (this.socket.readyState === 1) this.socket.send(JSON.stringify(obj));
  }

  results(transcript, { isFinal, speechFinal = false, fromFinalize = false }) {
    this.send({
      type: "Results",
      is_final: isFinal,
      speech_final: speechFinal,
      from_finalize: fromFinalize,
      channel: { number: 0, alternatives: [{ transcript, confidence: 0.9 }] },
      start_ms: 0,
      duration_ms: 0,
      speech_start: false,
      speech_end: speechFinal,
      anchors: [],
      keywords: [],
      geotag: [],
      timeouts: [],
      vscode: [],
      searches: [],
      entity: [],
      intents: [],
      sentents: [],
    });
  }

  error(message) {
    this.send({ type: "Error", message });
  }

  feed(mulawBuf) {
    const samples = mulawToPcm16(mulawBuf);
    let offset = 0;
    // frame alignment
    if (this.frame.length > 0) {
      const need = FRAME_SAMPLES - this.frame.length;
      const take = Math.min(need, samples.length);
      const merged = new Int16Array(this.frame.length + take);
      merged.set(this.frame, 0);
      merged.set(samples.subarray(0, take), this.frame.length);
      this.frame = merged.length === FRAME_SAMPLES ? new Int16Array(0) : merged;
      if (merged.length === FRAME_SAMPLES) this.processFrame(merged);
      offset = take;
    }
    for (let i = offset; i + FRAME_SAMPLES <= samples.length; i += FRAME_SAMPLES) {
      this.processFrame(samples.subarray(i, i + FRAME_SAMPLES));
    }
    if (offset < samples.length) {
      const rest = samples.subarray(Math.max(offset, Math.floor((samples.length - offset) / FRAME_SAMPLES) * FRAME_SAMPLES + offset));
      if (rest.length > 0) this.frame = Int16Array.from(rest);
    }
  }

  rms(frame) {
    let sum = 0;
    for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
    return Math.sqrt(sum / frame.length);
  }

  processFrame(frame) {
    const energy = this.rms(frame);
    const threshold = 150; // tuned for mulaw telephony speech levels
    const voiced = energy > threshold;
    if (!this.inSpeech) {
      this.preroll.push(frame);
      this.prerollLen += frame.length;
      const maxPre = (PREROLL_MS / 1000) * SAMPLE_RATE;
      while (this.prerollLen > maxPre) this.prerollLen -= this.preroll.shift().length;
      if (voiced) {
        this.beginSpeech();
      }
      return;
    }
    this.append(frame);
    this.speechMs += FRAME_MS;
    if (voiced) this.silenceFrames = 0;
    else this.silenceFrames++;
    const silenceMs = this.silenceFrames * FRAME_MS;
    if (silenceMs >= this.endpointingMs) {
      this.endSpeech("endpoint");
    } else if (this.speechMs >= UTTERANCE_MAX_MS) {
      this.endSpeech("max-length");
    } else if (
      this.interim &&
      !this.transcribing &&
      this.speechMs - this.lastPartialAt >= PARTIAL_EVERY_MS &&
      this.speechMs >= PARTIAL_MIN_MS &&
      silenceMs < 300
    ) {
      this.emitPartial();
    }
  }

  beginSpeech() {
    this.inSpeech = true;
    this.speechMs = 0;
    this.silenceFrames = 0;
    this.lastPartialAt = 0;
    this.partialText = "";
    this.partialMark = 0;
    for (const f of this.preroll) this.append(f);
    this.preroll = [];
    this.prerollLen = 0;
    this.send({ type: "SpeechStarted" });
  }

  append(frame) {
    this.pcm.push(frame);
    this.pcmLen += frame.length;
  }

  takePcm() {
    const out = new Int16Array(this.pcmLen);
    let off = 0;
    for (const c of this.pcm) {
      out.set(c, off);
      off += c.length;
    }
    this.pcm = [];
    this.pcmLen = 0;
    this.inSpeech = false;
    this.speechMs = 0;
    this.silenceFrames = 0;
    return out;
  }

  queueWork(job) {
    this.pendingOps++;
    this.queueTail = this.queueTail
      .then(() => job())
      .catch((err) => this.log(`queued job failed: ${err.message}`))
      .finally(() => {
        this.pendingOps--;
      });
  }

  // Transcribe finished audio, reusing the last partial checkpoint when the
  // remaining tail is short (keeps CPU work down to beat the client timeout).
  async transcribeFinished(samples) {
    const mark = this.partialMark;
    const text = this.partialText;
    this.partialText = "";
    this.partialMark = 0;
    if (text && mark > 0 && samples.length - mark <= 2 * SAMPLE_RATE) {
      const tail = await transcribe(samples.subarray(mark), this.language);
      return tail ? `${text} ${tail}`.replace(/\s+/g, " ").trim() : text;
    }
    return transcribe(samples, this.language);
  }

  emitPartial() {
    this.lastPartialAt = this.speechMs;
    const samples = this.copyPcm();
    const mark = this.pcmLen;
    this.queueWork(async () => {
      if (this.finalPending) return;
      this.transcribing = true;
      try {
        const text = await transcribe(samples, this.language);
        if (text && this.inSpeech && !this.finalPending) {
          this.partialText = text;
          this.partialMark = mark;
          this.results(text, { isFinal: false });
        }
      } catch (err) {
        this.log(`partial transcribe failed: ${err.message}`);
      } finally {
        this.transcribing = false;
      }
    });
  }

  copyPcm() {
    const out = new Int16Array(this.pcmLen);
    let off = 0;
    for (const c of this.pcm) {
      out.set(c, off);
      off += c.length;
    }
    return out;
  }

  endSpeech(reason) {
    const samples = this.takePcm();
    this.log(`utterance end (${reason}) ${(samples.length / SAMPLE_RATE).toFixed(1)}s`);
    this.finalPending = true;
    this.queueWork(async () => {
      this.finalPending = false;
      try {
        const text = await this.transcribeFinished(samples);
        this.results(text, { isFinal: true, speechFinal: true });
      } catch (err) {
        this.log(`final transcribe failed: ${err.message}`);
        this.error(err.message);
      }
    });
  }

  finalize() {
    if (!this.inSpeech || this.pcmLen === 0) {
      if (this.pendingOps === 0) {
        this.results("", { isFinal: true, speechFinal: true, fromFinalize: true });
      }
      return;
    }
    const samples = this.takePcm();
    this.log(`finalize ${(samples.length / SAMPLE_RATE).toFixed(1)}s`);
    this.finalPending = true;
    this.queueWork(async () => {
      this.finalPending = false;
      try {
        const text = await this.transcribeFinished(samples);
        this.results(text, { isFinal: true, speechFinal: true, fromFinalize: true });
      } catch (err) {
        this.log(`finalize transcribe failed: ${err.message}`);
        this.error(err.message);
      }
    });
  }
}

const server = http.createServer((req, res) => {
  if (req.url === "/health") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ ok: true, service: "openclaw-voice-bridge", model: WHISPER_MODEL }));
    return;
  }
  res.writeHead(404);
  res.end();
});

const wss = new WebSocketServer({ server, path: "/listen" });

wss.on("connection", (socket, req) => {
  const url = new URL(req.url, "http://localhost");
  const params = url.searchParams;
  const remote = req.socket.remoteAddress;
  let n = 0;
  const log = (msg) => console.log(`[dg ${remote}#${++n === 1 ? "" : ""}] ${msg}`);
  const session = new BridgeSession(socket, params, log);
  log(`open model=${params.get("model")} encoding=${params.get("encoding")} sr=${params.get("sample_rate")} endpointing=${params.get("endpointing")}`);
  const badEnc = (params.get("encoding") || "").toLowerCase();
  if (badEnc !== "mulaw") session.error(`voice-bridge supports mulaw only, got ${badEnc}`);
  socket.on("message", (data, isBinary) => {
    if (isBinary) {
      session.feed(data);
      return;
    }
    try {
      const msg = JSON.parse(data.toString());
      if (msg.type === "Finalize") session.finalize();
      else if (msg.type === "CloseStream") socket.close();
    } catch (err) {
      log(`bad text frame: ${err.message}`);
    }
  });
  socket.on("close", () => log("closed"));
  socket.on("error", (err) => log(`socket error: ${err.message}`));
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`voice-bridge listening on ws://127.0.0.1:${PORT}/listen (Speaches ${SPEECHES_URL}, model ${WHISPER_MODEL})`);
});
