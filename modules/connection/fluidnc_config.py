"""FluidNC configuration utilities.

Provides functions to read/write FluidNC settings via the main serial/WebSocket
connection. Uses $Config/Dump for bulk reads (single round-trip) and individual
$/path=value writes.

Pin settings (direction_pin, etc.) cannot be changed at runtime — FluidNC
requires editing the config YAML file on the controller's filesystem and
rebooting. For these settings, we download the YAML via $CD, modify it,
upload via HTTP to FluidNC's web server, and reboot with $Bye.

Targets FluidNC-based boards with bipolar stepper motors (DLC32, MKS boards).
"""

import copy
import time
import logging
import yaml
from modules.core.state import state

logger = logging.getLogger(__name__)

# Curated settings exposed in the Setup UI.
# Keys are FluidNC config tree paths queried via $/path.
CURATED_SETTINGS = {
    "x": [
        "axes/x/steps_per_mm",
        "axes/x/max_rate_mm_per_min",
        "axes/x/acceleration_mm_per_sec2",
        "axes/x/motor0/stepstick/direction_pin",
        "axes/x/homing/cycle",
        "axes/x/homing/positive_direction",
        "axes/x/homing/mpos_mm",
        "axes/x/homing/feed_mm_per_min",
        "axes/x/homing/seek_mm_per_min",
        "axes/x/homing/settle_ms",
        "axes/x/homing/seek_scaler",
        "axes/x/homing/feed_scaler",
        "axes/x/motor0/pulloff_mm",
    ],
    "y": [
        "axes/y/steps_per_mm",
        "axes/y/max_rate_mm_per_min",
        "axes/y/acceleration_mm_per_sec2",
        "axes/y/motor0/stepstick/direction_pin",
        "axes/y/homing/cycle",
        "axes/y/homing/positive_direction",
        "axes/y/homing/mpos_mm",
        "axes/y/homing/feed_mm_per_min",
        "axes/y/homing/seek_mm_per_min",
        "axes/y/homing/settle_ms",
        "axes/y/homing/seek_scaler",
        "axes/y/homing/feed_scaler",
        "axes/y/motor0/pulloff_mm",
    ],
    "global": [],
}


def send_command(command: str, timeout: float = 3.0, silence: float = 1.0) -> list[str]:
    """Send a command via the main connection and return response lines.

    Clears the input buffer, sends the command, then reads lines until
    'ok', 'error', silence gap, or timeout is reached.

    Args:
        command: The FluidNC command string.
        timeout: Absolute max wait time in seconds.
        silence: After receiving data, if no new data arrives for this many
                 seconds, consider the response complete. Handles commands
                 like $CD that may not end with 'ok'.
    """
    if not state.conn or not state.conn.is_connected():
        raise ConnectionError("Not connected to controller")

    # Clear input buffer
    try:
        while state.conn.in_waiting() > 0:
            state.conn.readline()
    except Exception:
        pass

    # Send command
    state.conn.send(command + "\n")
    time.sleep(0.2)

    # Read response lines
    lines: list[str] = []
    start_time = time.time()
    last_data_time = start_time
    got_data = False
    while time.time() - start_time < timeout:
        try:
            if state.conn.in_waiting() > 0:
                response = state.conn.readline()
                if response:
                    line = response.strip() if isinstance(response, str) else response.decode("utf-8", errors="replace").strip()
                    if line:
                        lines.append(line)
                        got_data = True
                        last_data_time = time.time()
                        if line.lower() == "ok" or line.lower().startswith("error"):
                            break
            else:
                # If data was flowing but has gone silent, we're done
                if got_data and (time.time() - last_data_time > silence):
                    break
                time.sleep(0.05)
        except Exception as e:
            logger.warning(f"Error reading response: {e}")
            break

    return lines


def read_setting(path: str) -> str | None:
    """Query a single FluidNC setting by config-tree path.

    Returns the value string, or None if the setting doesn't exist
    (e.g. stepstick path on a unipolar board).
    """
    try:
        responses = send_command(f"$/{path}")
    except ConnectionError:
        return None

    # Response format: "/axes/x/steps_per_mm=200.000" or similar
    leaf = path.split("/")[-1]
    for line in responses:
        if "=" in line and leaf in line:
            return line.split("=", 1)[1].strip()
        if line.lower().startswith("error"):
            return None
    return None


def _parse_value(raw: str | None, path: str) -> object:
    """Parse a raw FluidNC value string into a typed Python value."""
    if raw is None:
        return None

    # Direction pin — keep raw string but also derive inverted flag
    if "direction_pin" in path:
        return raw

    # Boolean-ish
    lower = raw.lower()
    if lower in ("true", "false"):
        return lower == "true"

    # Numeric
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _resolve_yaml_path(data: dict, path: str):
    """Walk a nested dict by slash-separated path. Returns None if any key is missing."""
    node = data
    for key in path.split("/"):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _make_key(path: str) -> str:
    """Convert a FluidNC config path to a flat UI key.

    e.g. "axes/x/motor0/stepstick/direction_pin" → "direction_pin"
         "axes/x/homing/feed_mm_per_min"         → "homing_feed_mm_per_min"
         "axes/x/motor0/hard_limits"              → "hard_limits"
    """
    parts = path.split("/")
    remaining = parts[2:]  # drop "axes/x" prefix
    remaining = [p for p in remaining if p not in ("motor0", "stepstick")]
    return "_".join(remaining)


def read_all_settings() -> dict:
    """Read all curated settings from the controller via $Config/Dump.

    Issues a single $Config/Dump command, parses the YAML response, and
    extracts curated settings. Much faster than individual queries
    (~1s vs ~9s for 30 settings).

    Returns a structured dict:
    {
        "axes": {
            "x": { "steps_per_mm": 320.0, "direction_inverted": True, "direction_pin": "i2so.2:low", ... },
            "y": { ... }
        },
        "start": { "must_home": False }
    }
    """
    lines = send_command("$CD", timeout=10.0, silence=1.5)
    logger.info(f"$CD returned {len(lines)} lines")

    # Filter out non-YAML lines (status messages, 'ok', etc.)
    yaml_lines = []
    for line in lines:
        if line.lower() == "ok" or line.lower().startswith("error"):
            continue
        # Skip FluidNC status messages like [MSG:...]
        if line.startswith("["):
            continue
        yaml_lines.append(line)

    yaml_text = "\n".join(yaml_lines)
    if not yaml_text.strip():
        logger.warning("$CD returned no YAML content, falling back to individual queries")
        return _read_all_settings_individual()

    try:
        config = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        logger.error(f"Failed to parse $CD YAML ({len(yaml_lines)} lines): {e}")
        return _read_all_settings_individual()

    if not isinstance(config, dict):
        logger.warning(f"$CD parsed as {type(config).__name__}, falling back to individual queries")
        return _read_all_settings_individual()

    logger.info(f"$CD YAML top-level keys: {list(config.keys())}")

    result: dict = {"axes": {"x": {}, "y": {}}, "start": {}}
    resolved_count = 0

    for axis in ("x", "y"):
        for path in CURATED_SETTINGS[axis]:
            raw = _resolve_yaml_path(config, path)
            key = _make_key(path)

            if raw is not None:
                resolved_count += 1

            if "direction_pin" in path:
                raw_str = str(raw) if raw is not None else None
                result["axes"][axis][key] = raw_str
                result["axes"][axis]["direction_inverted"] = (
                    ":low" in raw_str if raw_str else None
                )
            else:
                result["axes"][axis][key] = raw

    for path in CURATED_SETTINGS["global"]:
        raw = _resolve_yaml_path(config, path)
        if raw is not None:
            resolved_count += 1
        parts = path.split("/")
        key = "_".join(parts[1:])
        result["start"][key] = raw

    # If $CD parsed but we couldn't resolve any of our settings,
    # the YAML structure doesn't match — fall back to individual queries
    if resolved_count == 0:
        logger.warning(
            f"$CD YAML parsed ({len(config)} keys) but no curated settings resolved. "
            f"Falling back to individual queries."
        )
        return _read_all_settings_individual()

    logger.info(f"$CD resolved {resolved_count}/{len(CURATED_SETTINGS['x']) * 2 + len(CURATED_SETTINGS['global'])} settings")
    return result


def _read_all_settings_individual() -> dict:
    """Fallback: read curated settings one by one via $/path queries."""
    result: dict = {"axes": {"x": {}, "y": {}}, "start": {}}

    for axis in ("x", "y"):
        for path in CURATED_SETTINGS[axis]:
            raw = read_setting(path)
            key = _make_key(path)
            parsed = _parse_value(raw, path)
            result["axes"][axis][key] = parsed

            if "direction_pin" in path:
                result["axes"][axis]["direction_inverted"] = (
                    ":low" in raw if raw else None
                )

    for path in CURATED_SETTINGS["global"]:
        raw = read_setting(path)
        parts = path.split("/")
        key = "_".join(parts[1:])
        result["start"][key] = _parse_value(raw, path)

    return result


def write_setting(path: str, value: str) -> bool:
    """Write a single FluidNC setting. Returns True on success."""
    try:
        responses = send_command(f"$/{path}={value}")
    except ConnectionError:
        return False
    return any("ok" in r.lower() for r in responses)


def get_config_filename() -> str:
    """Get the active config filename from FluidNC."""
    try:
        responses = send_command("$Config/Filename")
    except ConnectionError:
        return "config.yaml"

    for line in responses:
        if "=" in line and "Filename" in line:
            return line.split("=", 1)[1].strip()
    return "config.yaml"


def save_config() -> bool:
    """Persist current in-RAM config to flash using the active config filename."""
    filename = get_config_filename()
    try:
        responses = send_command(f"$CD=/littlefs/{filename}", timeout=5.0)
    except ConnectionError:
        return False
    return any("ok" in r.lower() for r in responses)


def toggle_direction_pin(axis: str) -> tuple[bool, bool]:
    """Toggle :low on the direction pin for the given axis.

    Pin objects cannot be changed at runtime in FluidNC — this function
    downloads the config YAML, modifies the pin value, uploads the modified
    file to the controller via HTTP, and reboots.

    Returns (success, new_inverted_state).

    Raises:
        RuntimeError: If the ESP32 is not reachable via HTTP.
    """
    path = f"axes/{axis}/motor0/stepstick/direction_pin"
    current = read_setting(path)
    if current is None:
        return (False, False)

    if ":low" in current:
        new_val = current.replace(":low", "")
    else:
        new_val = current + ":low"

    success = update_config_yaml(path, new_val)
    return (success, ":low" in new_val)


###############################################################################
# Config YAML download / modify / upload (for non-runtime settings like pins)
###############################################################################

_ESP32_CANDIDATE_URLS = [
    "http://fluidnc.local",
    "http://fluidnc.local:80",
    "http://fluidnc.local:81",
]


def discover_esp32_url() -> str | None:
    """Try to find the ESP32's HTTP URL by probing known candidates.

    Checks state.esp32_url first (user override), then tries mDNS defaults.
    Returns the first URL that responds, or None.
    """
    import requests as _requests

    if state.esp32_url:
        try:
            resp = _requests.get(state.esp32_url, timeout=3)
            if resp.status_code < 500:
                logger.info(f"ESP32 reachable at configured URL: {state.esp32_url}")
                return state.esp32_url
        except _requests.RequestException:
            logger.warning(f"Configured ESP32 URL unreachable: {state.esp32_url}")

    for url in _ESP32_CANDIDATE_URLS:
        try:
            resp = _requests.get(url, timeout=3)
            if resp.status_code < 500:
                logger.info(f"ESP32 discovered at: {url}")
                return url
        except _requests.RequestException:
            continue

    logger.warning("ESP32 not reachable via HTTP at any known URL")
    return None


def download_config_yaml() -> str:
    """Download the full config YAML from the controller via $CD.

    Returns the raw YAML text (non-YAML lines like [MSG:...] and 'ok' filtered out).

    Raises:
        ConnectionError: If not connected to the controller.
        ValueError: If $CD returned no usable YAML content.
    """
    lines = send_command("$CD", timeout=10.0, silence=1.5)

    yaml_lines = []
    for line in lines:
        if line.lower() == "ok" or line.lower().startswith("error"):
            continue
        if line.startswith("["):
            continue
        yaml_lines.append(line)

    yaml_text = "\n".join(yaml_lines)
    if not yaml_text.strip():
        raise ValueError("$CD returned no YAML content")

    # Validate it parses
    yaml.safe_load(yaml_text)
    return yaml_text


def _set_yaml_value(data: dict, path: str, value) -> dict:
    """Return a new dict with the value at the slash-separated path replaced.

    Creates intermediate dicts if needed. Does not mutate the original.
    """
    keys = path.split("/")
    result = copy.deepcopy(data)
    node = result
    for key in keys[:-1]:
        if key not in node or not isinstance(node[key], dict):
            node[key] = {}
        node = node[key]
    node[keys[-1]] = value
    return result


def upload_config_to_controller(yaml_content: str, esp32_url: str | None = None) -> bool:
    """Upload a modified config YAML to the ESP32 via FluidNC's HTTP API.

    Uses multipart form upload to /upload_localfs, which writes directly to
    the controller's LittleFS filesystem.

    Args:
        yaml_content: The full YAML config file content.
        esp32_url: Base URL of the ESP32 (e.g., "http://fluidnc.local").
                   If None, auto-discovers.

    Returns:
        True if upload succeeded, False otherwise.

    Raises:
        RuntimeError: If ESP32 is not reachable via HTTP.
    """
    import requests as _requests

    url = esp32_url or discover_esp32_url()
    if not url:
        raise RuntimeError(
            "Cannot upload config: ESP32 is not reachable via HTTP. "
            "Set esp32_url in settings to the controller's HTTP address "
            "(e.g., http://fluidnc.local), or ensure the ESP32 has WiFi enabled."
        )

    filename = get_config_filename()
    upload_url = f"{url.rstrip('/')}/upload_localfs"

    try:
        resp = _requests.post(
            upload_url,
            files={"myfile": (filename, yaml_content.encode("utf-8"), "text/yaml")},
            data={"path": f"/littlefs/{filename}"},
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info(f"Config uploaded to {upload_url} as /littlefs/{filename}")
            return True
        else:
            logger.error(f"Config upload failed: HTTP {resp.status_code} — {resp.text}")
            return False
    except _requests.RequestException as e:
        logger.error(f"Config upload request failed: {e}")
        raise RuntimeError(f"Failed to upload config to ESP32 at {upload_url}: {e}")


def update_config_yaml(path: str, value) -> bool:
    """High-level: modify a single value in the controller's config YAML.

    Downloads the current config, modifies the value, uploads the new config,
    and reboots the controller so the change takes effect.

    Use this for settings that cannot be changed at runtime (e.g., pin objects).
    For runtime-settable values, use write_setting() instead.

    Args:
        path: FluidNC config tree path (e.g., "axes/x/motor0/stepstick/direction_pin").
        value: The new value to set.

    Returns:
        True if the config was successfully updated and controller rebooted.

    Raises:
        ConnectionError: If not connected to the controller.
        ValueError: If the config YAML could not be downloaded or parsed.
        RuntimeError: If the ESP32 is not reachable via HTTP for upload.
    """
    logger.info(f"Updating config YAML: {path} = {value}")

    # 1. Download current config
    yaml_text = download_config_yaml()
    config = yaml.safe_load(yaml_text)

    # 2. Modify the value
    updated_config = _set_yaml_value(config, path, value)

    # 3. Serialize back to YAML
    updated_yaml = yaml.dump(updated_config, default_flow_style=False, sort_keys=False)

    # 4. Upload to controller
    uploaded = upload_config_to_controller(updated_yaml)
    if not uploaded:
        return False

    # 5. Reboot controller to apply
    logger.info("Rebooting controller to apply config changes...")
    try:
        send_command("$Bye", timeout=5.0)
    except ConnectionError:
        logger.warning("Lost connection after $Bye (expected during reboot)")

    logger.info(f"Config updated successfully: {path} = {value}")
    return True
