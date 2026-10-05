# Audio File Stream

<!-- block-metadata:start -->
[![Block version: 0.1.0](https://img.shields.io/badge/block-0.1.0-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->

<p align="center"><img src="media/cover.png" alt="An audio file feeding a paced sound stream" width="640"></p>

Turn a **completed local audio file** into a paced 48 kHz Opus stream for
BloxSmith's Audio Play Stream, Save Audio, VAD or other compatible audio inputs.
This bridges file-producing local TTS blocks without requiring framework changes.
It does not synthesize speech, wait for a growing file or play sound directly.

## Wiring

- Local TTS `audio_file` → **file_in** (ordinary data link).
- **audio_out** → Speaker `audio_in` (audio link).
- **command_out** → recorder `command_in`, if recording (ordinary data link).
- Interruption controller → **command_in** and **Speaker command_in**, separately.

Configure **Allowed source directory** before sending a file. Relative paths are
resolved beneath that directory; absolute paths must remain beneath it. Sources
are read-only and symbolic links, devices, FIFOs, parent traversal and URLs are
refused. This restriction is not an operating-system sandbox.

`file_in` accepts a path, or JSON such as:

```json
{"path":"reply.ogg","request_id":"turn-12"}
```

The optional request identifier is preserved in status and audio-frame correlation;
each playback gets a fresh `stream_id`, including repeated playback of the same
file. To preserve a TTS metadata identifier, explicitly map its completed path and
identifier into this envelope. A metadata object with unrelated fields is refused.

`command_in` accepts only `{"action":"interrupt"}`. It interrupts preparation or
publication, drops earlier pending files and leaves the listener ready for later
requests. It does **not** clear audio already queued by the Speaker. If a batch
contains both file and interrupt inputs, interruption takes priority.

## Audio and lifecycle

Supported input: completed Ogg/Opus mono/stereo or PCM16 WAV mono/stereo at
8–48 kHz. WAV is encoded with local FFmpeg/libopus. Existing Opus packets are
remuxed into short pages without re-encoding. Headers, pre-skip and end trimming
are validated. No MP3, AAC, playlists, network media or implicit format guessing.

The block validates and prepares the entire file before sending any audio. This
adds preparation latency; it is not incremental TTS. Pages are paced by their
sample clock, not file size. The bounded serial queue never intentionally floods
downstream audio subscribers to catch up after a stall.

Lifecycle uses a distinct output. `start` carries `stream_id` only (beside `action`),
remaining compatible with VAD's strict producer-command parser;
`stop` adds exact accepted `frame_count`, `byte_count` and `aborted`. Recipients must
reconcile independently delivered commands and audio. A business interrupt sends
an aborted stop if publication started. Framework Stop may revoke those deliveries.
Completed publication does not acknowledge that a browser has finished playback.

Per-file errors and queue rejection appear on `status` without killing the Run or
silently retrying. Missing source, invalid/truncated audio, limits and conversion
failure never publish a successful partial stream. Simulation is deliberately
IO-free; only Active Runtime produces audio. Reordering ports does not change IDs.

## Configuration and requirements

Linux, Python 3.12 and an already installed FFmpeg with libopus. No package or model
is downloaded by Run. Default limits: 32 MiB input, 120 s audio, 30 s preparation,
four pending files and 96 kb/s for WAV conversion. Maximum converted file: 32 MiB.
The block owns its converter process group and fences it on interruption, timeout
and host death. Ordinary completion removes private job directories; abrupt host
termination may leave an unpublished temporary directory in this node's storage.
User source files are never deleted.

Modal and inspector share translated EN/FR fields, directory picker, explicit
Apply/Cancel, reachable actions and advanced limits. Settings apply on the next Run.

## Validation status

Three suites passed against pinned BloxSmith 1.0.9: fourteen core tests with real
FFmpeg codecs, both runtimes across bundled/managed/linked packages, actual Save
Audio recording, browser Speaker decoding/scheduling, interruption/recovery and
responsive English/French modal and inspector surfaces. Converter timeout,
interruption and abrupt parent death are exercised with an explicitly stuck process
double; source admission and sample-preserving Opus remux use real files/codecs.
Screenshots were visually reviewed on desktop and narrow mobile views. Browser
sample scheduling is not certification of a physical speaker or acoustic quality.
The private report is `tests/results/run-5qon6qpu/report.json`; temporary app/audio
files were deleted automatically while bounded diagnostics were retained.
No release or code publication is implied. The Ogg parser is adapted locally from BloxSmith
OpenAI TTS Stream; UI helpers reuse package-owned Apache-2.0 code. No sibling block
implementation is imported at runtime.

## License

Apache-2.0 for this block. FFmpeg and linked codecs retain their own licenses.
