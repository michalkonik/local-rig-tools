#!/usr/bin/env node
// Test client: streams a WAV (any fmt ffmpeg understands) as mulaw/8k to the
// bridge, mimicking the OpenClaw gateway transcription relay, and prints events.
// Usage: node test-client.mjs <audio-file> [language]
import { WebSocket } from "ws";
import { execFileSync } from "node:child_process";

const file = process.argv[2];
const lang = process.argv[3];
if (!file) {
  console.error("usage: node test-client.mjs <audio-file> [language]");
  process.exit(1);
}
const raw = execFileSync("ffmpeg", ["-i", file, "-f", "mulaw", "-ar", "8000", "-ac", "1", "-"], {
  maxBuffer: 50 * 1024 * 1024,
});
// pad 0.5s silence (mulaw silence = 0xff) front and back so VAD endpoints cleanly
const pad = Buffer.alloc(4000, 0xff);
const audio = Buffer.concat([pad, raw, pad]);

const url = new URL("ws://127.0.0.1:8790/listen");
url.searchParams.set("model", "nova-3");
url.searchParams.set("encoding", "mulaw");
url.searchParams.set("sample_rate", "8000");
url.searchParams.set("channels", "1");
url.searchParams.set("interim_results", "true");
url.searchParams.set("endpointing", "800");
if (lang) url.searchParams.set("language", lang);

const ws = new WebSocket(url.toString(), { headers: { Authorization: "***" } });
const t0 = Date.now();
const ev = (o) => console.log(`+${((Date.now() - t0) / 1000).toFixed(1)}s`, JSON.stringify(o.type === "Results" ? { type: o.type, is_final: o.is_final, speech_final: o.speech_final, from_finalize: o.from_finalize, text: o.channel?.alternatives?.[0]?.transcript } : o));
ws.on("open", () => {
  console.log("open, streaming", (audio.length / 8000).toFixed(1), "s of audio in realtime");
  const CHUNK = 320; // 40 ms
  let off = 0;
  const timer = setInterval(() => {
    if (off >= audio.length) {
      clearInterval(timer);
      ws.send(JSON.stringify({ type: "Finalize" }));
      return;
    }
    ws.send(audio.subarray(off, off + CHUNK));
    off += CHUNK;
  }, 40);
});
let gotFinal = false;
ws.on("message", (m) => {
  try {
    const o = JSON.parse(m.toString());
    ev(o);
    if (o.type === "Results" && o.is_final && (o.speech_final || o.from_finalize) && !gotFinal) {
      gotFinal = true;
      setTimeout(() => ws.close(), 200);
    }
  } catch {
    console.log("non-json frame", m.toString().slice(0, 100));
  }
});
setTimeout(() => {
  console.log("TIMEOUT waiting for final result");
  process.exit(gotFinal ? 0 : 2);
}, 45000);
ws.on("close", () => process.exit(0));
ws.on("error", (e) => {
  console.error("ws error", e.message);
  process.exit(1);
});
