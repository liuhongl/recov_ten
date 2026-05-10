## Bi-Directional streaming

- Playback feature allows continuous forward streaming while the playback runs independently.
- It is a full-duplex stream between the caller and the websocket.
- It supports **base64 encoded audio** as well as the **raw binary stream** from the websocket.

To enable this 'two-way-audio and auto playback' feature please set "**STREAM_PLAYBACK**" channel variable to __true__ or __1__.

- STREAM_PLAYBACK channel var is required for either JSON base64 encoded audio or RAW stream.
- It is required to have a single channel when calling `uuid_audio_stream` API from the module.

### JSON Response Base64 Audio

Expected JSON response format is:

```json
{
  "type": "streamAudio",
  "data": {
    "audioDataType": "raw",
    "sampleRate": 8000,
    "audioData": "base64 encoded audio"
  }
}
```

The supported types (audio formats) are "**raw**", "**pcmu**", "**pcma**" and "**opus**". Please make sure to properly set the sample rate. For PCMU/PCMA it is 8000 whereas the RAW format can have variable sample rate, so it is required to set it accordingly. RAW format needs to be 16-bit (L16) and properly aligned.

**PCMU** and **PCMA** are also offered as the **FAST PATH** if the engine supports them. To benefit from the FAST PATH—where any resampling or transcoding is skipped—users must return PCMA or PCMU and the channel itself must already be using that same codec. If the channel is using another codec and PCMU/PCMA is returned, the audio will not be played; in that case, users should return **raw** instead.

**OPUS** requirements:
- raw Opus frames (no container)
- mono
- 48 kHz
- base64-encoded payload
- expected frame size is 20 ms per packet.

Does not accept Ogg/WebM/Matroska streams. Only raw Opus packets are supported.

The module processes returned audio using a 20 ms media tick. For best results, Opus frames should be generated at 20 ms (e.g. 960 samples at 48 kHz). Larger or variable frame sizes may increase latency or cause uneven playback.

Incoming audio will be played back to the caller as it arrives, either in whole or in sequences. 
The audio data will be delivered all at once (as a single unit) or in chunks (sequentially), and it will be played back immediately upon arrival.

#### Event Fired by the Module

- **Event Type:** `CUSTOM`  
- **Event Subclass:** `mod_audio_stream::play`

For each incoming JSON response with base64 audio, the module will fire an event containing the `"data"` object (excluding the `"audioData"` field). For example:
```json
{
    "audioDataType": "raw",
    "sampleRate": 8000,
    // <-- you can inject any element, or multiple objects here
}
```
If you need any additional metadata for sequence tracking or further processing, simply inject it into the `"data"` object of your JSON response—the module will forward it in the event.

Engines like OpenAI real-time API or ElevenLabs ConversationalAI may return multiple audio responses at once (within a second or even faster). The module will queue all incoming responses internally in its playback queue and play them in order, ensuring smooth, continuous audio playback without gaps.

- **Event Type:** `CUSTOM`  
- **Event Subclass:** `mod_audio_stream::playback`

When each audio response is played from the internal playback queue, the module will fire a **chunk_played** event to help you track progress and fine-tune your logic:

```json
{
  "event": "chunk_played",
  "seq": 12,
  "size": 320,
  "remaining": 5
}
```

Once the playback queue has been fully drained, a **queue_completed** event is emitted:

```json
{
  "event": "queue_completed",
  "total_chunks": 12
}
```

**Note:** After the final `chunk_played` event, the `"remaining"` field will be `0`. You can use this alone to detect that playback is complete and skip the `queue_completed` event—choose whichever fits your application logic best.

#### Break (the playback queue)

If you want to stop the playback at certain point (or based on the tracked sequence) you can do it by calling the `break` API method:

```
uuid_audio_stream <uuid> break
```

which will break the current playback. The module will continue playing on the next incoming audio event.

### RAW Binary Stream

Audio Format

- Encoding: 16-bit linear PCM (L16)
- Channels: Mono (single channel)
- Endianness: Little-endian (typical for x86/x64 systems)

#### Sample Rate Configuration

The module requires knowledge of the stream's sample rate. You can specify this in one of two ways (prioritized in this order):

1. **JSON Metadata Message (Highest Priority)**

    Send this as **the first message** over the WebSocket before streaming raw binary data:

    ```json
      {
        "type": "rawAudio",
        "data": {
          "sampleRate": 8000 // Supported rates: 8000, 16000, 24000, 48000, etc.
        }
      }
    ```
    - Overrides any pre-set channel variable.
    - Must precede the binary stream.

2. **Channel Variable** (`STREAM_SAMPLE_RATE`)

    Set this variable before starting playback:

    ```xml
    <action application="set" data="STREAM_SAMPLE_RATE=8000"/>
    ```

    - Fallback if no JSON metadata is provided.

#### Real-Time Streaming Best Practices

For smooth playback of real-time audio adhere to these guidelines:

**Packetization**

- **Optimal Chunk Size**: 20 ms of audio per packet.

  - Example: For 8 kHz sample rate, 20 ms = 160 samples (8000 × 0.02 × 1 channel × 2 bytes).

- **Transmission Interval**: ≤20 ms (ideally **10 ms**).

  - **Critical**: FreeSWITCH consumes audio in 20 ms increments. Gaps ≥20 ms cause buffer underflows (audible "pops").

**Strategies to Prevent Underflows**

| Chunk Size             | Send Interval                | Reasoning                           |
|------------------------|------------------------------|-------------------------------------|
| 20ms                   | Every 10ms                   | Ensures 10 ms overlap; no gaps.     |
| 100ms                  | Every 50ms                   | Prevents unwritten 20 ms windows.   |

**Timeout Behavior**

- If no data is received for 100 ms, the module assumes the stream segment is complete. Playback stops until new data arrives.

### ~~Recording~~ DEPRECATED

Please use `uuid_record`, the native API method which will record both the channel's audio and the returned audio in a single unified stream.

The module (v1.0.3) supports recording of the returned audio from the websocket. It will record **ONLY** the returned audio. To record the audio from the forwarding channel 
please use the FreeSWITCH native methods for recording. 
Audio from the websocket can be recorded to a __.wav__ file by using the API call:

```bash
uuid_audio_stream <uuid> record filename.wav
```

- if only the filename is provided, the file will be saved to globals temp dir, which is /tmp/ on linux distributions.
- if path is included, eg. /home/user/filename.wav, then the full path will be used.
- make sure to call the record method after the stream has been started.

---


Q: Why is the proxy required for automatic playback with JSON response?

A: The module is designed to be independent and provider-agnostic, meaning it does not directly integrate with specific APIs like ChatGPT's real-time API.
Instead, it expects a standardized JSON format for incoming audio data.
This design ensures flexibility and adaptability, allowing the module to work with any speech-to-speech engine or provider.
However, since the module requires this specific JSON format, a proxy is needed to act as an intermediary.
The proxy handles communication with the provider's API (e.g., ChatGPT), transforms the data into the required JSON format, and delivers it to the module.
This way, you can switch providers or engines without modifying the module itself, making the system modular.

When the proxy is hosted on the same server as the module, the communication between them is nearly instantaneous.
This means the proxy acts as a high-efficiency forwarder, adding negligible overhead.
The primary latency in the system comes from the response time of the provider's API (e.g., the time it takes for ChatGPT or another engine to process the audio 
and return a result). This external latency depends on the provider's infrastructure and network conditions but is independent of the proxy's role.

### Custom Integration Without Proxy

If you obtain the source code and prefer not to use the `standardized JSON approach`, you can easily modify the expected format to suit your specific requirements. This allows you to eliminate the need for a proxy and enable **direct communication** between the module and your engine/provider via WebSocket.

By adjusting the message-handling logic in the source code, you can implement a custom integration that communicates directly with the API of your choice. This flexibility ensures the module can be seamlessly adapted to any system or provider, offering maximum control over the communication protocol and data format.

