#!/usr/bin/env python3
"""
examples/virtual_loopback_demo.py — Zero-Dependency Ninelives Loopback & Ghost OTA Demo

Runs a complete, self-contained simulation over an in-memory RS-485 loopback bus
with ZERO physical hardware and ZERO external packages required (pure Python stdlib):

  1. Spawns a Virtual RP2350 Limb connected to the Ninelives Host Switchboard.
  2. Exchanges live MTIP telemetry (`ST=IDLE`, Voltage, Temp, Sensors, Actuators)
     and synchronizes the Host Digital Twin.
  3. Fires a persistent hardware edge event (`0x40 EVT`) and proves the W-Protocol
     piggybacked wipe (`|W=seq`).
  4. Simulates a catastrophic Limb crash into Ghost Mode (`ST=GHOST,ERR=NO_APP`).
  5. Runs the chunked SHA-256 OTA recovery protocol (`0x50 OTA_START` -> `0x51 OTA_DATA`
     -> `0x52 OTA_END`) over the virtual bus to resurrect the Limb back into `FLOW` mode.

Usage:
  python3 examples/virtual_loopback_demo.py
"""

import asyncio
import hashlib
import logging
import math
import struct
import sys
from pathlib import Path

# Allow running directly from a repo checkout without installing first
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from ninelives import machine_states as ms
from ninelives import protocol as mtip
from ninelives.digital_twin import GLOBAL_TWIN
from ninelives.switchboard import operator as FleetOperator

log = logging.getLogger("DEMO")


class InMemorySerialLink:
    """
    Zero-dependency async stream pair that emulates a physical UART/RS-485 transceiver
    between the Host Switchboard and a Virtual RP2350 Limb.
    """

    def __init__(self, virtual_limb):
        self.rx_reader = asyncio.StreamReader()
        self.virtual_limb = virtual_limb
        self.virtual_limb.attach_host_reader(self.rx_reader)

    def write(self, data: bytes):
        self.virtual_limb.receive_from_host(data)

    async def drain(self):
        await asyncio.sleep(0)

    def __call__(self):
        return self.rx_reader, self

    def __repr__(self):
        return "virtual://rs485-loopback-0"


class VirtualLimb:
    """
    Simulates an RP2350 Limb node on the other end of the RS-485 bus.
    Implements Dual-Core telemetry, W-Protocol event ledger, and Ghost Mode OTA rescue.
    """

    def __init__(self, limb_id: int = 1):
        self.limb_id = limb_id
        self.parser = mtip.PacketParser(local_id=limb_id)
        self.host_reader = None
        self.tx_seq = 0
        self.state = "IDLE"
        self.uptime = 12
        self.pending_events = {}  # {seq_id: payload_str}
        self.unacked_seqs = []

        # Ghost OTA staging state
        self.ota_expected_hash = ""
        self.ota_buffer = bytearray()
        self.flashed_firmware = b""

    def attach_host_reader(self, reader: asyncio.StreamReader):
        self.host_reader = reader

    def _next_seq(self) -> int:
        self.tx_seq = (self.tx_seq + 1) % 65535
        return self.tx_seq

    def send_frame(self, target_id: int, seq: int, mtype: int, payload):
        pkt = mtip.build_packet(target_id, self.limb_id, seq, mtype, payload)
        if self.host_reader is not None:
            self.host_reader.feed_data(pkt)

    def trigger_edge_event(self, event_text: str):
        """Queues a persistent edge event in the Limb's W-Protocol ledger and transmits it."""
        seq = self._next_seq()
        self.pending_events[seq] = event_text
        self.send_frame(0, seq, mtip.MSG_TYPE_EVT, f"EVT:{event_text}")
        log.info(f"  [Limb {self.limb_id} HW] Triggered Edge Event (Seq={seq}): {event_text}")

    def crash_into_ghost_mode(self):
        """Simulates a fatal app.py failure where boot.py drops into Ghost Mode."""
        self.state = "GHOST"
        log.warning(
            f"  [Limb {self.limb_id} BOOT] !!! 0 LIVES LEFT -> ENTERING GHOST MODE RESCUE KERNEL !!!"
        )

    def receive_from_host(self, raw_bytes: bytes):
        for target, source, seq, mtype, payload in self.parser.parse(raw_bytes):
            self._handle_packet(source, seq, mtype, payload)

    def _handle_packet(self, src: int, seq: int, mtype: int, payload: bytes):
        # 1. Check for piggybacked W-Protocol wipes (`|W=10,12`) on incoming polls/commands
        if mtype in (mtip.MSG_TYPE_CMD, mtip.MSG_TYPE_CMD_STS):
            text = payload.decode("ascii", "ignore")
            if "|W=" in text:
                _, wipe_part = text.split("|W=", 1)
                for tok in wipe_part.split(","):
                    tok = tok.strip()
                    if tok.isdigit():
                        wid = int(tok)
                        if wid in self.pending_events:
                            del self.pending_events[wid]
                            self.unacked_seqs.append(wid)
                            log.info(
                                f"  [Limb {self.limb_id} W-Proto] Wiped event Seq={wid} from Limb ledger"
                            )

        # 2. Ghost Mode Rescue Handler
        if self.state == "GHOST":
            if mtype == mtip.MSG_TYPE_CMD_STS:
                self.send_frame(src, self._next_seq(), mtip.MSG_TYPE_STS, "ST=GHOST,V=4.98,T=31.2,UPT=1")
            elif mtype == mtip.MSG_TYPE_OTA_START:
                fid = payload[0]
                total_chunks = struct.unpack(">H", payload[1:3])[0]
                self.ota_expected_hash = payload[3:].decode("ascii")
                self.ota_buffer.clear()
                log.info(
                    f"  [Limb {self.limb_id} GHOST] OTA_START: FileID=0x{fid:02X}, "
                    f"Chunks={total_chunks}, SHA256={self.ota_expected_hash[:12]}..."
                )
                self.send_frame(src, seq, mtip.MSG_TYPE_ACK, "READY")
            elif mtype == mtip.MSG_TYPE_OTA_DATA:
                self.ota_buffer.extend(payload)
                self.send_frame(src, seq, mtip.MSG_TYPE_ACK, "OK")
            elif mtype == mtip.MSG_TYPE_OTA_END:
                calc_hash = hashlib.sha256(self.ota_buffer).hexdigest()
                if calc_hash == self.ota_expected_hash:
                    self.flashed_firmware = bytes(self.ota_buffer)
                    self.state = "FLOW"
                    log.info(
                        f"  [Limb {self.limb_id} GHOST] SHA-256 Verified ({calc_hash[:12]}...)! "
                        "Atomic swap app.py.new -> app.py (backed up app.py.bak). Rebooting into FLOW!"
                    )
                    self.send_frame(src, seq, mtip.MSG_TYPE_ACK, "REBOOT")
                else:
                    self.send_frame(src, seq, mtip.MSG_TYPE_NAK, "HASH_FAIL")
            return

        # 3. Normal Application Mode Handler
        if mtype == mtip.MSG_TYPE_CMD_STS:
            self.uptime += 1
            ua_suffix = ""
            if self.unacked_seqs:
                ua_suffix = ",UA=" + ",".join(map(str, self.unacked_seqs))
                self.unacked_seqs.clear()
            sts = (
                f"ST={self.state},V=5.02,VM=4.95,T=36.4,UPT={self.uptime},"
                f"LA=120,LM=450,CE=0,IE=0{ua_suffix}"
            )
            self.send_frame(src, self._next_seq(), mtip.MSG_TYPE_STS, sts)
            # Re-broadcast any un-wiped persistent events
            for ev_seq, ev_text in self.pending_events.items():
                self.send_frame(src, ev_seq, mtip.MSG_TYPE_EVT, f"EVT:{ev_text}")

        elif mtype == mtip.MSG_TYPE_CMD_SNS:
            self.send_frame(
                src,
                self._next_seq(),
                mtip.MSG_TYPE_SNS,
                f"SENS:line_rpm=1420,bus_temp=36.4,odometer={self.uptime * 10}",
            )

        elif mtype == mtip.MSG_TYPE_CMD_ACT:
            self.send_frame(
                src,
                self._next_seq(),
                mtip.MSG_TYPE_ACT,
                "ACT:feeder_pwm=75.0,feeder_pwm_ST=VERIFIED",
            )

        elif mtype in (mtip.MSG_TYPE_CMD, mtip.CMD_SYNC_TIME):
            self.send_frame(src, seq, mtip.MSG_TYPE_ACK, str(seq))


class DemoCoordinator:
    """Minimal Host Coordinator that manages W-Protocol wipe sets and RTT tracking."""

    def __init__(self):
        self.pending_wipes = {1: set()}
        self.alarms = None

    def record_ack(self, limb_id: int, seq: int):
        limb = GLOBAL_TWIN.limbs.get(limb_id)
        if limb:
            limb.last_acked_seq = seq


async def perform_async_ota_rescue(ctrl, new_app_bytes: bytes):
    """Streams a new firmware image over the active Switchboard controller using MTIP OTA."""
    chunk_size = 64
    total_chunks = math.ceil(len(new_app_bytes) / chunk_size)
    digest = hashlib.sha256(new_app_bytes).hexdigest()

    # 1. OTA_START (0x50) for File ID 0x02 (app.py)
    start_payload = struct.pack(">BH", 0x02, total_chunks) + digest.encode("ascii")
    seq = await ctrl.send_packet(mtip.MSG_TYPE_OTA_START, start_payload, requires_ack=True)
    while seq in ctrl.pending_acks:
        await asyncio.sleep(0.01)

    # 2. OTA_DATA (0x51) chunks
    for i in range(total_chunks):
        chunk = new_app_bytes[i * chunk_size : (i + 1) * chunk_size]
        seq = await ctrl.send_packet(mtip.MSG_TYPE_OTA_DATA, chunk, requires_ack=True)
        while seq in ctrl.pending_acks:
            await asyncio.sleep(0.01)

    # 3. OTA_END (0x52) atomic commit & reboot
    seq = await ctrl.send_packet(mtip.MSG_TYPE_OTA_END, b"", requires_ack=True)
    while seq in ctrl.pending_acks:
        await asyncio.sleep(0.01)


async def run_demo():
    from ninelives import switchboard as sb

    # Speed up Switchboard polling intervals so the interactive demo completes in ~2 seconds
    sb.HEARTBEAT_FAST = 0.08
    sb.HEARTBEAT_SLOW = 0.15

    vlimb = VirtualLimb(limb_id=1)
    virtual_link = InMemorySerialLink(vlimb)

    log.info(f"=== 1. Creating Virtual RS-485 Bus ({virtual_link}) ===")
    GLOBAL_TWIN.register_limb(1, "DEMO_LIMB")
    logic_queue = asyncio.Queue(maxsize=256)
    warning_queue = asyncio.Queue(maxsize=64)
    coord = DemoCoordinator()

    fleet = FleetOperator({1: virtual_link}, logic_queue, warning_queue, coord)
    await fleet.start()

    async def process_logic_queue():
        while True:
            msg = await logic_queue.get()
            if msg["mtype"] == mtip.MSG_TYPE_EVT:
                log.info(
                    f"[Host Brain] Received Event Seq={msg['seq']} ({msg['payload']}) -> Queueing W-Protocol Wipe"
                )
                coord.pending_wipes[1].add(msg["seq"])
            elif msg["mtype"] == mtip.MSG_TYPE_STS and "UA=" in msg["payload"]:
                coord.pending_wipes[1].clear()

    logic_task = asyncio.create_task(process_logic_queue())

    try:
        log.info("=== 2. Polling Virtual Limb & Syncing Digital Twin ===")
        await asyncio.sleep(0.5)
        twin_limb = GLOBAL_TWIN.limbs[1]
        log.info(
            f"[Digital Twin] State={twin_limb.remote_state} | V={twin_limb.voltage}V | "
            f"T={twin_limb.temp}C | Sensors={ {k: s.raw_value for k, s in twin_limb.sensors.items()} } | "
            f"Actuators={ {k: (a.reported_value, a.verification_state) for k, a in twin_limb.actuators.items()} }"
        )

        log.info("=== 3. Testing W-Protocol Piggybacked Event Ledger ===")
        vlimb.trigger_edge_event("PART_DETECTED:CHUTE_1,WIDTH_US=4120")
        await asyncio.sleep(0.5)
        assert len(vlimb.pending_events) == 0, "Expected W-Protocol to wipe event ledger!"
        log.info("[Verified] Event was delivered once and wiped via piggybacked '|W=<seq>' on heartbeat!")

        log.info("=== 4. Simulating Catastrophic Limb Crash -> Ghost Mode ===")
        vlimb.crash_into_ghost_mode()
        await asyncio.sleep(0.5)
        log.info(f"[Digital Twin] Limb 1 State is now: {twin_limb.remote_state}")
        assert twin_limb.remote_state == ms.STATE_GHOST

        log.info("=== 5. Rescuing Bricked Limb Over RS-485 via OTA Flash ===")
        patched_firmware = (
            b"# Patched Ninelives app.py\ndef main():\n    print('Resurrected via Ghost OTA!')\n"
            * 3
        )
        await perform_async_ota_rescue(fleet.controllers[1], patched_firmware)
        await asyncio.sleep(0.5)

        log.info(
            f"[Digital Twin] Limb 1 State after Ghost OTA Rescue: {twin_limb.remote_state} "
            f"(Flashed {len(vlimb.flashed_firmware)} bytes)"
        )
        assert twin_limb.remote_state == ms.STATE_FLOW
        assert vlimb.flashed_firmware == patched_firmware
        log.info("=== SUCCESS: All Ninelives Protocol, Twin, W-Protocol & Ghost OTA Checks Passed! ===")

    finally:
        fleet.running = False
        logic_task.cancel()


if __name__ == "__main__":
    asyncio.run(run_demo())
