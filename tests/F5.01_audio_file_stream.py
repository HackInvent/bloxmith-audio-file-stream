"""FB1/FB2/FB3/FB4: bounded files, real Opus conversion, pacing, interruption and simulation."""
from collections import deque
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import signal
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from blocs.audio_file_stream.configuration import AudioFileError, Cancelled, configuration, interrupt, request
from blocs.audio_file_stream import files, runtime
from blocs.audio_file_stream.block import AudioFileStreamBlock
from bloxsmith_app.block_api import BlockRuntimeContext, BlockRuntimePreparationContext


def wav(path, duration=.35, channels=1, rate=24000):
    import wave
    with wave.open(str(path), "wb") as target:
        target.setnchannels(channels); target.setsampwidth(2); target.setframerate(rate)
        target.writeframes(b"".join(struct.pack("<h", int(4000 * math.sin(2 * math.pi * 440 * i / rate))) * channels
                                  for i in range(int(rate * duration))))
    return path


class Context:
    def __init__(self, root, **settings):
        self.root_dir = root
        self.config = {**configuration({}), "allowed_root": str(root), **settings}
        self.stopped = threading.Event()
        self.mail = deque()
        self.results = []
        self.frames = []
        self.on_frame = None
        self.audio = NS(available=True, port_routes=[NS(direction="output", port_name="audio_out")], publish_port=self.publish)
        self.services = {"get_block_storage_dir": lambda: root / "storage", "runtime_audio_streams": self.audio}

    def stop_requested(self):
        return self.stopped.is_set()

    def receive_command(self, timeout_sec):
        return NS(payload=self.mail.popleft()) if self.mail else None

    def emit_result(self, result):
        self.results.append(result)

    def publish(self, port, page, **metadata):
        self.frames.append((time.monotonic(), page, metadata))
        if self.on_frame:
            self.on_frame()

    def output(self, port):
        return [json.loads(output.value) for result in self.results for output in result.outputs if output.port_id == port]


class AudioFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = configuration({"allowed_root": str(self.root)})
        self.source = wav(self.root / "voice.wav")

    def tearDown(self):
        self.temporary.cleanup()

    def prepare(self, source=None, **settings):
        folder = self.root / ("job-" + str(len(list(self.root.glob("job-*")))))
        folder.mkdir()
        return files.prepare(str(source or self.source), {**self.config, **settings}, self.root, folder, lambda: False)

    def test_configuration_and_requests(self):
        for setting in ({"queue_size": True}, {"max_audio_sec": float("nan")}, {"max_input_mib": 1000}, {"bitrate_kbps": 12.1}, {"unknown": "x"}):
            with self.assertRaises(AudioFileError): configuration(setting)
        for value in ("", "https://invalid.example/audio.wav", {"path": "x", "request_id": "../turn"}, '{"path":"a","path":"b"}'):
            with self.assertRaises(AudioFileError): request(value)
        self.assertEqual(request('{"path":"a.wav","request_id":"turn-1"}'), {"path":"a.wav", "request_id":"turn-1"})
        for value in ({"action": "stop"}, '{"action":"interrupt","action":"interrupt"}'):
            with self.assertRaises(AudioFileError): interrupt(value)

    def test_no_follow_and_outside_path(self):
        link = self.root / "link.wav"; link.symlink_to(self.source)
        nested = self.root / "linked"; nested.symlink_to(self.root, target_is_directory=True)
        fifo = self.root / "pipe.wav"; os.mkfifo(fifo)
        for path in (link, nested / "voice.wav", fifo, self.root, self.root / ".." / self.root.name / "voice.wav", self.root.parent / "outside.wav"):
            with self.subTest(path=path.name), self.assertRaises(AudioFileError):
                with files.open_source(str(path), str(self.root), self.root): pass
        with self.assertRaises(AudioFileError):
            with files.open_source("voice.wav", "", self.root): pass

    def test_size_limit_and_cancellation(self):
        self.source.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaises(AudioFileError): self.prepare(max_input_mib=1)
        folder = self.root / "cancel"; folder.mkdir()
        with self.assertRaises(Cancelled): files.prepare(str(self.source), self.config, self.root, folder, lambda: True)
        self.assertFalse(list(folder.iterdir()))

    def test_real_mono_and_stereo_conversion(self):
        for channels in (1, 2):
            path, duration, actual = self.prepare(wav(self.source, channels=channels))
            self.assertEqual(actual, channels)
            self.assertAlmostEqual(duration, .35, delta=.002)
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels", "-of", "json", str(path)], capture_output=True, check=True)
            stream = json.loads(probe.stdout)["streams"][0]
            self.assertEqual((stream["codec_name"], stream["sample_rate"], stream["channels"]), ("opus", "48000", channels))

    def test_ogg_remux_preserves_decoded_samples(self):
        original, duration, _ = self.prepare()
        remuxed, final_duration, _ = self.prepare(original)
        def decode(path):
            return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-"], capture_output=True, check=True).stdout
        self.assertEqual(decode(original), decode(remuxed))
        self.assertEqual(duration, final_duration)

    def test_truncated_and_unsupported_files(self):
        self.source.write_bytes(self.source.read_bytes()[:-10])
        with self.assertRaises(AudioFileError): self.prepare()
        self.source.write_bytes(b"#EXTM3U\nhttps://invalid.example/audio")
        with self.assertRaises(AudioFileError): self.prepare()
        output, _, _ = self.prepare(wav(self.root / "other.wav"))
        output.write_bytes(output.read_bytes()[:-1])
        with self.assertRaises(ValueError): self.prepare(output)

    def test_duration_rejects_instead_of_truncating(self):
        with self.assertRaises(AudioFileError): self.prepare(wav(self.source, duration=1.2), max_audio_sec=1)

    def test_lifecycle_pacing_cleanup_and_fresh_stream_ids(self):
        context = Context(self.root)
        listener = runtime.FileListener(context)
        original = self.source.read_bytes()
        for _ in range(2): listener.play({"path": str(self.source), "request_id": "same"}, listener.epoch)
        commands = context.output(2)
        self.assertEqual([item["action"] for item in commands], ["start", "stop", "start", "stop"])
        # Existing VAD accepts only the exact common producer fields, unlike
        # consumers that ignore unknown metadata. No dependency in production.
        from blocs.voice_activity_detection.block import _command as vad_command
        for command in commands:
            self.assertEqual(vad_command(command), command)
        self.assertNotEqual(commands[0]["stream_id"], commands[2]["stream_id"])
        for stop in commands[1::2]:
            sent = [row for row in context.frames if row[2]["stream_id"] == stop["stream_id"]]
            self.assertFalse(stop["aborted"])
            self.assertEqual(stop["frame_count"], len(sent))
            self.assertEqual(stop["byte_count"], sum(len(row[1]) for row in sent))
            self.assertGreater(sent[-1][0] - sent[0][0], .20)
            self.assertTrue(all(row[2]["correlation_id"] == "same" for row in sent))
        self.assertFalse(list((self.root / "storage").iterdir()))
        self.assertEqual(self.source.read_bytes(), original)

    def test_interrupt_purges_then_next_file_works(self):
        context = Context(self.root)
        listener = runtime.FileListener(context)
        def after_frame():
            if len(context.frames) == 3:
                context.mail.extend([{"action": "play", "path": str(self.source), "request_id": "old"},
                                     {"action": "interrupt"},
                                     {"action": "play", "path": str(self.source), "request_id": "next"}])
        context.on_frame = after_frame
        with self.assertRaises(Cancelled): listener.play({"path": str(self.source), "request_id": "current"}, listener.epoch)
        stop = context.output(2)[-1]
        self.assertEqual(stop["frame_count"], 3); self.assertTrue(stop["aborted"])
        self.assertEqual([item["request_id"] for item in listener.pending], ["next"])
        context.on_frame = None
        listener.play(listener.pending.popleft(), listener.epoch)
        self.assertEqual(context.output(3)[-1]["state"], "completed")
        self.assertFalse(list((self.root / "storage").iterdir()))

    def test_bounded_queue_rejection(self):
        context = Context(self.root, queue_size=1)
        context.mail.extend({"action": "play", "path": str(self.source), "request_id": str(i)} for i in range(3))
        listener = runtime.FileListener(context); listener.pump()
        self.assertEqual(len(listener.pending), 1)
        self.assertEqual([row["request_id"] for row in context.output(3) if row["state"] == "rejected"], ["1", "2"])

    def test_recoverable_error_and_listener_stop(self):
        context = Context(self.root)
        context.mail.append({"action": "play", "path": "missing.wav", "request_id": "bad"})
        context.mail.append({"action": "play", "path": str(self.source), "request_id": "good"})
        listener = runtime.FileListener(context)
        thread = threading.Thread(target=listener.run); thread.start()
        try:
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and not any(row["state"] == "completed" for row in context.output(3)):
                time.sleep(.01)
            self.assertTrue(any(row["state"] == "error" and row["request_id"] == "bad" for row in context.output(3)))
            self.assertTrue(any(row["state"] == "completed" and row["request_id"] == "good" for row in context.output(3)))
            self.assertTrue(all(result.status != "failed" for result in context.results))
        finally:
            context.stopped.set(); thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_fresh_events_no_cached_replay(self):
        attributes = {"file_in": NS(port_id=1, status="available", value="old.wav"),
                      "command_in": NS(port_id=2, status="updated", value={"action": "interrupt"})}
        context = NS(input_attribute=attributes.get, input_events=[NS(input_port_id=2, value={"action":"interrupt"})])
        self.assertEqual(runtime.fresh(context, "file_in"), [])
        self.assertEqual(runtime.fresh(context, "command_in"), [{"action": "interrupt"}])

    def test_preparation_simulation_and_reordered_ports(self):
        block = AudioFileStreamBlock()
        context = BlockRuntimeContext(kind=block.kind, node_id="bridge", runtime_mode="centralized",
            config=block.default_config(), root_dir=self.root,
            input_ports=tuple(NS(**port) for port in reversed(block.model["ports"]["inputs"])),
            output_ports=tuple(NS(**port) for port in reversed(block.model["ports"]["outputs"])),
            inputs={"file_in": str(self.source)})
        with patch.object(files, "prepare", side_effect=AssertionError("No simulation IO")), patch.object(runtime, "prepare", side_effect=AssertionError("No simulation IO")):
            self.assertFalse(block.prepare_runtime(BlockRuntimePreparationContext.from_context(context)).listen_on_run)
            self.assertEqual(block.execute_runtime(context).status, "skipped")
            context.runtime_mode = "zeromq_active"
            self.assertTrue(block.prepare_runtime(BlockRuntimePreparationContext.from_context(context)).listen_on_run)
        context.input_ports = context.input_ports[:1]
        with self.assertRaises(AudioFileError): block.prepare_runtime(BlockRuntimePreparationContext.from_context(context))

    def test_converter_timeout_interrupt_and_parent_death(self):
        """A deliberately stuck converter double tests fencing; other tests use real FFmpeg."""
        executable = self.root / "ffmpeg"
        executable.write_text("#!" + sys.executable + "\n" +
            "import json,os,subprocess,sys,time\nfrom pathlib import Path\n" +
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n" +
            "Path(sys.argv[-1]).with_suffix('.pid').write_text(json.dumps([os.getpid(),child.pid]))\n" +
            "time.sleep(30)\n", encoding="utf-8")
        executable.chmod(0o700)
        output = self.root / "output.ogg"
        marker = output.with_suffix(".pid")
        def alive(pid):
            try: return Path('/proc',str(pid),'stat').read_text().split(') ',1)[1].split()[0] not in {'Z','X'}
            except FileNotFoundError: return False
        def gone():
            if not marker.exists(): return
            pids = json.loads(marker.read_text())
            deadline = time.monotonic() + 2
            while any(alive(pid) for pid in pids) and time.monotonic() < deadline: time.sleep(.01)
            self.assertFalse(any(alive(pid) for pid in pids), pids)
            marker.unlink()
        with patch.dict(os.environ, {"PATH":str(self.root)+os.pathsep+os.environ.get('PATH','')}):
            with self.assertRaises(AudioFileError):
                files.convert(self.source, output, is_ogg=False, config=self.config, directory=self.root,
                              deadline=time.monotonic()+.4, cancel=lambda:False)
            self.assertTrue(marker.exists()); gone()
            with self.assertRaises(Cancelled):
                files.convert(self.source, output, is_ogg=False, config=self.config, directory=self.root,
                              deadline=time.monotonic()+4, cancel=marker.exists)
            self.assertTrue(marker.exists()); gone()
            code = '\n'.join([
                'from pathlib import Path;import sys,time',
                'from blocs.audio_file_stream.files import convert',
                'from blocs.audio_file_stream.configuration import configuration',
                'root=Path(sys.argv[1])',
                'convert(root/"voice.wav",root/"output.ogg",is_ogg=False,config=configuration({}),directory=root,deadline=time.monotonic()+20,cancel=lambda:False)'])
            parent = subprocess.Popen([sys.executable,'-B','-c',code,str(self.root)])
            try:
                deadline=time.monotonic()+3
                while not marker.exists() and time.monotonic()<deadline: time.sleep(.01)
                self.assertTrue(marker.exists())
                parent.kill(); parent.wait(timeout=3); gone()
            finally:
                if parent.poll() is None: parent.kill(); parent.wait(timeout=3)
                if marker.exists():
                    for pid in json.loads(marker.read_text()):
                        if alive(pid): os.kill(pid,signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
