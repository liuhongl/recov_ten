## ⚠️ Important: Real-Time Response Handling

This module is built for **live, progressive audio streaming**. To ensure smooth and low-latency audio output, it's critical that clients **stream data as it becomes available — not in buffered batches**.

When sending back audio responses (e.g., from LLMs or speech-to-speech engines), you **must forward them immediately** as they are received — whether you're using:

- 🔊 **Raw binary streams** (20ms audio bursts)
- 📦 **Base64-encoded audio chunks in JSON**

**Do not** buffer, accumulate, or wait to combine multiple chunks before sending them. The engine is optimized for **low-latency, real-time streaming**.

Delaying or batching responses will cause:

- 🕒 Noticeable audio lag or delay  
- 🔉 Choppy or interrupted playback  
- 😕 A degraded user experience

To maintain **fluid, natural conversation quality**, treat incoming audio as **live** — process and send each chunk **without delay**.

This module assumes that clients are streaming audio in real time. Any attempt to buffer, batch, or use FreeSWITCH `playback` will **break the real-time flow** and degrade streaming performance.

## ⚠️ Avoid Blocking Media Operations (e.g., TTS or Playback)

**Do not use FreeSWITCH TTS (`speak`) or `playback` commands on the same channel while this module is active.**

These operations are **blocking by nature** — they take control of the media thread and will **pause all media bugs**, including this module's streaming logic. As a result:

- 🔁 No audio will be forwarded to the WebSocket endpoint until playback or TTS finishes
- 🛑 Real-time streaming will be interrupted
- ⏳ Clients may experience silence, delay, or dropped audio

This module relies on **uninterrupted, non-blocking media flow** to function correctly.

> Treat all outgoing audio as part of the real-time stream. Any blocking operations on the channel will break this assumption and severely degrade the experience.