# lib/digital_twin.py - Ninelives Core 0 (v6.5.3 Passive Mirror)
# ROLE: The Passive Whiteboard. Central source of truth for reported reality.
# PURPOSE: Pure data storage. Zero logic. Zero decisions. Zero calculations.
# COMPLIANCE: Spec 3 (Structure), Spec 6 (Hierarchy), Spec 4.3.7 (Status metrics)

"""
[Spec 3.0] Ninelives Digital Twin System.

The Digital Twin serves as the single source of truth for the entire Ninelives
architecture. It acts as a real-time, memory-resident mirror of the physical
hardware. Per Goal 1, the UI and logic processes never query hardware directly;
they interact exclusively with this passive model.

Architectural Roles:
1. Data Model (Spec 3.1): Hierarchical representation of Host, Limbs, and Components.
2. Verification Hub (Spec 3.2): Tracks the 'Verification Spectrum' for actuators.
3. Telemetry Sink (Spec 6.1): Ingests high-frequency sensor data from the Nervous System.
4. Health Registry (Spec 20): Holds the raw metrics used for LQI and RTT calculations.
"""

from dataclasses import dataclass, field
from collections import deque
import time
from . import machine_states as ms

# ==============================================================================
# SECTION 1: COMPONENT MODELS (Data Only)
# ==============================================================================


@dataclass
class Actuator:
    """
    [Spec 3.2] Representation of a physical output (Motor, Solenoid).

    Stores both the 'Commanded Intent' (what the brain wants) and the
    'Reported Reality' (what the hardware actually reports). The gap between
    these two fields drives the industrial verification logic.

    Attributes:
        name (str): Canonical identifier for the actuator.
        reported_value (float): Last known value reported by hardware feedback.
        verification_state (str): Current status in the Verification Spectrum (Spec 15.2).
        last_report_time (float): Timestamp of the last hardware feedback receipt.
        intent_value (float): The targeted value requested by the control system.
        intent_mtype (int): The protocol message type used for the last intent.
        last_intent_time (float): Timestamp of the last command dispatch.
        last_update (float): Internal record of the last object modification.
        desc (str): Human-readable description of the component.
        unit (str): Unit of measure (e.g., '%', 'Hz', 'V').
    """

    name: str

    # Reported Reality (From Hardware feedback)
    reported_value: float = 0.0
    verification_state: str = ms.ACT_UNMEASURED
    last_report_time: float = 0.0

    # Commanded Intent (From Coordinator / JobManager)
    intent_value: float = 0.0
    intent_mtype: int = 0x18
    last_intent_time: float = 0.0

    # Metadata
    last_update: float = 0.0
    desc: str = ""
    unit: str = "%"


@dataclass
class Sensor:
    """
    [Spec 4.3.9] Representation of a physical input (Light, Gyro, Odometer).

    Captures raw telemetry values and timestamps from the fleet. Values are
    typically mapped into standard SI units by the Logic Engine during analysis.

    Attributes:
        name (str): Canonical identifier for the sensor.
        raw_value (any): The unprocessed data received from hardware.
        last_update (float): Timestamp of the last data ingestion.
        desc (str): Human-readable description of the sensor's role.
    """

    name: str
    raw_value: any = 0
    last_update: float = 0.0
    desc: str = ""


# ==============================================================================
# SECTION 2: SYSTEM MODELS
# ==============================================================================


@dataclass
class HostHealth:
    """
    [Spec 21.0] Host Health Mirror (Passive).

    A passive container for host-level OS metrics (RP5). Logic for updating
    these fields has been moved to the SystemCoordinator to maintain the
    'Passive Mirror' architectural standard.

    Attributes:
        cpu_cores (list): Percentage utilization per logical CPU core.
        cpu_avg (float): Mean utilization across all CPU cores.
        temp (float): Current SoC temperature in Celsius.
        ram_percent (float): Virtual memory utilization percentage.
        disk_percent (float): Root filesystem utilization percentage.
        start_time (float): The Unix epoch timestamp of module initialization.
    """

    cpu_cores: list = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    cpu_avg: float = 0.0
    temp: float = 0.0
    ram_percent: float = 0.0
    disk_percent: float = 0.0
    start_time: float = field(default_factory=time.time)


@dataclass
class Limb:
    """
    [Spec 6.2] The Digital Mirror of a physical Pico node.

    Maintains the complete state for a single hardware chassis. This class
    is logic-free; it is populated by the Coordinator and Switchboard and
    read by the GUI and SafetyManager.

    Attributes:
        id (int): Unique hardware identifier (Port ID).
        name (str): Human-readable node name (e.g., 'LOADER').
        remote_state (str): The logical state reported by the Pico firmware.
        firmware_version (str): The semver string retrieved during handshake.
        last_crash_log (str): Cached content of the most recent hardware crash report.
        remote_config (dict): The active configuration parameters on the device.
        remote_versions (dict): Version tracking for internal hardware sub-modules.
        lqi (float): Link Quality Indicator score (0-100).
        host_rtt_avg (float): Moving average of the Round-Trip Time in milliseconds.
        uptime (int): Seconds since last hardware reboot.
        voltage (float): Current input rail voltage.
        actuators (dict): Map of canonical names to Actuator component objects.
        sensors (dict): Map of canonical names to Sensor component objects.
    """

    id: int
    name: str
    remote_state: str = ms.STATE_OFFLINE

    # --- Metadata (Spec 4.4 Persistent Metadata) ---
    firmware_version: str = "Unknown"
    last_crash_log: str = ""
    remote_config: dict = field(default_factory=dict)
    remote_versions: dict = field(default_factory=dict)

    # --- Transport Health (Spec 20 - Updated by Coordinator) ---
    last_acked_seq: int = -1
    last_update: float = 0.0
    lqi: float = 100.0
    host_rtt_avg: float = 0.0
    host_rtt_max: float = 0.0
    host_seq_skips: int = 0
    host_crc_errors: int = 0
    packet_count: int = 0

    # --- Hardware Telemetry (Spec 4.3.7 Status Metrics) ---
    uptime: int = 0
    voltage: float = 0.0
    voltage_min: float = 0.0
    temp: float = 0.0
    reset_cause: str = "0"
    loop_avg: int = 0
    loop_max: int = 0
    crc_errors: int = 0  # Remote-side errors reported by Pico firmware
    i2c_errors: int = 0
    write_count: int = 0
    resp_avg: int = 0
    resp_max: int = 0
    chk_errors: int = 0

    # --- Component Registries ---
    actuators: dict = field(default_factory=dict)  # {act_name: Actuator}
    sensors: dict = field(default_factory=dict)  # {sens_name: Sensor}

    # --- Visualization Buffers ---
    history_lm: deque = field(default_factory=lambda: deque(maxlen=100))
    history_vm: deque = field(default_factory=lambda: deque(maxlen=100))

    # --- UI Triggers ---
    ui_flash_trigger: bool = False

    def touch(self):
        """Resets the liveness timer. Called by Switchboard on valid RX."""
        self.last_update = time.time()


# ==============================================================================
# SECTION 3: THE DIGITAL TWIN (The Mirror Hub)
# ==============================================================================


class DigitalTwin:
    """
    [Spec 3.0] The Global Hierarchy Root.

    Serves as the central access point for the entire machine state.
    Maintains a dictionary of Limbs representing the physical fleet.
    """

    def __init__(self):
        """Initializes the host health monitor and logical state."""
        self.host = HostHealth()
        self.host_state = ms.STATE_BOOT
        # Rectification 3.B: Removed hardcoded dictionary.
        # Populated dynamically via register_limb().
        self.limbs = {}

    def register_limb(self, port_id, name):
        """
        [Spec 6.2] Dynamically registers a new hardware limb.

        Allows the twin to scale with the provided serial configuration,
        ensuring the mirror matches the physical reality established by the Hub.

        Args:
            port_id (int): Unique hardware identifier (Port ID).
            name (str): Human-readable node name.
        """
        if port_id not in self.limbs:
            self.limbs[port_id] = Limb(port_id, name)

    def update_actuator_telemetry(self, limb_id, act_dict):
        """
        [Spec 19.2] Synchronizes hardware actuator states into the Twin.

        Maps incoming 'ACT' telemetry payloads to the appropriate internal
        Actuator objects. Detects '_ST' badges to update the Verification
        Spectrum (Spec 15.2).

        Args:
            limb_id (int): The identifier of the limb reporting the data.
            act_dict (dict): Key-value pairs of reported actuator metrics.
        """
        limb = self.limbs.get(limb_id)
        if not limb:
            return
        for key, val in act_dict.items():
            # Identify if key is a status badge (e.g., 'sol1_ST')
            name, is_status = (key[:-3], True) if key.endswith("_ST") else (key, False)
            if name not in limb.actuators:
                limb.actuators[name] = Actuator(name)

            act = limb.actuators[name]
            act.last_update = time.time()
            if is_status:
                act.verification_state = val
            else:
                try:
                    act.reported_value = float(val)
                except:
                    pass

    def update_sensor_telemetry(self, limb_id, sens_dict):
        """
        [Spec 6.1] Synchronizes hardware sensor telemetry into the Twin.

        Maps incoming 'SENS' payloads to the appropriate Sensor objects,
        updating raw values and timestamps for logic consumption.

        Args:
            limb_id (int): The identifier of the limb reporting the data.
            sens_dict (dict): Key-value pairs of raw sensor readings.
        """
        limb = self.limbs.get(limb_id)
        if not limb:
            return
        for name, val in sens_dict.items():
            if name not in limb.sensors:
                limb.sensors[name] = Sensor(name)
            limb.sensors[name].raw_value = val
            limb.sensors[name].last_update = time.time()

    def set_state(self, new_state):
        """
        [Spec 13.0] Updates the global logical state machine.
        Typically called by the Coordinator or SafetyManager.

        Args:
            new_state (str): The machine state constant from machine_states.py.
        """
        self.host_state = new_state


# [Spec 3.0] Singleton Instance for system-wide consumption
GLOBAL_TWIN = DigitalTwin()
