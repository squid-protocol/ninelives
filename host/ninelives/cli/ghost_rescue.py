# utilities/ghost_exit.py (Updated Port Mappings)
# RUN ON: Raspberry Pi 5
# PURPOSE: MTIP v5 Diagnostic Utility to terminate Ghost Mode.

import serial
import time
import struct
import binascii

BAUD_RATE = 115200
MY_ID = 0

# Unified mapping to match network_sweeper.py
FLEET = {
    1: "/dev/ttyAMA0",  # Pico 1 (Loader)
    2: "/dev/ttyAMA2",  # Pico 2 (Gatekeeper)
    3: "/dev/ttyAMA3",  # Pico 3 (Motor Ctrl)
    4: "/dev/ttyACM0",  # Direct USB Cable connection
}

MSG_TYPE_CMD = 0x10
MSG_TYPE_CMD_STS = 0x11
MSG_TYPE_CMD_RST = 0x14


def crc16_ccitt(data_bytes):
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


def build_packet(target_id, source_id, seq_id, msg_type, payload=b""):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    header = struct.pack(">BBHB", target_id, source_id, seq_id, msg_type)
    full_binary = header + payload
    hex_body = binascii.hexlify(full_binary).upper()
    crc_val = crc16_ccitt(hex_body)
    return f"<{hex_body.decode('ascii')}:{crc_val:04X}>\n".encode("ascii")


def parse_response(raw_bytes):
    try:
        txt = raw_bytes.decode("ascii", errors="ignore").strip()
        if "<" not in txt or ">" not in txt:
            return None

        start = txt.find("<")
        end = txt.find(">")
        content = txt[start + 1 : end]

        if ":" not in content:
            return ("NAK", "FMT")
        hex_body, crc_str = content.rsplit(":", 1)

        if crc16_ccitt(hex_body.encode()) != int(crc_str, 16):
            return ("NAK", "CRC")

        binary = binascii.unhexlify(hex_body)
        if len(binary) < 5:
            return ("NAK", "LEN")

        tgt, src, seq, mtype = struct.unpack(">BBHB", binary[:5])
        payload = binary[5:]
        return (tgt, src, seq, mtype, payload)
    except Exception as e:
        return ("NAK", f"EXC_{e}")


def main():
    print("=== Ninelives Hardware Diagnostic ===")
    print("Select physical connection:")
    print("  [1] Pico 1 (RS-485 -> ttyAMA0)")
    print("  [2] Pico 2 (RS-485 -> ttyAMA2)")
    print("  [3] Pico 3 (RS-485 -> ttyAMA3)")
    print("  [4] USB Cable (Direct -> ttyACM0)")

    while True:
        node = input("Enter Connection ID (1-4): ").strip()
        if node in ["1", "2", "3", "4"]:
            selection = int(node)
            serial_port = FLEET[selection]
            # If using USB, the target ID is still likely 1
            current_target = 1 if selection == 4 else selection
            break
        print("Invalid input.")

    print(f"\n--- OPENING {serial_port} @ {BAUD_RATE}bps ---")
    try:
        ser = serial.Serial(serial_port, BAUD_RATE, timeout=0.1)
    except Exception as e:
        print(f"ERROR: Could not open {serial_port}: {e}")
        return

    seq_counter = 0

    try:
        while True:
            print(
                f"\n=== PORT: {serial_port} | ACTIVE TARGET: PICO {current_target} ==="
            )
            print("Select Command:")
            print("  [s] Status Request (Ping)")
            print("  [r] Reset Command  (Hard Reset)")
            print("  [e] EXIT GHOST MODE")
            print("  [q] Quit")

            choice = input("Enter selection: ").strip().lower()

            if choice == "q":
                break
            elif choice == "s":
                msg_type, payload = MSG_TYPE_CMD_STS, b""
            elif choice == "r":
                msg_type, payload = MSG_TYPE_CMD_RST, b""
            elif choice == "e":
                msg_type, payload = MSG_TYPE_CMD, b"EXIT"
            else:
                continue

            ser.reset_input_buffer()
            pkt = build_packet(current_target, MY_ID, seq_counter, msg_type, payload)
            ser.write(pkt)
            print(f"[TX] {pkt.strip().decode()}")

            seq_counter = (seq_counter + 1) % 65536

            start = time.time()
            buffer = b""
            got_reply = False

            while (time.time() - start) < 1.0:
                if ser.in_waiting:
                    buffer += ser.read(ser.in_waiting)
                    if b"\n" in buffer or b">" in buffer:
                        res = parse_response(buffer)
                        if res and res[0] != "NAK":
                            tgt, src, seq, mtype, pay = res
                            try:
                                pay_str = pay.decode("utf-8", "ignore")
                            except:
                                pay_str = pay.hex()
                            print(
                                f"     [RX] SRC={src} TYPE={hex(mtype)} | PAYLOAD={pay_str}"
                            )
                            got_reply = True
                            break
                time.sleep(0.01)

            if not got_reply:
                print("     [TIMEOUT] No valid reply received.")

    except KeyboardInterrupt:
        print("\nExiting.")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
