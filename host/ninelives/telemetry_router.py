# lib/telemetry_router.py - "The Black Box" Flight Recorder (v1.94 - Namespace Isolation)
# PURPOSE: Routes logs from Picos to Disk, GUI, and Alarm Manager.
# COMPLIANCE: Core 0 Spec Section 18.3, 9.1 (Logger), 2.4 (Timestamps), 20.2 (LQI)
# CHANGES:
#   - v1.94: Implemented Namespace Isolation (forensic) to fix Dual Logging Paths.
#   - v1.93: Integrated lib.protocol_parser for centralized MTIP decoding.

"""
[Spec 19.0] Ninelives Telemetry Routing System (The Black Box).

The TelemetryRouter serves as the central clearinghouse for all log data entering
the RP5 Brain. It is responsible for parsing raw byte payloads from the nervous
system and distributing them to the appropriate consumers.

Key Architectural Roles:
1. Forensic Distribution (Spec 9.1): Routes hardware-level debug messages to standardized disk logs.
2. Temporal Sync (Spec 2.4): Assigns synchronized high-resolution timestamps to every log entry.
3. Safety Mapping (Spec 18.3): Translates Pico-reported severities into global Alarm Manager events.
4. Memory Safety (Spec 19.5): Manages the fixed-size GUI buffer to ensure HMI responsiveness.
5. Signal Auditing (Spec 20.2): Auto-tags Link Quality logs for specialized dashboard filtering.
6. Unified Parsing (Spec 4.3): Leverages lib.protocol_parser for consistent data decoding.
7. De-recursion (Rectification B): Uses isolated logging namespaces to prevent GUI log echos.
"""

import collections
import time
import logging
from .rp5_logger import logger  # Spec 9.1: Standardized Host Logger
from .digital_twin import GLOBAL_TWIN
from . import protocol as meowprotocol
from . import machine_states as ms
from . import protocol_parser  # Spec 4.3: Centralized Protocol Translator

# --- LOGGING ISOLATION (Rectification 2.B) ---
# We define a dedicated 'forensic' logger for hardware telemetry.
# By setting propagate to False, these logs don't reach the Root Logger,
# effectively bypassing the GuiLogBridge in app.py and preventing duplicate lines.
forensic_logger = logging.getLogger("forensic")
forensic_logger.propagate = False

# Copy existing handlers (File/Console) from the main logger to the forensic logger
# but EXCLUDE the GuiLogBridge to prevent infinite recursion and buffer floods.
if not forensic_logger.handlers:
    for handler in logger.handlers:
        if "GuiLogBridge" not in str(type(handler)):
            forensic_logger.addHandler(handler)

# --- DEBUG CONFIGURATION ---
try:
    import config.debug as dbg

    DEBUG_ROUTER = getattr(dbg, "DEBUG_GLOBAL", True)
except ImportError:
    DEBUG_ROUTER = True


class TelemetryRouter:
    """
    [Spec 19.0] The Telemetry Router.

    Acts as the primary ingestion point for all asynchronous reports from the
    hardware fleet. It deconstructs MTIP payloads and performs the logical
    handoff to specialized system managers.
    """

    def __init__(self):
        """
        Initializes the router and the memory-safe GUI log buffer.

        [Spec 19.5] Uses a fixed-length deque to implement automatic DOM pruning,
        preventing browser resource exhaustion during high-frequency logging events.
        """
        # Buffer for the GUI "Flight Recorder" panel (Last 250 lines)
        self.gui_log_buffer = collections.deque(maxlen=250)

    def route_packet(self, source_id, msg_type, payload):
        """
        [Spec 19.1] Primary Telemetry Ingestion Path.

        Ingests raw MTIP packets from the transport layer and dispatches the
        contents based on protocol message types.

        Args:
            source_id (int): The ID of the originating Pico node.
            msg_type (int): The MTIP protocol message type identifier.
            payload (bytes): Raw byte data received from the hardware link.
        """
        try:
            # 1. Real-time Telemetry Logs (0x44)
            if msg_type == meowprotocol.MSG_TYPE_LIVE_LOG:
                self._handle_live_log(source_id, payload)

            # 2. Historic/Crash Logs (0x43) - Spec 4.3.11
            elif msg_type == meowprotocol.MSG_TYPE_LOG:
                self._handle_crash_log(source_id, payload)

            # 3. Persistent Events (0x40) - Spec 4.3.6.1
            elif msg_type == meowprotocol.MSG_TYPE_EVT:
                text = payload.decode("utf-8", "ignore")
                # Log to isolated forensic logger
                forensic_logger.info(f"[Pico {source_id}] [EVENT] {text}")
                self.gui_log_buffer.append(
                    {
                        "ts": time.time(),
                        "src": source_id,
                        "lvl": "I",
                        "tag": "EVT",
                        "msg": text,
                    }
                )

            # 4. Safety Alarms (0x48) - High Priority Redundancy
            elif msg_type == meowprotocol.MSG_TYPE_ALARM:
                text = payload.decode("utf-8", "ignore")
                forensic_logger.critical(f"[Pico {source_id}] [SAFETY] {text}")

                # Escalation to Alarm Manager for global interlock enforcement
                if (
                    hasattr(GLOBAL_TWIN, "coordinator")
                    and GLOBAL_TWIN.coordinator.alarms
                ):
                    GLOBAL_TWIN.coordinator.alarms.raise_alarm(
                        f"P{source_id}_ALARM", ms.SEVERITY_CRITICAL, context=text
                    )

                self.gui_log_buffer.append(
                    {
                        "ts": time.time(),
                        "src": source_id,
                        "lvl": "C",
                        "tag": "ALARM",
                        "msg": text,
                    }
                )

            # 5. Version Report (0x42) - Spec 9.3
            elif msg_type == meowprotocol.MSG_TYPE_VRS:
                text = payload.decode("utf-8", "ignore")
                msg = f"Firmware Version: {text}"
                forensic_logger.info(f"[Pico {source_id}] {msg}")

                # Update Twin version metadata
                limb = GLOBAL_TWIN.limbs.get(source_id)
                if limb:
                    limb.firmware_version = text
                    # [FIX] Parse the string into a dictionary so the GUI table can render it!
                    parsed = protocol_parser.parse_kv_payload(text)
                    if parsed:
                        limb.remote_versions = parsed

                self.gui_log_buffer.append(
                    {
                        "ts": time.time(),
                        "src": source_id,
                        "lvl": "I",
                        "tag": "VER",
                        "msg": msg,
                    }
                )

            # --- ADD THIS NEW BLOCK: 6. Status Reports (0x41) ---
            elif msg_type == meowprotocol.MSG_TYPE_STS:
                text = payload.decode("ascii", "ignore")
                parsed = protocol_parser.parse_kv_payload(text)

                limb = GLOBAL_TWIN.limbs.get(source_id)
                if limb:
                    # Sync the hardware reality into the Digital Twin
                    if "ST" in parsed:
                        limb.remote_state = parsed["ST"]
                    if "V" in parsed:
                        limb.voltage = float(parsed["V"])
                    if "VM" in parsed:
                        limb.voltage_min = float(parsed["VM"])
                    if "T" in parsed:
                        limb.temp = float(parsed["T"])
                    if "UPT" in parsed:
                        limb.uptime = int(parsed["UPT"])

                    # --- Resiliency & Diagnostic Metrics ---
                    if "LA" in parsed:
                        limb.loop_avg = int(parsed["LA"])
                    if "LM" in parsed:
                        limb.loop_max = int(parsed["LM"])
                    if "RA" in parsed:
                        limb.resp_avg = int(parsed["RA"])
                    if "RL" in parsed:
                        limb.resp_max = int(parsed["RL"])
                    if "CE" in parsed:
                        limb.crc_errors = int(parsed["CE"])
                    if "IE" in parsed:
                        limb.i2c_errors = int(parsed["IE"])
                    if "CSE" in parsed:
                        limb.chk_errors = int(parsed["CSE"])
                    if "WC" in parsed:
                        limb.write_count = int(parsed["WC"])
                    if "RST" in parsed:
                        limb.reset_cause = str(parsed["RST"])

            # --- 7. Actuator Reports (0x45) ---
            elif msg_type == meowprotocol.MSG_TYPE_ACT:
                text = payload.decode("ascii", "ignore")
                parsed = protocol_parser.parse_kv_payload(text)
                GLOBAL_TWIN.update_actuator_telemetry(source_id, parsed)

            # --- 8. Sensor Reports (0x46) ---
            elif msg_type == meowprotocol.MSG_TYPE_SNS:
                text = payload.decode("ascii", "ignore")
                parsed = protocol_parser.parse_kv_payload(text)
                GLOBAL_TWIN.update_sensor_telemetry(source_id, parsed)

            # --- 9. Config Reports (0x47) ---
            elif msg_type == meowprotocol.MSG_TYPE_CFG:
                text = payload.decode("ascii", "ignore")
                parsed = protocol_parser.parse_kv_payload(text)
                limb = GLOBAL_TWIN.limbs.get(source_id)
                if limb:
                    limb.remote_config = parsed

        except Exception as e:
            # We use the main 'logger' for system-level errors so the GUI bridge
            # still captures internal router failures.
            logger.error(f"[Router] Routing Fail: {e}")

    def _handle_live_log(self, src, payload):
        """
        [Spec 19.2] Live Operational Log Processor.

        Uses the centralized MTIP translator to parse the envelope and body.
        Auto-syncs hardware state with the Digital Twin and maps severities
        to the global Safety Gradient.

        Args:
            src (int): Source identifier of the reporting limb.
            payload (bytes): The raw log bytes (LVL|TAG|MSG).
        """
        try:
            # [Spec 4.3] Unified MTIP Translation
            result = protocol_parser.decode_envelope(payload)
            lvl, tag, msg, data = (
                result["lvl"],
                result["tag"],
                result["msg"],
                result["data"],
            )

            # --- [Spec 20.2] LQI FLAG MONITORING ---
            if any(term in msg.upper() for term in ["LQI", "LINK QUALITY", "SIGNAL"]):
                tag = "LQI"

            # --- [Spec 3.0] Digital Twin Synchronization ---
            if msg.startswith("SENS:"):
                GLOBAL_TWIN.update_sensor_telemetry(src, data)
            elif msg.startswith("ACT:"):
                GLOBAL_TWIN.update_actuator_telemetry(src, data)

            # 1. Standardized Disk Logging (Spec 9.1 Formatting)
            # Use forensic_logger to prevent duplication in GUI buffer via Bridge
            log_msg = f"[Pico {src}] [{tag}] {msg}"
            if lvl == "E":
                forensic_logger.error(log_msg)
            elif lvl == "C":
                forensic_logger.critical(log_msg)
            elif lvl == "W":
                forensic_logger.warning(log_msg)
            elif lvl == "D":
                forensic_logger.debug(log_msg)
            else:
                forensic_logger.info(log_msg)

            # 2. Safety Gradient Mapping (Spec 18.3)
            if hasattr(GLOBAL_TWIN, "coordinator") and GLOBAL_TWIN.coordinator.alarms:
                alarm_code = f"P{src}_{tag}"
                if lvl == "W":
                    GLOBAL_TWIN.coordinator.alarms.raise_alarm(
                        alarm_code, ms.SEVERITY_WARNING, context=msg
                    )
                elif lvl == "E":
                    GLOBAL_TWIN.coordinator.alarms.raise_alarm(
                        alarm_code, ms.SEVERITY_PAUSE, context=msg
                    )
                elif lvl == "C":
                    GLOBAL_TWIN.coordinator.alarms.raise_alarm(
                        alarm_code, ms.SEVERITY_CRITICAL, context=msg
                    )

            # 3. Flight Recorder Injection (Spec 2.4)
            if lvl == "D" and not DEBUG_ROUTER:
                return

            self.gui_log_buffer.append(
                {"ts": time.time(), "src": src, "lvl": lvl, "tag": tag, "msg": msg}
            )

        except Exception as e:
            if DEBUG_ROUTER:
                # Use standard logger for internal parser/logic warnings
                logger.warning(f"[Router] Parse Error from P{src}: {e}")

    def _handle_crash_log(self, src, payload):
        """
        [Spec 4.3.11] Flash-Resident Crash Log Handler.
        """
        try:
            text = payload.decode("utf-8", "ignore").strip()

            if not text:
                msg = "Crash Log Request: No Data (Clean)"
                forensic_logger.info(f"[Pico {src}] {msg}")
                self.gui_log_buffer.append(
                    {
                        "ts": time.time(),
                        "src": src,
                        "lvl": "I",
                        "tag": "LOG",
                        "msg": msg,
                    }
                )
                return

            # Log the bulk dump to disk using isolated forensic namespace
            forensic_logger.info(
                f"[Pico {src}] CRASH LOG DUMP START\n{text}\n[Pico {src}] CRASH LOG DUMP END"
            )

            # Dispatch a summary notification to the human operator
            summary = f"Crash Log Retrieved: {len(text)} bytes saved to disk."
            self.gui_log_buffer.append(
                {
                    "ts": time.time(),
                    "src": src,
                    "lvl": "W",
                    "tag": "LOG",
                    "msg": summary,
                }
            )

        except Exception as e:
            logger.error(f"[Router] Log Dump Fail: {e}")


# [Spec 19.0] Singleton Instance for system-wide telemetry access
STREAM_ROUTER = TelemetryRouter()
