"""Single owned listener: independent message commands and paced binary audio."""
from collections import deque
from collections.abc import Mapping
from contextlib import suppress
import json
from pathlib import Path
import tempfile
import time
from uuid import uuid4

from bloxsmith_app.block_api import BlockRuntimeOutput, BlockRuntimeResult
from .configuration import AudioFileError, Cancelled, configuration, interrupt, request, validate_ports
from .files import prepare
from .ogg_stream import OggOpusStream, OpusStreamError


def result(state, *, request_id="", detail="", **values):
    """Per-file failures remain recoverable; never stop the whole conversation Run."""
    payload = {"state": state, "request_id": request_id, **values}
    if detail:
        payload["detail"] = detail
    return BlockRuntimeResult(last_message=detail or "Audio file: " + state,
        outputs=[BlockRuntimeOutput(port_id=3, port_name="status", value=json.dumps(payload), content_type="application/json")],
        metadata={"audio_file_stream": payload})


def fresh(context, name):
    attribute = context.input_attribute(name)
    if attribute is None:
        return []
    if context.input_events:
        return [event.value for event in context.input_events if event.input_port_id == attribute.port_id]
    return [attribute.value] if attribute.status == "updated" else []


def execute(context):
    try:
        configuration(context.config)
        validate_ports(context)
        if context.runtime_mode != "zeromq_active":
            return BlockRuntimeResult(status="skipped", last_message="Simulation: no file access, conversion or audio publication.")
        commands, values = fresh(context, "command_in"), fresh(context, "file_in")
        if not commands and not values:
            return BlockRuntimeResult(status="skipped", last_message="Waiting for a fresh audio file.")
        sender = context.services.get("runtime_listener")
        if sender is None:
            raise AudioFileError("The audio file listener is unavailable; Stop then Run again.")
        # Interrupt wins if a batch also contains a file. No cached inputs are replayed.
        if commands:
            for command in commands:
                interrupt(command)
            sender.send({"action": "interrupt"})
            return BlockRuntimeResult(last_message="Interruption forwarded. Interrupt Speaker separately to clear its queue.")
        if len(values) > 16:
            raise AudioFileError("Too many files in one activation.")
        accepted = [request(raw) for raw in values]
        for item in accepted:
            sender.send({"action": "play", **item})
        return BlockRuntimeResult(last_message="File requests forwarded to the bounded audio queue.")
    except Exception as error:
        detail = str(error) if isinstance(error, AudioFileError) else "The audio file request could not be accepted."
        return result("error", detail=detail)


class FileListener:
    """All mutable state belongs to one hook, never a shared block definition."""

    def __init__(self, context):
        self.context = context
        self.config = configuration(context.config)
        self.pending = deque()
        self.epoch = 0

    def emit(self, state, **values):
        if not self.context.stop_requested():
            self.context.emit_result(result(state, **values))

    def pump(self):
        """Drain a bounded mailbox slice even during copying/conversion/pacing."""
        for _ in range(32):
            if self.context.stop_requested():
                return False
            message = self.context.receive_command(timeout_sec=0)
            if message is None:
                return True
            item = message.payload
            if item == {"action": "interrupt"}:
                discarded = len(self.pending)
                self.pending.clear()
                self.epoch += 1
                self.emit("interrupted", discarded=discarded,
                          detail="File production interrupted. Speaker needs its own interrupt command.")
                continue
            try:
                if not isinstance(item, Mapping) or item.get("action") != "play":
                    raise AudioFileError("Invalid internal file command.")
                candidate = request({key: value for key, value in item.items() if key != "action"})
                if len(self.pending) >= self.config["queue_size"]:
                    self.emit("rejected", request_id=candidate["request_id"], detail="Pending audio queue is full; this file was not retained.")
                else:
                    self.pending.append(candidate)
                    self.emit("queued", request_id=candidate["request_id"], pending=len(self.pending))
            except AudioFileError as error:
                self.emit("error", detail=str(error))
        return False

    def cancelled(self, epoch):
        self.pump()
        return self.context.stop_requested() or epoch != self.epoch

    def wait_until(self, deadline, cancel):
        while time.monotonic() < deadline:
            if cancel():
                raise Cancelled("Audio file request interrupted.")
            time.sleep(min(.01, max(0, deadline - time.monotonic())))
        if cancel():
            raise Cancelled("Audio file request interrupted.")

    def play(self, item, epoch):
        context, config = self.context, self.config
        cancel = lambda: self.cancelled(epoch)
        audio = context.services.get("runtime_audio_streams")
        if audio is None or not audio.available or not any(route.direction == "output" and route.port_name == "audio_out" for route in audio.port_routes):
            raise AudioFileError("Connect audio_out to a compatible Opus input before sending a file.")
        storage = context.services.get("get_block_storage_dir")
        if not callable(storage):
            raise AudioFileError("Block storage is unavailable.")
        directory = Path(storage())
        directory.mkdir(parents=True, exist_ok=True)
        self.emit("preparing", request_id=item["request_id"])
        with tempfile.TemporaryDirectory(prefix="file-stream-", dir=directory) as work:
            path, duration, channels = prepare(item["path"], config, context.root_dir, Path(work), cancel)
            if cancel():
                raise Cancelled("Audio file request interrupted.")
            stream_id = uuid4().hex
            frames, size = 0, 0
            started = stopped = False
            clock = time.monotonic()
            parser = OggOpusStream()

            def command(action, aborted=False):
                # Keep the shared producer contract exact: VAD deliberately rejects
                # extra lifecycle fields. Request correlation lives on audio/status.
                value = {"action": action, "stream_id": stream_id}
                if action == "stop":
                    value.update(frame_count=frames, byte_count=size, aborted=aborted)
                context.emit_result(BlockRuntimeResult(outputs=[BlockRuntimeOutput(port_id=2,
                    port_name="command_out", value=json.dumps(value), content_type="application/json")]))

            try:
                self.emit("streaming", request_id=item["request_id"], stream_id=stream_id, duration_sec=duration)
                with path.open("rb") as source:
                    previous = 0.0
                    while True:
                        if cancel():
                            raise Cancelled("Audio file request interrupted.")
                        chunk = source.read(4096)
                        if not chunk:
                            break
                        for page in parser.push(chunk):
                            # Never burst to catch up after a delayed consumer/service call.
                            clock = max(clock, time.monotonic() - previous)
                            self.wait_until(clock + previous - .10, cancel)
                            if not started:
                                command("start")
                                started = True
                            audio.publish_port("audio_out", page, codec="opus", sample_rate_hz=48000,
                                               channels=channels, stream_id=stream_id, correlation_id=item["request_id"])
                            frames += 1
                            size += len(page)
                            previous = parser.seconds
                parser.finish()
                if cancel():
                    raise Cancelled("Audio file request interrupted.")
                command("stop")
                stopped = True
                self.wait_until(clock + duration, cancel)
                self.emit("completed", request_id=item["request_id"], stream_id=stream_id,
                          frame_count=frames, byte_count=size, duration_sec=duration)
            finally:
                if started and not stopped and not context.stop_requested():
                    with suppress(Exception):
                        command("stop", aborted=True)

    def run(self):
        while not self.context.stop_requested():
            drained = self.pump()
            if self.pending and drained:
                item = self.pending.popleft()
                try:
                    self.play(item, self.epoch)
                except Cancelled:
                    self.emit("cancelled", request_id=item["request_id"])
                except Exception as error:
                    detail = str(error) if isinstance(error, (AudioFileError, OpusStreamError)) else "Audio file processing failed; no automatic retry."
                    self.emit("error", request_id=item["request_id"], detail=detail)
            else:
                time.sleep(.01)
        self.pending.clear()
