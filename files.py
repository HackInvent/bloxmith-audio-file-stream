"""Validate completed local audio before any publication; never open a remote URL."""
from contextlib import contextmanager, suppress
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time
import wave

from .configuration import AudioFileError, Cancelled
from .ogg_stream import OggOpusStream


def check(deadline, cancel):
    if cancel():
        raise Cancelled("Audio file request interrupted.")
    if time.monotonic() >= deadline:
        raise AudioFileError("Audio preparation timed out; no partial stream was published.")


@contextmanager
def open_source(raw_path, allowed_root, app_root):
    """Open beneath an explicit directory using no-follow descriptor traversal.

    All components, including the configured root, reject symlinks. A source is a
    regular file, not a FIFO/device/directory. This is file admission, not an OS sandbox.
    """
    if not allowed_root:
        raise AudioFileError("Choose the allowed source directory in the block settings first.")
    root = Path(allowed_root).expanduser()
    if not root.is_absolute():
        root = Path(app_root) / root
    root = Path(os.path.abspath(root))
    source = Path(raw_path).expanduser()
    source = source if source.is_absolute() else root / source
    # Reject traversal explicitly instead of normalizing an escaped input back in.
    if ".." in source.parts:
        raise AudioFileError("Parent-directory traversal is refused.")
    try:
        relative = source.relative_to(root)
    except ValueError:
        raise AudioFileError("The audio file is outside the allowed directory.") from None
    if not relative.parts:
        raise AudioFileError("Choose an audio file, not the source directory.")
    descriptor = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
    file_descriptor = None
    try:
        for part in (*root.parts[1:], *relative.parts[:-1]):
            next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(relative.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=descriptor)
        if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
            raise AudioFileError("The source must be a regular audio file.")
        with os.fdopen(file_descriptor, "rb") as stream:
            file_descriptor = None
            yield stream
    except OSError:
        raise AudioFileError("The local audio file is missing, unreadable or uses a symlink.") from None
    finally:
        os.close(descriptor)
        if file_descriptor is not None:
            os.close(file_descriptor)


def snapshot(path, config, app_root, directory, deadline, cancel):
    """Bound copying and reject files changed while being copied."""
    target = directory / "input.audio"
    maximum = config["max_input_mib"] * 1024 * 1024
    check(deadline, cancel)
    with open_source(path, config["allowed_root"], app_root) as source:
        before = os.fstat(source.fileno())
        if not 0 < before.st_size <= maximum:
            raise AudioFileError("The source file is empty or exceeds the configured size limit.")
        total = 0
        with target.open("xb") as output:
            while True:
                check(deadline, cancel)
                chunk = source.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise AudioFileError("The source grew beyond the configured size limit.")
                output.write(chunk)
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or total != before.st_size:
            raise AudioFileError("The source changed while being read; send a completed file.")
    return target


def inspect_ogg(path, maximum_seconds, deadline, cancel):
    parser = OggOpusStream()
    with path.open("rb") as source:
        while True:
            check(deadline, cancel)
            chunk = source.read(4096)
            if not chunk:
                break
            for _page in parser.push(chunk):
                if parser.seconds > maximum_seconds:
                    raise AudioFileError("The audio exceeds the configured duration limit.")
    parser.finish()
    return parser.seconds, parser.channels


def inspect_wave(path, maximum_seconds, deadline, cancel):
    """PCM16 mono/stereo only; consume every declared frame to reject truncated WAV."""
    try:
        with wave.open(str(path), "rb") as source:
            frames, rate = source.getnframes(), source.getframerate()
            channels, width = source.getnchannels(), source.getsampwidth()
            if width != 2 or channels not in (1, 2) or not 8000 <= rate <= 48000 or source.getcomptype() != "NONE":
                raise AudioFileError("WAV must be uncompressed PCM16 mono/stereo at 8–48 kHz.")
            duration = frames / rate
            if not 0 < duration <= maximum_seconds:
                raise AudioFileError("The WAV is empty or exceeds the configured duration limit.")
            consumed = 0
            while True:
                check(deadline, cancel)
                chunk = source.readframes(8192)
                if not chunk:
                    break
                consumed += len(chunk)
            if consumed != frames * channels * width:
                raise AudioFileError("The WAV audio data is truncated.")
            return duration, channels
    except (wave.Error, EOFError):
        raise AudioFileError("Invalid or incomplete PCM16 WAV file.") from None


def convert(source, output, *, is_ogg, config, directory, deadline, cancel):
    """Owned finite FFmpeg conversion/remux with bounded pipes and parent-death fencing."""
    check(deadline, cancel)
    if sys.platform != "linux":
        raise AudioFileError("Audio File Stream currently requires Linux.")
    codec = ["-c:a", "copy"] if is_ogg else ["-c:a", "libopus", "-ar", "48000", "-b:a", str(config["bitrate_kbps"]) + "k", "-frame_duration", "20"]
    args = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror",
            "-protocol_whitelist", "file,pipe", "-f", "ogg" if is_ogg else "wav", "-i", str(source),
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1", "-threads", "1", *codec,
            "-f", "ogg", "-page_duration", "20000", "-n", str(output)]
    command = [sys.executable, "-E", "-B", str(Path(__file__).with_name("process_guard.py")), str(os.getpid()), *args]
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
    child = subprocess.Popen(command, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
    selector = selectors.DefaultSelector()
    count = 0
    try:
        os.set_blocking(child.stderr.fileno(), False)
        selector.register(child.stderr, selectors.EVENT_READ)
        while selector.get_map():
            check(deadline, cancel)
            for key, _ in selector.select(.02):
                data = os.read(key.fd, 8192)
                if not data:
                    selector.unregister(key.fileobj)
                count += len(data)
                if count > 65536:
                    raise AudioFileError("Audio conversion exceeded its diagnostic limit.")
            if output.exists() and output.stat().st_size > 32 * 1024 * 1024:
                raise AudioFileError("Converted audio exceeds 32 MiB.")
        while child.poll() is None:
            check(deadline, cancel)
            time.sleep(.01)
        check(deadline, cancel)
        if child.returncode != 0:
            raise AudioFileError("FFmpeg could not prepare the complete audio; check its Opus support.")
    finally:
        selector.close()
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=2)
        child.stderr.close()


def prepare(path, config, app_root, directory, cancel):
    """Snapshot, validate and prepare a complete Opus stream before start is emitted."""
    deadline = time.monotonic() + config["prepare_timeout_sec"]
    source = snapshot(path, config, app_root, directory, deadline, cancel)
    with source.open("rb") as stream:
        signature = stream.read(12)
    is_ogg = signature[:4] == b"OggS"
    if is_ogg:
        original_duration, channels = inspect_ogg(source, config["max_audio_sec"], deadline, cancel)
    elif signature[:4] == b"RIFF" and signature[8:] == b"WAVE":
        original_duration, channels = inspect_wave(source, config["max_audio_sec"], deadline, cancel)
    else:
        raise AudioFileError("Only completed Ogg/Opus and PCM16 WAV files are supported.")
    output = directory / "stream.ogg"
    convert(source, output, is_ogg=is_ogg, config=config, directory=directory, deadline=deadline, cancel=cancel)
    duration, output_channels = inspect_ogg(output, config["max_audio_sec"] + .05, deadline, cancel)
    if output.stat().st_size > 32 * 1024 * 1024 or abs(duration - original_duration) > .05 or channels != output_channels:
        raise AudioFileError("Converted audio duration or channel count is inconsistent.")
    return output, duration, channels
