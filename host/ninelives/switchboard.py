# lib/switchboard.py - Ninelives Core 0 Nervous System (v6.4.1)
# ROLE: High-Availability RS-485 Fleet Commander & MTIP Transport Layer.
# PURPOSE: Manages the physical UART links, packet framing, and reliability.
# COMPLIANCE:
#   - Spec 4.2 (Limb Touch/Watchdog)
#   - Spec 4.3.6.1 (Safety Channel Preemption)
#   - Spec 4.3.6.3 (W-Protocol / Piggyback Wiping)
#   - Spec 4.6 (Reliability & Retries)
#   - Spec 20.2 (Bayesian Link Quality Indicator)
#   - Spec 23 (CRC Noise Harvesting)
#   - Rectification 4.3 (Centralized Parser Integration)

"""
[Spec 4.0] Ninelives Nervous System (The Transport Layer).

The Switchboard serves as the physical interface between the RP5 Brain and the
Pico fleet. It handles the low-level complexities of asynchronous serial
communication, MTIP packet framing, and transport-layer reliability.

Key Architectural Roles:
1. Physical Fleet Commander (Spec 4.1): Manages the lifecycle of UART connections.
2. Greedy Ingestion (Spec 4.3.1): High-efficiency buffer draining to prevent spikes.
3. Command Accountability (Spec 4.6): Retries and sequence tracking for Action packets.
4. Noise Harvesting (Spec 23.0): Converts CRC failures into diagnostic metrics.
5. W-Protocol Delivery (Spec 4.3.6.3): Delivery vehicle for persistent event wipes.
6. Unified Parsing (Rectification 4.3): Offloads string decoding to lib.protocol_parser.
"""

import asyncio
import time
import logging
import traceback

try:
    import serial_asyncio
except ImportError:
    serial_asyncio = None

from . import protocol as meowprotocol
from .digital_twin import GLOBAL_TWIN
from . import machine_states as ms

# ==============================================================================
# SECTION 1: TRANSPORT CONFIGURATION & TUNING
# ==============================================================================

# --- DEBUG CONFIGURATION ---
try:
    import config.debug as dbg

    DEBUG_TRANSPORT = getattr(dbg, "DEBUG_TRANSPORT", True)
    DEBUG_PACKETS = getattr(dbg, "DEBUG_PACKETS", False)
except ImportError:
    DEBUG_TRANSPORT = True
    DEBUG_PACKETS = False

# Setup Named Logger for the Fleet Operator
logger = logging.getLogger("Operator")

# --- RELIABILITY CONSTANTS (Section 4.6) ---
HEARTBEAT_FAST = 0.5  # 2Hz Polling during active operation (ms.STATE_FLOW)
HEARTBEAT_SLOW = 2.0  # 0.5Hz Polling during standby to reduce log noise
LQI_BURN_IN_THRESHOLD = 50  # Ignore packet loss math for the first 50 packets
LQI_PRIOR_SAMPLES = 200  # Bayesian "Virtual" successes to stabilize early LQI math

# --- BREADBOARD / PROTOTYPE TUNING (v6.4) ---
ACK_TIMEOUT = 1.5  # Seconds to wait for 0x20 ACK before retrying
MAX_RETRIES = 3  # Maximum re-broadcast attempts for Action commands
INTER_PACKET_DELAY = 0.05  # 50ms safety gap to prevent RS-485 bus collisions

# ==============================================================================
# SECTION 2: PICO CONTROLLER (The Limb Handler)
# ==============================================================================


class PicoController:
    """
    [Spec 4.1] Individual Limb Controller.

    Responsible for the lifecycle of a single serial connection to a Pico node.
    It manages the transition from physical bytes to logical MTIP frames and
    maintains the accountability registry for that node.
    """

    def __init__(self, port, baud_rate, device_id, logic_queue, coordinator):
        """
        Initializes the controller for a specific Pico ID.

        Args:
            port (str): Path to UART device (e.g. /dev/ttyAMA0).
            baud_rate (int): Typically 115200 for RS-485 links.
            device_id (int): The Pico ID (1-3) matching the Digital Twin.
            logic_queue (asyncio.Queue): Queue for passing events to LogicEngine.
            coordinator (SystemCoordinator): Reference to the Hub.
        """
        self.port = port
        self.baud = baud_rate
        self.id = device_id
        self.coord = coordinator

        self.model = GLOBAL_TWIN.limbs.get(device_id)
        self.logic_queue = logic_queue

        self.writer = None
        self.reader = None
        self.connected = False

        # Timing & Sequence History
        self.last_rx_time = 0
        self.last_tx_time = 0
        self.last_rx_seq = -1

        # [Spec 4.3.5] Sequence Tracking for packet loss detection.
        self.tx_seq_counter = 0

        # Polling state for the 3-phase heartbeat
        self.poll_index = 0

        # [Spec 4.3.6.2] Command Accountability Registry
        self.pending_acks = {}  # {seq: {ts, retries, pkt, type}}

        # Protocol Parser (local_id=0 represents the RP5 Brain)
        self.parser = meowprotocol.PacketParser(local_id=0)

        # --- ASYNCIO LOCK FIX ---
        # Prevents concurrent tasks from mangling the RS485 transmission gap
        self.tx_lock = asyncio.Lock()

        logger.debug(f"[Pico {self.id}] Controller Initialized on {port}")

    async def connect(self):
        """
        [Spec 4.1.1] Attempts to open the asynchronous serial connection.

        Utilizes serial_asyncio to provide non-blocking IO (or a custom stream
        factory for virtual loopbacks/tests). If the link fails to open, the
        Digital Twin state for this limb is forced to OFFLINE.
        """
        try:
            if callable(self.port):
                res = self.port()
                if asyncio.iscoroutine(res):
                    self.reader, self.writer = await res
                else:
                    self.reader, self.writer = res
            elif self.reader is not None and self.writer is not None:
                pass
            else:
                if serial_asyncio is None:
                    raise RuntimeError(
                        "pyserial-asyncio is not installed (pip install pyserial-asyncio)"
                    )
                self.reader, self.writer = await serial_asyncio.open_serial_connection(
                    url=self.port, baudrate=self.baud
                )
            self.connected = True
            logger.info(f"[Pico {self.id}] Physical Link Established: {self.port}")
            return True
        except Exception as e:
            self.connected = False
            if self.model:
                self.model.remote_state = ms.STATE_OFFLINE
            if DEBUG_TRANSPORT:
                logger.error(f"[Pico {self.id}] Connection Failed: {e}")
            return False

    async def send_packet(self, msg_type, payload, requires_ack=False):
        """
        [Spec 4.3.6.3] Transmits a packet with integrated W-Protocol injection.

        This method acts as the 'Delivery Vehicle' for the W-Protocol. It scans
        the Coordinator's wipe registry and appends pending sequence IDs to the
        outgoing payload.

        Args:
            msg_type (int): MTIP message type code.
            payload (str|bytes): Raw message body.
            requires_ack (bool): Whether to track for transport-layer ACK.

        Returns:
            int|None: Allocated Sequence ID (Integer) or None if blocked.
        """
        if not self.connected:
            return None

        # Safety Interlock (Spec 4.5): Prevent operational noise from a failed node
        if self.model and self.model.remote_state == ms.STATE_ERROR:
            # [FIX] Only block operational commands. We MUST allow STS/SNS polling!
            if msg_type in [meowprotocol.MSG_TYPE_CMD, meowprotocol.MSG_TYPE_SET_CFG]:
                return None

        try:
            # --- ASYNCIO LOCK FIX ---
            # Hold the lock across gap calculation, sequence assignment, and UART drain
            async with self.tx_lock:
                now = time.time()
                # Physical Backpressure safety gap
                elapsed = now - self.last_tx_time
                if elapsed < INTER_PACKET_DELAY:
                    await asyncio.sleep(INTER_PACKET_DELAY - elapsed)
                    now = time.time()  # Update 'now' after waking up from sleep

                # --- W-PROTOCOL INJECTION (Spec 4.3.6.3.B) ---
                w_payload = payload
                pending_wipes = self.coord.pending_wipes.get(self.id, set())

                if pending_wipes and msg_type in [
                    meowprotocol.MSG_TYPE_CMD,
                    meowprotocol.MSG_TYPE_CMD_STS,
                ]:
                    # Cap injections to prevent packet fragmentation
                    to_wipe = list(pending_wipes)[:5]
                    wipe_str = ",".join(map(str, to_wipe))

                    if isinstance(payload, bytes):
                        p_str = payload.decode("ascii", "ignore")
                        w_payload = f"{p_str}|W={wipe_str}".encode("ascii")
                    else:
                        w_payload = f"{payload}|W={wipe_str}"

                # Sequence Assignment
                self.tx_seq_counter = (self.tx_seq_counter + 1) % 65535
                seq = self.tx_seq_counter

                # Assembly and Dispatch
                packet = meowprotocol.build_packet(self.id, 0, seq, msg_type, w_payload)
                self.last_tx_time = time.time()

                # [Spec 4.3.6.2] Accountability Registry Entry
                # Register BEFORE writer.write() so ultra-fast or in-memory loopbacks
                # that deliver the ACK during drain() find `seq` in `pending_acks`.
                if requires_ack:
                    self.pending_acks[seq] = {
                        "ts": self.last_tx_time,
                        "retries": 0,
                        "pkt": packet,
                        "type": msg_type,
                    }

                self.writer.write(packet)
                await self.writer.drain()
                self.last_tx_time = time.time()

                if DEBUG_PACKETS:
                    logger.debug(f"[TX P{self.id}] MType={hex(msg_type)} Seq={seq}")

                return seq

        except Exception as e:
            logger.error(f"[Pico {self.id}] Transmission Crash: {e}")
            self.connected = False
            return None

    async def read_loop(self):
        """
        [Spec 4.3.1.1] Infinite RX Consumer Loop.

        Implements 'Greedy Ingestion': drains the entire hardware buffer in
        one pass to maximize throughput. Performs 'Noise Harvesting' by
        reporting CRC failures to the Digital Twin.
        """
        while True:
            if not self.connected:
                await asyncio.sleep(1.0)
                await self.connect()
                continue

            try:
                # 4KB buffer drain per pass
                data = await self.reader.read(4096)
                if data:
                    packets = self.parser.parse(data)

                    # [Spec 23.0] Noise Harvesting
                    if self.parser.crc_error_count > 0:
                        if self.model:
                            self.model.host_crc_errors += self.parser.crc_error_count
                        self.parser.crc_error_count = 0

                    # [Spec 4.3.6.1] Priority Sorting: Safety Alarms jump the processing queue
                    safety_batch = []
                    normal_batch = []

                    for pkt in packets:
                        mtype = pkt[3]
                        if mtype in [
                            meowprotocol.MSG_TYPE_ALARM,
                            meowprotocol.MSG_TYPE_CMD_STOP,
                        ]:
                            safety_batch.append(pkt)
                        else:
                            normal_batch.append(pkt)

                    for pkt in safety_batch:
                        await self._handle_packet(pkt)
                    for pkt in normal_batch:
                        await self._handle_packet(pkt)

            except Exception as e:
                logger.error(f"[Pico {self.id}] RX Error: {e}")
                self.connected = False
                await asyncio.sleep(1.0)

    async def _handle_packet(self, pkt):
        """
        [Spec 4.3.6] The Nervous System Logic Dispatcher.

        Performs transport-level bookkeeping (Sequence validation, duplicate
        detection, RTT calculation) before forwarding data to consumers.
        """
        target, source, seq, mtype, payload = pkt

        # Only decode to string if it is a known text-based message type
        # Bypass decoding for binary payloads (like MSG_TYPE_OTA_DATA)
        payload_str = ""
        if mtype < 0x50:
            payload_str = payload.decode("ascii", "ignore")

        # [Spec 4.2] Watchdog Touch
        if self.model:
            self.model.touch()

        # --- INTERNAL TRANSPORT ACKS ---
        # Note: ACK packets echo the Host's outbound TX sequence ID in their header
        # or payload, which is an independent sequence space from the Limb's own
        # monotonic TX sequence counter (`last_rx_seq`). Handle ACKs before
        # updating `last_rx_seq` so echoed Host sequence IDs don't cause false
        # DATA GAP warnings or get dropped as duplicate Limb sequence numbers.
        if mtype == meowprotocol.MSG_TYPE_ACK:
            try:
                if "|W=" in payload_str:
                    ack_seq = int(payload_str.split("|W=")[0])
                elif payload_str.isdigit():
                    ack_seq = int(payload_str)
                else:
                    ack_seq = seq

                if ack_seq in self.pending_acks:
                    # [Spec 20.1] Capture exact RTT for Link Quality math
                    self.coord.record_ack(self.id, ack_seq)
                    del self.pending_acks[ack_seq]
            except Exception:
                pass
            return

        # --- SEQUENCE VALIDATION & DEDUPLICATION ---
        is_duplicate = self.last_rx_seq != -1 and seq == self.last_rx_seq

        if not is_duplicate and self.last_rx_seq != -1:
            expected = (self.last_rx_seq + 1) % 65535
            if seq != expected:
                # Silently accept sequence resets caused by hardware reboots
                if seq < 5 and expected > 10:
                    self.last_rx_seq = seq
                else:
                    if self.model:
                        self.model.host_seq_skips += 1
                    logger.warning(
                        f"[Pico {self.id}] DATA GAP: Exp {expected} Got {seq}"
                    )

        if not is_duplicate:
            self.last_rx_seq = seq
            if self.model:
                self.model.packet_count += 1

        # Forward to LogicEngine for W-Protocol verification
        try:
            self.logic_queue.put_nowait(
                {"source": self.id, "mtype": mtype, "seq": seq, "payload": payload_str}
            )
        except asyncio.QueueFull:
            # Physical backpressure: Drop telemetry to save the OS from crashing
            logger.warning(
                f"[Pico {self.id}] Logic Queue Full! Dropping telemetry event."
            )

        # [Spec 4.3.6.1] Safety Preemption: Force a status refresh on Alarm
        if mtype == meowprotocol.MSG_TYPE_ALARM:
            asyncio.create_task(self.send_packet(meowprotocol.MSG_TYPE_CMD_STS, ""))

        # Broadcast telemetry to the passive mirror (Twin)
        # [FIX] Do NOT filter message types here! Pass all valid payloads
        # to the Telemetry Router and let the Router sort them out.
        if is_duplicate:
            return
        try:
            from .telemetry_router import STREAM_ROUTER

            STREAM_ROUTER.route_packet(self.id, mtype, payload)
        except Exception:
            pass


# ==============================================================================
# SECTION 3: FLEET OPERATOR (The Orchestrator)
# ==============================================================================


class operator:
    """
    [Spec 4.1.2] Fleet Operator.

    Orchestrates the polling cycles and transport-layer synchronization for
    all connected Pico nodes. It ensures that the network remains active
    even when the system is IDLE.
    """

    def __init__(self, port_map, logic_queue, warning_queue, coordinator):
        """
        Initializes the fleet manager and instantiates controllers for each limb.

        :param port_map: Dictionary of Pico IDs to device paths.
        :param logic_queue: Destination for logical events.
        :param warning_queue: Destination for UI warnings.
        :param coordinator: Reference to the SystemCoordinator.
        """
        self.coord = coordinator
        self.controllers = {}
        for pid, port in port_map.items():
            self.controllers[pid] = PicoController(
                port, 115200, pid, logic_queue, coordinator
            )

        self.running = True
        self.last_sync_time = 0

    async def start(self):
        """
        Launches the nervous system processes.
        Initializes links and starts background RX/Polling loops.
        """
        for ctrl in self.controllers.values():
            await ctrl.connect()
            asyncio.create_task(ctrl.read_loop())

        asyncio.create_task(self.polling_loop())
        logger.info("[Switchboard] Fleet Commander Online (v6.4.1).")

    async def polling_loop(self):
        """
        [Spec 4.3] Master Polling Cycle.

        Executes periodic transport duties:
        1. Epoch Sync (Spec 4.3.13): Updates hardware clocks every 10 minutes.
        2. Heartbeat Dispatch: Alternates between STS, SNS, and ACT queries.
        3. Transport Retries (Spec 4.6.2): Re-broadcasts Action packets if ACK times out.
        """
        while self.running:
            try:
                now = time.time()

                # --- EPOCH SYNC (Spec 4.3.13) ---
                if now - self.last_sync_time > 600:
                    for ctrl in self.controllers.values():
                        if ctrl.connected:
                            await ctrl.send_packet(
                                meowprotocol.CMD_SYNC_TIME, str(now), requires_ack=True
                            )
                    self.last_sync_time = now

                # --- HEARTBEAT DISPATCH ---
                for ctrl in self.controllers.values():
                    if not ctrl.connected:
                        continue

                    # Adaptive polling based on state
                    interval = (
                        HEARTBEAT_SLOW
                        if ctrl.model and ctrl.model.remote_state == ms.STATE_OFFLINE
                        else HEARTBEAT_FAST
                    )

                    if (now - ctrl.last_tx_time) > interval:
                        # Round-robin between Status, Sensors, and Actuators
                        mtype = [0x11, 0x16, 0x15][ctrl.poll_index]
                        await ctrl.send_packet(mtype, "")
                        ctrl.poll_index = (ctrl.poll_index + 1) % 3

                # --- TRANSPORT RETRIES (Spec 4.6.2) ---
                for ctrl in self.controllers.values():
                    for seq, info in list(ctrl.pending_acks.items()):
                        if now - info["ts"] > ACK_TIMEOUT:
                            if info["retries"] < MAX_RETRIES:
                                if ctrl.writer:
                                    async with ctrl.tx_lock:
                                        elapsed = time.time() - ctrl.last_tx_time
                                        if elapsed < INTER_PACKET_DELAY:
                                            await asyncio.sleep(
                                                INTER_PACKET_DELAY - elapsed
                                            )
                                        ctrl.writer.write(info["pkt"])
                                        await ctrl.writer.drain()
                                        ctrl.last_tx_time = time.time()
                                    info["retries"] += 1
                                    info["ts"] = ctrl.last_tx_time
                            else:
                                logger.error(
                                    f"[Pico {ctrl.id}] Comms Timeout (Seq {seq})."
                                )
                                del ctrl.pending_acks[seq]

                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error(f"[Switchboard] Polling Error: {traceback.format_exc()}")
                await asyncio.sleep(1.0)

    async def send(self, target_id, msg_type, payload):
        """
        High-level send interface used by the Coordinator.
        Automatically determines if a message type requires transport-layer ACK.

        :param target_id: Pico ID (1-3).
        :param msg_type: MTIP Message Type code.
        :param payload: Data string or bytes.
        :return: Sequence ID for tracking.
        """
        if target_id in self.controllers:
            ctrl = self.controllers[target_id]
            # [FIX] Removed 0x14 (RESET). Do not expect ACKs from dying Picos.
            requires_ack = msg_type in [0x10, 0x18, 0x1A]
            return await ctrl.send_packet(msg_type, payload, requires_ack=requires_ack)
        return None
