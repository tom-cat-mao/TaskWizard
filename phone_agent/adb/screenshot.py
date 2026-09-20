"""Screenshot utilities for capturing Android device screen."""

import base64
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from io import BytesIO

from PIL import Image


@dataclass
class Screenshot:
    """Represents a captured screenshot."""

    base64_data: str
    width: int
    height: int
    mime_type: str = "image/png"
    is_sensitive: bool = False
    is_valid: bool = True
    is_placeholder: bool = False
    failure_code: str | None = None
    failure_message: str | None = None


def get_screenshot(
    device_id: str | None = None,
    timeout: int = 10,
    *,
    black_screen_detect: bool | None = None,
    use_exec_out: bool | None = None,
) -> Screenshot:
    """
    Capture a screenshot from the connected Android device.

    Args:
        device_id: Optional ADB device ID for multi-device setups.
        timeout: Timeout in seconds for screenshot operations.
        black_screen_detect: Whether a decoded uniformly black image should be
            treated as a system-protected screen. ``None`` reads
            ``PHONE_AGENT_BLACK_SCREEN_DETECT`` (default on).
        use_exec_out: Whether to capture through ``adb exec-out screencap -p``
            (one ADB roundtrip, PNG read from stdout) instead of the legacy
            write-on-device + pull + rm path (three roundtrips). ``None`` reads
            ``PHONE_AGENT_SCREENSHOT_USE_EXEC_OUT`` (default on; only
            ``1``/``true``/``yes``/``on`` enable it).

    Returns:
        Screenshot object containing base64 data and dimensions.

    Note:
        If capture fails, the returned object is an invalid placeholder with
        is_valid=False, is_placeholder=True, and a stable failure_code. Graph
        nodes must treat it as unavailable and fail closed before model calls.
    """

    if use_exec_out is None:
        use_exec_out = _env_bool("PHONE_AGENT_SCREENSHOT_USE_EXEC_OUT", True)

    adb_prefix = _get_adb_prefix(device_id)

    try:
        if use_exec_out:
            return _exec_out_screencap(adb_prefix, timeout, black_screen_detect)
        # Legacy path: screencap to a device file + pull + cleanup.
        return _legacy_screencap(
            adb_prefix,
            timeout,
            f"/sdcard/tmp_{uuid.uuid4().hex}.png",
            black_screen_detect,
        )

    except Exception as e:
        print(f"Screenshot error: {e}")
        return _create_fallback_screenshot(
            is_sensitive=False,
            failure_code="screenshot_unavailable",
            failure_message=type(e).__name__,
        )


def _env_bool(key: str, default: bool) -> bool:
    """Read an on/off env switch; only ``1``/``true``/``yes``/``on`` count as on."""

    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _exec_out_screencap(
    adb_prefix: list[str], timeout: int, black_screen_detect: bool | None
) -> Screenshot:
    """Read one PNG from ``exec-out screencap -p`` stdout (single ADB roundtrip)."""

    try:
        result = subprocess.run(
            adb_prefix + ["exec-out", "screencap", "-p"],
            capture_output=True,
            timeout=timeout,
        )

        if result.returncode != 0:
            return _create_fallback_screenshot(
                is_sensitive=False,
                failure_code="adb_screencap_failed",
                failure_message=f"screencap exited with status {result.returncode}",
            )

        png_data = result.stdout
        if not png_data:
            return _create_fallback_screenshot(
                is_sensitive=False,
                failure_code="empty_screenshot",
                failure_message="No PNG data received",
            )

        img = Image.open(BytesIO(png_data))
        width, height = img.size

        if _black_screen_detect_enabled(black_screen_detect) and _is_uniform_black(img):
            return _create_fallback_screenshot(
                is_sensitive=True,
                failure_code="secure_screenshot_blocked",
                failure_message="系统级保护页（登录/支付等），截图不可用",
            )

        buffered = BytesIO()
        mime_type = _save_model_image(img, buffered)
        base64_data = base64.b64encode(buffered.getvalue()).decode("utf-8")

        return Screenshot(
            base64_data=base64_data,
            width=width,
            height=height,
            mime_type=mime_type,
            is_sensitive=False,
        )

    except subprocess.TimeoutExpired:
        return _create_fallback_screenshot(
            is_sensitive=False,
            failure_code="screenshot_timeout",
            failure_message="ADB screenshot timed out",
        )
    except Exception as exc:
        return _create_fallback_screenshot(
            is_sensitive=False,
            failure_code="screenshot_exec_out_failed",
            failure_message=str(exc),
        )


def _legacy_screencap(
    adb_prefix: list[str],
    timeout: int,
    device_temp_path: str,
    black_screen_detect: bool | None,
) -> Screenshot:
    """Legacy path: screencap to a device file + pull + rm (3 ADB roundtrips)."""

    temp_path = os.path.join(tempfile.gettempdir(), f"screenshot_{uuid.uuid4()}.png")

    try:
        # Execute screenshot command
        result = subprocess.run(
            adb_prefix + ["shell", "screencap", "-p", device_temp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        output = result.stdout + result.stderr
        if "Status: -1" in output or "Failed" in output:
            return _create_fallback_screenshot(
                is_sensitive=True,
                failure_code="secure_screenshot_blocked",
                failure_message="系统级保护页（登录/支付等），截图不可用",
            )
        if result.returncode != 0:
            return _create_fallback_screenshot(
                is_sensitive=False,
                failure_code="adb_screencap_failed",
                failure_message=f"screencap exited with status {result.returncode}",
            )

        # Pull screenshot to local temp path
        pull_result = subprocess.run(
            adb_prefix + ["pull", device_temp_path, temp_path],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if pull_result.returncode != 0 or not os.path.exists(temp_path):
            return _create_fallback_screenshot(
                is_sensitive=False,
                failure_code="screenshot_pull_failed",
                failure_message="ADB screenshot pull failed",
            )

        # Read and encode image
        img = Image.open(temp_path)
        width, height = img.size
        if _black_screen_detect_enabled(black_screen_detect) and _is_uniform_black(
            img
        ):
            return _create_fallback_screenshot(
                is_sensitive=True,
                failure_code="secure_screenshot_blocked",
                failure_message="系统级保护页（登录/支付等），截图不可用",
            )

        buffered = BytesIO()
        mime_type = _save_model_image(img, buffered)
        base64_data = base64.b64encode(buffered.getvalue()).decode("utf-8")

        return Screenshot(
            base64_data=base64_data,
            width=width,
            height=height,
            mime_type=mime_type,
            is_sensitive=False,
        )

    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        try:
            subprocess.run(
                adb_prefix + ["shell", "rm", "-f", device_temp_path],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception:
            pass


def _get_adb_prefix(device_id: str | None) -> list:
    """Get ADB command prefix with optional device specifier."""
    if device_id:
        return ["adb", "-s", device_id]
    return ["adb"]


def _black_screen_detect_enabled(override: bool | None) -> bool:
    """Resolve the opt-out black-screen detector switch."""

    if override is not None:
        return bool(override)
    raw = os.getenv("PHONE_AGENT_BLACK_SCREEN_DETECT")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() != "off"


def _is_uniform_black(img: Image.Image) -> bool:
    """Return True when every visible colour channel has a maximum <= 4.

    Android screencap PNGs may include an opaque alpha channel, so the test is
    intentionally performed after conversion to RGB. Any real highlight, text,
    or icon above the near-black threshold keeps a dark-mode screen valid.
    """

    extrema = img.convert("RGB").getextrema()
    return all(channel_max <= 4 for _channel_min, channel_max in extrema)


def _save_model_image(img: Image.Image, buffered: BytesIO) -> str:
    """Save screenshot payload for model input while preserving screen dimensions."""
    image_format = os.getenv("PHONE_AGENT_SCREENSHOT_FORMAT", "jpeg").lower()
    if image_format in {"jpg", "jpeg"}:
        quality = _parse_jpeg_quality()
        img.convert("RGB").save(
            buffered,
            format="JPEG",
            quality=max(1, min(95, quality)),
            optimize=True,
        )
        return "image/jpeg"

    img.save(buffered, format="PNG", optimize=True)
    return "image/png"


def _parse_jpeg_quality() -> int:
    """Parse JPEG quality with a safe default for malformed env values."""
    try:
        return max(1, min(95, int(os.getenv("PHONE_AGENT_SCREENSHOT_JPEG_QUALITY", "80"))))
    except ValueError:
        return 80


def _create_fallback_screenshot(
    is_sensitive: bool,
    *,
    failure_code: str,
    failure_message: str | None = None,
) -> Screenshot:
    """Create a structured invalid placeholder when screenshot capture fails."""
    default_width, default_height = 1080, 2400

    black_img = Image.new("RGB", (default_width, default_height), color="black")
    buffered = BytesIO()
    mime_type = _save_model_image(black_img, buffered)
    base64_data = base64.b64encode(buffered.getvalue()).decode("utf-8")

    return Screenshot(
        base64_data=base64_data,
        width=default_width,
        height=default_height,
        mime_type=mime_type,
        is_sensitive=is_sensitive,
        is_valid=False,
        is_placeholder=True,
        failure_code=failure_code,
        failure_message=failure_message or failure_code,
    )
