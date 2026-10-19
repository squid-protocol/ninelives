# lib/meowprotocol.py - MTIP v4.6 Protocol Implementation (v5.86 Compliance)
# PURPOSE: Packet framing, CRC-16-CCITT validation, and Message Definitions.
# COMPLIANCE: Core 0 Spec Section 4.3 (Hex-Encoded ASCII switchboard)
# CHANGES: Added Network Health Instrumentation (CRC Counting) and Infrastructure Commands.

"""
[Spec 4.3] Ninelives Message Transfer Interface Protocol (MTIP).

This module implements the 'Nervous System' protocol used for communication
between the RP5 Brain and the Pico Fleet. It utilizes a Hex-Encoded ASCII
switchboard layer to ensure that binary payloads do not interfere with
packet framing delimiters.

Key Architectural Roles:
1. Wire Format (Spec 4.3.3): Implements the <HEX_BODY:CRC> framing standard.
2. Integrity (Spec 23.1): Provides CRC-16-CCITT validation for every packet.
3. Resilience (Section 1.3): Implements 'Zombie Mode' recovery for parasitic power scenarios.
4. Message Registry (Spec 4.3.4): Defines the global inventory of command and response types.
"""


# ==============================================================================
# SECTION 1: MESSAGE INVENTORY (Spec 4.3.4)
# ==============================================================================

MSG_TYPE_CMD_STOP = 0x00  # [Spec 15.1] Priority E-STOP: Highest priority on wire.

# --- INFRASTRUCTURE COMMANDS ---
MSG_TYPE_CMD = 0x10  # Generic: "FLOW", "IDLE", "SET:..."
MSG_TYPE_CMD_STS = 0x11  # QST: Heartbeat Query (Poll for industrial metrics)
MSG_TYPE_CMD_VER = 0x12  # QVR: Version Query (Spec 9.3)
MSG_TYPE_CMD_LOG = 0x13  # QLG: Logs Query (Spec 4.3.11)
MSG_TYPE_CMD_RST = 0x14  # RST: System Reset (Warm Boot)
MSG_TYPE_CMD_ACT = 0x15  # QAC: Query Actuators (Spec 4.3.8)
MSG_TYPE_CMD_SNS = 0x16  # QSN: Query Sensors (Spec 4.3.9)
MSG_TYPE_CMD_CFG = 0x17  # QCF: Query Config (Spec 4.3.10)
MSG_TYPE_SET_CFG = 0x18  # SCF: Set Config (Tuning/Calibration)

# --- TIME & SYNC COMMANDS ---
CMD_SYNC_TIME = 0x1A  # [Spec 4.3.13] Payload: Current Epoch Float
CMD_CFG_HASH = 0x1B  # Query: Request CRC32 of current Pico flash config
CMD_CLR_STATS = 0x1C  # Command: Reset local hardware session counters

# --- CONFIRMATION TYPES ---
MSG_TYPE_ACK = 0x20  # [Spec 4.3.6.2] Brain Accountability Acknowledgment
MSG_TYPE_NAK = 0x30  # Logical Rejection

# --- NAK SUBTYPES ---
MSG_TYPE_NAK_SYNTAX = 0x31  # Format/Parser error
MSG_TYPE_NAK_BUSY = 0x32  # Resource contention (e.g., Flash writing)
MSG_TYPE_NAK_STATE = 0x33  # Command invalid for current Pico state
MSG_TYPE_NAK_RANGE = 0x34  # Param value out of hardware bounds
MSG_TYPE_NAK_AUTH = 0x35  # Security/SourceID violation

# --- EVENT TYPES ---
MSG_TYPE_EVT = 0x40  # [Spec 4.3.6.1] Persistent Async Events (Part Detected)

# --- RESPONSE TYPES ---
MSG_TYPE_STS = 0x41  # Status Report: Mandatory industrial metrics (V, T, UPT)
MSG_TYPE_VRS = 0x42  # Version Report: Firmware hash/branch metadata
MSG_TYPE_LOG = 0x43  # Log File Content: Transmitted during forensic retrieval
MSG_TYPE_LIVE_LOG = 0x44  # Live Log Stream: Real-time debug channel
MSG_TYPE_ACT = 0x45  # Actuator Report: Current verification badges (ON/OFF/STALL)
MSG_TYPE_SNS = 0x46  # Sensor Report: Raw physical telemetry
MSG_TYPE_CFG = 0x47  # Config Report: Returns active flash parameters
MSG_TYPE_ALARM = 0x48  # [Spec 15.5] CRITICAL: Safety Channel (Bypasses normal queues)

# --- OTA TYPES (0x5X) ---
# [Spec 4.3.14] Over-the-Air Update Protocol
MSG_TYPE_OTA_START = 0x50
MSG_TYPE_OTA_DATA = 0x51
MSG_TYPE_OTA_END = 0x52
MSG_TYPE_OTA_ABORT = 0x53

# ==============================================================================
# SECTION 2: SENSOR ERROR CODES (Spec 4.3.9.1)
# ==============================================================================

SENS_ERR_OK = 0  # Successful read and processing
SENS_ERR_IO = -1  # Broken Wire (I2C/PIO peripheral error)
SENS_ERR_BUG = -2  # General Exception in driver code
SENS_ERR_STALE = -3  # Hardware warming up; ignore for 500ms
SENS_ERR_MISSING = -4  # [Spec 4.3.9.2] Ghost Driver: Dependency file not found
SENS_ERR_BUS_LOCK = -5  # I2C Traffic Jam: Recovery required
SENS_ERR_POISONED = -6  # Bad config: Pins or parameters invalid
SENS_ERR_LIMIT = -7  # Saturated/Blind: Sensor exceeding physical range
SENS_ERR_ZOMBIE = -8  # Silicon Lock: Data identical for 100 polls

# ==============================================================================
# SECTION 3: CORE UTILITIES
# ==============================================================================


def crc16_ccitt(data: bytes) -> int:
    """
    [Spec 23.1] High-performance CRC-16-CCITT implementation.

    Standard: 0xFFFF initialization, 0x1021 polynomial.
    Used to verify integrity of Hex-Encoded bodies before parsing.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def build_packet(target_id, source_id, seq, msg_type, payload):
    """
    [Spec 4.3.3] Constructs an MTIP wire-ready packet.

    Format: <HEX_BODY:CRC>
    1. Packs header (Target, Source, Seq_High, Seq_Low, Type) into binary.
    2. Appends payload.
    3. Converts body to Uppercase Hex.
    4. Appends CRC-16 of the Hex string.
    """
    # Header: 5 bytes (Big Endian sequence splitting)
    seq_high = (seq >> 8) & 0xFF
    seq_low = seq & 0xFF

    header = bytes([target_id, source_id, seq_high, seq_low, msg_type])

    if isinstance(payload, str):
        payload_bytes = payload.encode("ascii")
    else:
        payload_bytes = payload

    body = header + payload_bytes

    # Calculate CRC on the HEX STRING of the body per Protocol Definition
    body_hex = body.hex().upper().encode("ascii")
    crc = crc16_ccitt(body_hex)
    crc_hex = f"{crc:04X}".encode("ascii")

    return b"<" + body_hex + b":" + crc_hex + b">\n"


class PacketParser:
    """
    [Spec 4.3.1] Robust Delimiter-Based Parser.

    Designed for unreliable industrial serial links.
    Features:
    - Fragmentation Support: Handles packets split across multiple UART reads.
    - Noise Harvesting (Spec 23.1): Tracks CRC failures as a diagnostic metric.
    - Zombie Recovery: Accepts '?' as a start bit if bit-shifts occur during low-power.
    """

    def __init__(self, local_id):
        """
        Initializes the parser buffer and local addressing.

        :param local_id: The ID of the current node (Brain=0, Limbs=1-3).
        """
        self.local_id = local_id
        self.buffer = b""
        self.crc_error_count = 0  # Metric for Physical Noise/EMI

    def parse(self, chunk):
        """
        Ingests a raw data chunk and extracts valid MTIP packets.

        Implements 'Greedy Ingestion' (Spec 4.3.1.1.A). Scans the buffer
        for valid delimiters, validates CRC, and filters packets by target ID.

        :return: List of valid tuples (Target, Source, Seq, MType, Payload).
        """
        if not chunk:
            return []

        self.buffer += chunk
        packets = []

        # Search for full frames wrapped in delimiters
        # Support both normal '<' and zombie '?' start markers
        while True:
            start_idx_normal = self.buffer.find(b"<")
            start_idx_zombie = self.buffer.find(b"?")

            # Determine which start delimiter comes first
            start_idx = -1
            if start_idx_normal != -1 and start_idx_zombie != -1:
                start_idx = min(start_idx_normal, start_idx_zombie)
            elif start_idx_normal != -1:
                start_idx = start_idx_normal
            elif start_idx_zombie != -1:
                start_idx = start_idx_zombie

            if start_idx == -1:
                break

            # Discard garbage before the start delimiter
            if start_idx > 0:
                self.buffer = self.buffer[start_idx:]
                start_idx = 0

            end_idx = self.buffer.find(b">", start_idx)
            if end_idx == -1:
                # --- POISONED BUFFER TRAP FIX ---
                # MTIP packets should not exceed 512 bytes. If we have scanned
                # past 512 bytes from the start character without seeing '>',
                # the start character was electrical noise. Drop the false start!
                if len(self.buffer) - start_idx > 512:
                    self.buffer = self.buffer[start_idx + 1 :]
                    continue
                break

            # If a newer start delimiter ('<' or '?') appears before '>', the earlier
            # start delimiter was electrical noise—advance to the newer start delimiter.
            next_normal = self.buffer.find(b"<", start_idx + 1, end_idx)
            next_zombie = self.buffer.find(b"?", start_idx + 1, end_idx)
            next_start = -1
            if next_normal != -1 and next_zombie != -1:
                next_start = min(next_normal, next_zombie)
            elif next_normal != -1:
                next_start = next_normal
            elif next_zombie != -1:
                next_start = next_zombie

            if next_start != -1:
                self.buffer = self.buffer[next_start:]
                continue

            frame_content = self.buffer[start_idx + 1 : end_idx]
            self.buffer = self.buffer[end_idx + 1 :]

            if b":" not in frame_content:
                continue

            try:
                body_hex, crc_hex = frame_content.rsplit(b":", 1)
                calc_crc = crc16_ccitt(body_hex)

                # Checksum validation
                if f"{calc_crc:04X}".encode("ascii") != crc_hex:
                    self.crc_error_count += 1
                    continue

                # Decode Hex Body to Binary
                body = bytes.fromhex(body_hex.decode("ascii"))

                # Minimum length check (Header + empty payload = 5 bytes)
                if len(body) < 5:
                    continue

                target = body[0]
                source = body[1]

                # Reconstruct 16-bit SeqID from bytes 2 and 3 (Big Endian)
                seq = (body[2] << 8) | body[3]

                mtype = body[4]
                payload = body[5:]

                # Address filtering: Only accept broadcast (0) or packets meant for us
                if target == self.local_id or target == 0:
                    packets.append((target, source, seq, mtype, payload))

            except Exception:
                continue

        # Safety Flush (Buffer Overflow Protection)
        if len(self.buffer) > 4096:
            self.buffer = b""

        return packets
