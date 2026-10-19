#!/usr/bin/env python3
"""
examples/minimal_host.py — Minimal Ninelives Host Controller

Demonstrates how to spin up the Ninelives RS-485 Switchboard and Digital Twin
in ~50 lines of Python to:
  1. Connect to one or more RP2040/RP2350 Limbs over UART/RS-485
  2. Automatically poll Status (0x11), Sensors (0x16), and Actuators (0x15)
  3. Process incoming W-Protocol persistent events (0x40) and piggyback wipes
  4. Read live hardware state from the passive Digital Twin

Usage:
  python3 examples/minimal_host.py --port /dev/ttyAMA0 --limb-id 1
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Allow running directly from a repo checkout without installing first
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))

from ninelives import protocol as mtip
from ninelives.digital_twin import GLOBAL_TWIN
from ninelives.switchboard import operator as FleetOperator


class MinimalCoordinator:
    """Lightweight coordinator tracking RTT and W-Protocol event wipes."""

    def __init__(self, limb_ids):
        self.pending_wipes = {lid: set() for lid in limb_ids}
        self.alarms = None

    def record_ack(self, limb_id, seq):
        limb = GLOBAL_TWIN.limbs.get(limb_id)
        if limb:
            limb.last_acked_seq = seq


async def consume_events(logic_queue: asyncio.Queue, coord: MinimalCoordinator):
    """Consumes incoming packets from the Switchboard and queues W-Protocol wipes."""
    while True:
        item = await logic_queue.get()
        src = item["source"]
        mtype = item["mtype"]
        seq = item["seq"]
        payload = item["payload"]

        # When a Limb reports a persistent edge event (0x40), queue its sequence ID
        # in pending_wipes so the next outbound poll piggybacks `|W=<seq>`.
        if mtype == mtip.MSG_TYPE_EVT:
            logging.info(f"[Limb {src}] Persistent Event (seq={seq}): {payload}")
            coord.pending_wipes.setdefault(src, set()).add(seq)

        # When a Status report (0x41) confirms `UA=<seq>`, clear it from pending_wipes
        elif mtype == mtip.MSG_TYPE_STS and "UA=" not in payload:
            coord.pending_wipes.get(src, set()).clear()


async def main():
    parser = argparse.ArgumentParser(description="Minimal Ninelives RS-485 Host")
    parser.add_argument("--port", default="/dev/ttyAMA0", help="Serial port path")
    parser.add_argument("--limb-id", type=int, default=1, help="Target Limb ID (1-254)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    port_map = {args.limb_id: args.port}
    GLOBAL_TWIN.register_limb(args.limb_id, f"NODE_{args.limb_id}")

    logic_queue = asyncio.Queue(maxsize=256)
    warning_queue = asyncio.Queue(maxsize=64)
    coord = MinimalCoordinator(port_map.keys())

    fleet = FleetOperator(port_map, logic_queue, warning_queue, coord)
    await fleet.start()
    asyncio.create_task(consume_events(logic_queue, coord))

    logging.info(f"Polling Limb {args.limb_id} on {args.port} (Ctrl+C to stop)...")
    while True:
        await asyncio.sleep(2.0)
        limb = GLOBAL_TWIN.limbs[args.limb_id]
        logging.info(
            f"[Twin Node {limb.id}] State={limb.remote_state} | "
            f"V={limb.voltage:.2f}V | T={limb.temp:.1f}C | "
            f"Uptime={limb.uptime}s | Packets={limb.packet_count} | "
            f"Sensors={ {k: s.raw_value for k, s in limb.sensors.items()} }"
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
