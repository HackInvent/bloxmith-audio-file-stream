"""Small, bounded and side-effect-free file-stream contract."""
from collections.abc import Mapping
import json
import math
import re
from uuid import uuid4

DEFAULTS = {"allowed_root": "", "max_input_mib": 32, "max_audio_sec": 120,
            "prepare_timeout_sec": 30, "queue_size": 4, "bitrate_kbps": 96}
LIMITS = {"max_input_mib": (1, 64), "max_audio_sec": (1, 600),
          "prepare_timeout_sec": (1, 120), "queue_size": (1, 16), "bitrate_kbps": (24, 192)}
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")


class AudioFileError(ValueError):
    """Safe diagnostics never contain source content or external tool output."""


class Cancelled(AudioFileError):
    """A business interruption is recoverable; framework Stop revokes publication."""


def configuration(raw):
    if raw is not None and not isinstance(raw, Mapping):
        raise AudioFileError("Invalid audio file settings.")
    raw = raw or {}
    if set(raw) - set(DEFAULTS) - {"position", "runtime_path", "runtime_path_label", "execution"}:
        raise AudioFileError("Unsupported Audio File Stream configuration setting.")
    result = {key: raw.get(key, value) for key, value in DEFAULTS.items()}
    root = result["allowed_root"]
    if not isinstance(root, str) or len(root) > 4096 or any(char in root for char in ("\0", "\n", "\r")):
        raise AudioFileError("Choose one valid local source directory.")
    result["allowed_root"] = root.strip()
    for key, (minimum, maximum) in LIMITS.items():
        value = result[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                or value != int(value) or not minimum <= value <= maximum):
            raise AudioFileError(f"{key} must be an integer between {minimum} and {maximum}.")
        result[key] = int(value)
    return result


def request(raw):
    """Accept a path or a bounded JSON envelope, never a URL or inline audio bytes."""
    if isinstance(raw, str) and raw.lstrip().startswith("{"):
        try:
            if len(raw) > 8192:
                raise ValueError()
            def unique(pairs):
                result = dict(pairs)
                if len(result) != len(pairs):
                    raise ValueError()
                return result
            raw = json.loads(raw, object_pairs_hook=unique)
        except (ValueError, RecursionError):
            raise AudioFileError("Invalid file request JSON.") from None
    if isinstance(raw, str):
        raw = {"path": raw}
    if not isinstance(raw, Mapping) or set(raw) - {"path", "request_id"} or "path" not in raw:
        raise AudioFileError("Send a local path or an object with path and optional request_id.")
    path = raw["path"]
    ident = raw.get("request_id", uuid4().hex)
    if (not isinstance(path, str) or not path.strip() or len(path) > 4096
            or any(char in path for char in ("\0", "\n", "\r")) or "://" in path):
        raise AudioFileError("Only a bounded local file path is accepted; URLs are refused.")
    if not isinstance(ident, str) or not IDENTIFIER.fullmatch(ident):
        raise AudioFileError("request_id must use 1–128 letters, digits, underscores or hyphens.")
    return {"path": path, "request_id": ident}


def interrupt(raw):
    if isinstance(raw, str):
        try:
            if len(raw) > 128:
                raise ValueError()
            def unique(pairs):
                value = dict(pairs)
                if len(value) != len(pairs):
                    raise ValueError()
                return value
            raw = json.loads(raw, object_pairs_hook=unique)
        except (ValueError, RecursionError):
            raise AudioFileError('command_in expects {"action":"interrupt"}.') from None
    if not isinstance(raw, Mapping) or dict(raw) != {"action": "interrupt"}:
        raise AudioFileError('command_in expects {"action":"interrupt"}.')
    return {"action": "interrupt"}


def validate_ports(context):
    """Stable identifiers, not visual order, determine the fixed contract."""
    for ports, expected in ((context.input_ports, {1: ("file_in", "message", "one"),
                                                  2: ("command_in", "message", "one")}),
                            (context.output_ports, {1: ("audio_out", "audio_stream", "many"),
                                                   2: ("command_out", "message", "many"),
                                                   3: ("status", "message", "many")})):
        if len(ports) != len(expected) or {port.id for port in ports} != set(expected):
            raise AudioFileError("Restore the fixed Audio File Stream ports.")
        for port in ports:
            if (port.name, port.transport, port.multiplicity) != expected[port.id]:
                raise AudioFileError("Restore the fixed Audio File Stream port names and transports.")
    if any(port.required for port in context.input_ports):
        raise AudioFileError("File and interrupt inputs must remain independent and non-required.")
    audio = next(port for port in context.output_ports if port.id == 1)
    profile = audio.audio_stream
    for key, expected in (("codecs", ("opus",)), ("sample_rates_hz", (48000,)), ("channels", (1, 2))):
        values = profile.get(key, ()) if isinstance(profile, Mapping) else getattr(profile, key, ())
        if tuple(values) != expected:
            raise AudioFileError("audio_out must keep the Opus 48 kHz mono/stereo profile.")
