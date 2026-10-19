#!/usr/bin/env python3
"""
Ninelives Mass OTA Flasher - Enterprise Edition
Protocol: MTIP v5.5
Description: Reliable, chunked binary transport over RS-485 for RP2350 limbs.
"""

import serial
import time
import struct
import binascii
import hashlib
import math
import os
import sys
import argparse
import logging

# --- SYSTEM DEFAULTS ---
MY_ID = 0
BAUD_RATE = 115200
CHUNK_SIZE = 64
MAX_RETRIES = 5
TIMEOUT_SEC = 2.0

# --- TARGET LIST ---
TARGETS = {
    1: {"port": "/dev/ttyAMA0", "name": "Distributor"},
    2: {"port": "/dev/ttyAMA2", "name": "Sensor Array"},
    3: {"port": "/dev/ttyAMA3", "name": "Motor Controller"},
}

# --- FILE ID MAP ---
FILE_ID_MAP = {
    "config.json": 0x01,
    "app.py": 0x02,
    "boot.py": 0x05,
    "lib/meowprotocol.py": 0x03,
    "lib/tsl2591.py": 0x04,
    "lib/actuators.py": 0x06,
    "lib/sensors.py": 0x07,
    "lib/diagnostics.py": 0x08,
    "lib/tester.py": 0x09,
    "lib/bldc_driver.py": 0x0A,
    "lib/logging.py": 0x0B,
    "lib/mpu6050.py": 0x0C,
    "lib/ota.py": 0x0D,
    "lib/pio_programs.py": 0x0E,
    "lib/vibration_driver.py": 0x0F,
    "lives.txt": 0x10,
    "boot_attempts.txt": 0x11,
}

# --- PROTOCOL CONSTANTS ---
MSG_TYPE_ACK = 0x20
MSG_TYPE_NAK = 0x30
MSG_TYPE_OTA_START = 0x50
MSG_TYPE_OTA_DATA = 0x51
MSG_TYPE_OTA_END = 0x52

# Configure Global Logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("OTA")


class OTASession:
    """Encapsulates a single OTA transfer session to a target device."""

    def __init__(self, target_id, port, file_path):
        self.target_id = target_id
        self.port = port
        self.file_path = file_path
        self.target_name = TARGETS.get(target_id, {}).get("name", "UNKNOWN")
        self.ser = None

    def _crc16_ccitt(self, data_bytes):
        crc = 0xFFFF
        for byte in data_bytes:
            crc ^= byte << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = (crc << 1) ^ 0x1021
                else:
                    crc = crc << 1
                crc &= 0xFFFF
        return crc

    def _build_packet(self, seq, mtype, payload):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")

        header = struct.pack(">BBHB", self.target_id, MY_ID, seq, mtype)
        full = header + payload

        hex_body = binascii.hexlify(full).upper()
        crc_val = self._crc16_ccitt(hex_body)

        return f"<{hex_body.decode()}:{crc_val:04X}>\n".encode()

    def _parse_packet(self, line):
        try:
            clean = line.strip(b"\x3f").strip().decode(errors="ignore").strip("<>")
            if ":" not in clean:
                return None
            hex_body, crc_str = clean.rsplit(":", 1)

            if self._crc16_ccitt(hex_body.encode()) != int(crc_str, 16):
                return "CRC_FAIL"

            binary = binascii.unhexlify(hex_body)
            tgt, src, seq, mtype = struct.unpack(">BBHB", binary[:5])
            payload = binary[5:]
            return (tgt, src, seq, mtype, payload)
        except Exception:
            return None

    def _wait_for_ack(self, expected_seq):
        start = time.time()
        while (time.time() - start) < TIMEOUT_SEC:
            if self.ser.in_waiting:
                line = self.ser.readline()
                if b"<" in line:
                    res = self._parse_packet(line)
                    if isinstance(res, tuple):
                        tgt, src, seq, mtype, pay = res
                        if seq == expected_seq:
                            if mtype == MSG_TYPE_ACK:
                                return True, pay.decode(errors="ignore")
                            elif 0x30 <= mtype <= 0x3F:
                                # Catch 0x30 NAK as well as 0x31-0x35 NAK subtypes
                                return False, pay.decode(errors="ignore")
            time.sleep(0.005)
        return False, "TIMEOUT"

    def _send_reliable(self, pkt, seq, label="TX", verbose=False):
        for attempt in range(MAX_RETRIES):
            self.ser.write(pkt)
            success, msg = self._wait_for_ack(seq)

            if success:
                return True

            if verbose or attempt == MAX_RETRIES - 1:
                logger.warning(f"[{label}] Retry {attempt + 1}/{MAX_RETRIES} ({msg})")

        logger.error(
            f"[{label}] FATAL: Packet sequence {seq} failed after {MAX_RETRIES} retries."
        )
        return False

    def _print_progress(self, iteration, total, length=40):
        percent = ("{0:.1f}").format(100 * (iteration / float(total)))
        filled_length = int(length * iteration // total)
        bar = "█" * filled_length + "-" * (length - filled_length)
        sys.stdout.write(f"\r[TX] |{bar}| {percent}% (Chunk {iteration}/{total})")
        sys.stdout.flush()
        if iteration == total:
            sys.stdout.write("\n")

    def _resolve_file_id(self):
        filename = os.path.basename(self.file_path)
        if filename in FILE_ID_MAP:
            return FILE_ID_MAP[filename]
        for key, val in FILE_ID_MAP.items():
            if key.endswith(f"/{filename}"):
                return val
        return filename

    def execute(self):
        """Executes the full OTA lifecycle."""
        logger.info(
            f"Targeting Node {self.target_id} ({self.target_name}) on {self.port}"
        )

        try:
            with open(self.file_path, "rb") as f:
                file_data = f.read()
        except OSError as e:
            logger.error(f"Failed to read source file: {e}")
            return False

        file_id = self._resolve_file_id()
        file_hash = hashlib.sha256(file_data).hexdigest()
        total_chunks = math.ceil(len(file_data) / CHUNK_SIZE)

        logger.info(
            f"Payload ID: {file_id} | Size: {len(file_data)} bytes | Chunks: {total_chunks}"
        )

        try:
            self.ser = serial.Serial(self.port, BAUD_RATE, timeout=0.1)
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except serial.SerialException as e:
            logger.error(f"Serial port failure: {e}")
            return False

        seq = 1
        start_time = time.time()
        max_file_retries = 3
        file_attempts = 0
        success = False

        while file_attempts < max_file_retries:
            file_attempts += 1
            try:
                # 1. HANDSHAKE
                logger.info(
                    f"Phase 1: Requesting OTA Lock (Attempt {file_attempts}/{max_file_retries})"
                )
                if isinstance(file_id, int):
                    payload = struct.pack(
                        ">BH", file_id, total_chunks
                    ) + file_hash.encode("utf-8")
                else:
                    encoded_name = file_id.encode("utf-8")
                    payload = (
                        struct.pack(">BB", 0xFF, len(encoded_name))
                        + encoded_name
                        + struct.pack(">H", total_chunks)
                        + file_hash.encode("utf-8")
                    )

                start_pkt = self._build_packet(seq, MSG_TYPE_OTA_START, payload)
                if not self._send_reliable(
                    start_pkt, seq, label="OTA_START", verbose=True
                ):
                    logger.warning(
                        "Phase 1 Handshake failed. Triggering Autonomous Recovery."
                    )
                    continue  # Skips the rest of this attempt and loops back to try again

                # 2. TRANSPORT
                logger.info("Phase 2: Streaming Data Chunks")
                seq = (seq + 1) % 256 or 1

                transport_failed = False
                for i in range(total_chunks):
                    chunk = file_data[i * CHUNK_SIZE : (i + 1) * CHUNK_SIZE]
                    pkt = self._build_packet(seq, MSG_TYPE_OTA_DATA, chunk)

                    if not self._send_reliable(pkt, seq, label=f"CHK_{i + 1}"):
                        transport_failed = True
                        break  # Break out of the chunk loop

                    self._print_progress(i + 1, total_chunks)
                    seq = (seq + 1) % 256 or 1

                if transport_failed:
                    logger.warning(
                        "Phase 2 Transport failed. Triggering Autonomous Recovery."
                    )
                    # Send an explicit abort to clear the Pico's state before looping back
                    abort_pkt = self._build_packet(
                        seq + 1, MSG_TYPE_OTA_ABORT, b"ABORT"
                    )
                    self.ser.write(abort_pkt)
                    time.sleep(1.0)
                    continue

                # 3. COMMIT
                logger.info("Phase 3: Finalizing and Verifying Hash (OTA_END)")
                end_pkt = self._build_packet(seq, MSG_TYPE_OTA_END, b"COMMIT")

                for attempt in range(MAX_RETRIES):
                    self.ser.write(end_pkt)
                    ack_ok, ack_msg = self._wait_for_ack(seq)

                    if ack_ok:
                        success = True
                        break

                    logger.warning(f"[OTA_END] Commit failed: {ack_msg}")

                    # --- SMART NAK PARSING ---
                    if (
                        "NAK_STATE_ZERO_BYTE" in ack_msg
                        or "NAK_STATE_CORRUPT" in ack_msg
                        or "HASH_FAIL" in ack_msg
                    ):
                        logger.error(
                            f"Post-Flight Verification Failed ({ack_msg}). Triggering Autonomous Recovery."
                        )
                        abort_pkt = self._build_packet(
                            seq + 1, MSG_TYPE_OTA_ABORT, b"ABORT"
                        )
                        self.ser.write(abort_pkt)
                        time.sleep(1.0)
                        break

                if success:
                    elapsed = time.time() - start_time
                    logger.info(
                        f"SUCCESS: Flash completed in {elapsed:.2f}s ({len(file_data) / elapsed:.0f} bytes/sec)"
                    )
                    return True

            finally:
                # Only close the port if we are completely done, not during a recovery loop
                if file_attempts >= max_file_retries or success:
                    if self.ser and self.ser.is_open:
                        self.ser.close()

        logger.error(
            "FATAL: Maximum autonomous recoveries exceeded. File deployment failed."
        )
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Ninelives Mass OTA Flasher - Enterprise Edition"
    )
    parser.add_argument(
        "-t", "--targets", type=str, help="Target IDs (e.g., 1, 2, or A for All)"
    )
    parser.add_argument("-f", "--files", nargs="+", help="List of file paths to flash")
    parser.add_argument(
        "-b",
        "--baud",
        type=int,
        default=115200,
        help="Target baud rate (default: 115200)",
    )
    parser.add_argument(
        "--debug", action="store_true", help="Enable verbose debug logging"
    )

    args = parser.parse_args()

    # Dynamically override the global BAUD_RATE
    global BAUD_RATE
    BAUD_RATE = args.baud

    if args.debug:
        logger.setLevel(logging.DEBUG)

    # Required Args Check
    if not args.targets or not args.files:
        logger.error("Missing required arguments. Use -h for help.")
        sys.exit(1)

    # Parse Targets
    if args.targets.upper() in ["A", "ALL"]:
        selected_targets = list(TARGETS.keys())
    else:
        try:
            selected_targets = [int(x.strip()) for x in args.targets.split(",")]
        except ValueError:
            logger.error("Invalid target format. Use comma-separated integers or 'A'.")
            sys.exit(1)

    # Validate Files First
    valid_files = []
    for path in args.files:
        if not os.path.exists(path):
            logger.error(f"File not found: {path}. Aborting.")
            sys.exit(1)
        valid_files.append(path)

    # Execute Deployment
    logger.info(f"Initializing batch deployment to targets: {selected_targets}")

    for path in valid_files:
        for tid in selected_targets:
            if tid not in TARGETS:
                logger.warning(f"Skipping unknown target ID: {tid}")
                continue

            print(f"\n{'-' * 60}")
            session = OTASession(tid, TARGETS[tid]["port"], path)
            success = session.execute()

            if not success:
                logger.error("Batch job terminated early due to failure.")
                sys.exit(1)

            time.sleep(0.5)  # Settle bus between nodes

    print(f"\n{'-' * 60}")
    logger.info("Batch Deployment Complete.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n")
        logger.error("Process interrupted by user (Ctrl+C). Aborting.")
        sys.exit(130)
