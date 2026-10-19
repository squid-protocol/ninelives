# sensors.py - Ninelives Sensor HAL v1.10 (Standard v1.00 Compliant)
# PURPOSE: Hardware Abstraction Layer for Sensors (I2C, PIO, GPIO).

"""
[Spec 11.1 & 11.2] Sensor Subsystem Specification Overview.
The SensorManager serves as the unified interface for all environmental and feedback 
inputs. It provides a thread-safe, crash-resilient environment that abstracts 
hardware protocols (I2C/GPIO/PIO) into a simple dictionary of readings. 
Implements "Hardware Hygiene" (Spec 11.2) to ensure buses are released and 
state machines are stopped on system transitions.
"""

import machine
import time
import rp2
import _thread
import lib.logging as log 

# --- [Spec 11.5.1] STANDARDIZED ERROR CONSTANTS (THE ROSETTA STONE) ---
# These codes allow the RP5 'Brain' to distinguish between hardware failure, 
# software bugs, or environmental saturation.
ERR_NONE     = 0    # OK: Normal Operation
ERR_IO       = -1   # I/O FAIL: Hardware communication silence (Loose wire)
ERR_BUG      = -2   # BUG: Software/Logic crash in driver
ERR_STALE    = -3   # STALE: Sensor warming up/not ready
ERR_MISSING  = -4   # MISSING: Driver library not found in /lib
ERR_BUS_LOCK = -5   # BUS_LOCK: I2C line frozen (SDA/SCL high-z/stuck)
ERR_POISONED = -6   # POISONED: Invalid configuration (Wrong pin/bus)
ERR_LIMIT    = -7   # LIMIT: Physically impossible reading (Out of range)
ERR_ZOMBIE   = -8   # ZOMBIE: Data frozen/repeating exactly (Silicon lock)

# --- PIO PROGRAM IMPORT SAFETY (Spec 11.2) ---
pio_programs = None
try:
    import lib.pio_programs as pio_programs
except ImportError:
    log.crit("SYS", "CRITICAL: lib/pio_programs.py missing!")

# --- DRIVER IMPORT GHOSTING (Spec 4.3.9.2) ---
# Implements the "Ghost Driver" pattern. If a driver is missing, the system 
# assigns a None value which the BusWrapper translates to Code -4 (MISSING).
tsl2591 = None
mpu6050 = None

try: import lib.tsl2591 as tsl2591
except ImportError:
    try: import tsl2591
    except: pass

try: import lib.mpu6050 as mpu6050
except ImportError:
    try: import mpu6050
    except: pass

class DigitalInput:
    """
    [Spec 11.9] Digital Input Specification (GPIO Monitoring).
    Handles static GPIO inputs such as E-Stops, Limit Switches, and UI buttons.
    Implements mandatory software debouncing to reject EMI noise.
    """
    def __init__(self, pin_num, invert=False, debounce_ms=50):
        """
        [Spec 11.9.1 & 9.5] Hardware Configuration & Anti-Poison.
        Validates pin ranges for the RP2350 platform and initializes the 
        internal pull-up circuitry. Sets the default debounce window.
        """
        # Anti-Poison: Validate Pin Range
        if not (0 <= pin_num <= 29):
            log.error("SEN", f"Poisoned Pin: {pin_num}")
            self.pin = None
            return

        self.pin = machine.Pin(pin_num, machine.Pin.IN, machine.Pin.PULL_UP)
        self.invert = invert 
        self.debounce_ms = debounce_ms
        
        # State Tracking
        self.last_state = self._read_raw()
        self.last_change = time.ticks_ms()
        self.stable_state = self.last_state
        log.info("SEN", f"DigitalInput P{pin_num} Init")

    def _read_raw(self):
        """
        [Spec 11.9.2] Raw Sampling & Logic Normalization.
        Performs a hardware read and applies the 'invert' filter to ensure 
        consistent logical representation (1=Active).
        """
        if not self.pin: return ERR_POISONED
        val = self.pin.value()
        return 1 if (not val if self.invert else val) else 0

    def read(self):
        """
        [Spec 11.9.2] The Debounce Algorithm.
        Implements a non-blocking state machine that only updates the 
        stable_state when a signal persists beyond the debounce_ms threshold.
        """
        if not self.pin: return ERR_POISONED
        raw = self._read_raw()
        now = time.ticks_ms()
        
        if raw != self.last_state:
            self.last_change = now
            self.last_state = raw
            
        if time.ticks_diff(now, self.last_change) > self.debounce_ms:
            self.stable_state = raw
            
        return self.stable_state

    def close(self):
        """[Spec 11.8] Standard Lifecycle Cleanup."""
        pass

class PulseCounter:
    """
    [Spec 11.6 & 13.0] High-Performance Pulse Counting (PIO).
    Utilizes the RP2040/RP2350 PIO hardware to count Frequency Generator (FG) 
    pulses at 0% CPU load. Implements "Time as Distance" (Spec 13.1) for 
    high-speed conveyor synchronization.
    """
    def __init__(self, pin_num, state_machine_id=0, mirror_pin_num=None):
        """
        [Spec 13.2 & 13.5] Hardware Architecture & Signal Path.
        Initializes the PIO State Machine. Supports "Zero-Latency Mirroring" 
        (Spec 13.2.1) if a mirror_pin is defined, enabling the replica path 
        between Pico 3 and Pico 2.
        """
        if not pio_programs:
            log.error("PIO", f"PulseCounter {pin_num} Aborted: Lib Missing")
            self.sm = None
            return

        # Anti-Poison
        if not (0 <= pin_num <= 29):
            self.sm = None
            return

        self.pin = machine.Pin(pin_num, machine.Pin.IN, machine.Pin.PULL_UP)
        self.sm_id = state_machine_id
        
        try:
            if mirror_pin_num is not None:
                self.mirror_pin = machine.Pin(mirror_pin_num, machine.Pin.OUT)
                self.sm = rp2.StateMachine(state_machine_id, pio_programs.pulse_counter_with_mirror, in_base=self.pin, sideset_base=self.mirror_pin)
            else:
                self.sm = rp2.StateMachine(state_machine_id, pio_programs.pulse_counter_simple, in_base=self.pin)
            
            self.sm.active(1)
            log.info("PIO", f"PulseCounter {pin_num} OK")
        except Exception as e:
            log.error("PIO", f"PulseCounter {pin_num} FAIL: {e}")
            self.sm = None
            
        self.last_raw_value = 0xFFFFFFFF
        self.total_pulses = 0
        self.last_update_ms = time.ticks_ms()
        self.last_total_pulses = 0
        self.current_hz = 0
        self.zero_count = 0 
        self.calc_interval_ms = 100 

    def get_data(self):
        """
        [Spec 13.4] Python Driver Strategy (HAL).
        Implements Non-Blocking Reads (Spec 13.4.1) by checking rx_fifo() before 
        attempting to get data. Handles 32-bit counter wrap-around and applies 
        the "Rule of Eight" (Spec 13.4.5.B) for jitter-resilient stall detection.
        """
        if not self.sm: return ERR_POISONED, ERR_POISONED

        try:
            fifo_count = self.sm.rx_fifo()
            if fifo_count > 0:
                self.zero_count = 0 
                current_raw = self.last_raw_value
                for _ in range(fifo_count):
                    current_raw = self.sm.get()
                
                # Handle decrementing counter wrap
                if current_raw <= self.last_raw_value:
                    delta = self.last_raw_value - current_raw
                else:
                    delta = (self.last_raw_value + 1) + (0xFFFFFFFF - current_raw)
                
                self.total_pulses += delta
                self.last_raw_value = current_raw
            else:
                self.zero_count += 1
            
            now = time.ticks_ms()
            diff = time.ticks_diff(now, self.last_update_ms)
            
            if diff >= self.calc_interval_ms:
                pulses_in_window = self.total_pulses - self.last_total_pulses
                calculated_hz = int(pulses_in_window * 1000 / diff) if diff > 0 else 0
                
                # [Spec 13.4.5] Zero-Hold / Jitter Rejection
                # We only believe a zero Hz reading if we see it 8 times in a row.
                if calculated_hz > 0 or self.zero_count > 8:
                    self.current_hz = calculated_hz
                
                self.last_total_pulses = self.total_pulses
                self.last_update_ms = now
            
            return self.current_hz, self.total_pulses
            
        except Exception:
            return ERR_BUG, ERR_BUG

    def close(self):
        """[Spec 11.2] Hardware Hygiene: Disables the PIO State Machine."""
        if self.sm: self.sm.active(0)

class BusWrapper:
    """
    [Spec 11.4.4] Multi-Bus Abstraction (BusWrapper).
    """
    def __init__(self, config):
        """
        [Spec 11.4.4] Initializes the bus and performs initial device discovery.
        """
        self.id = config.get('id', 0)
        self.sda = config.get('sda')
        self.scl = config.get('scl')
        self.prefix = config.get('prefix', f"B{self.id}")
        self.i2c = None
        self.sensors = {} 
        self.history = {} # [Spec 11.5.2] Used for Zombie Detection
        self.err_count = 0
        self.last_read_ms = 0  # --- I2C THROTTLE TIMER ---
        self.ema_state = {}    # [LEVEL 2] EMA Filter History
        self.setup()

    def setup(self):
        """
        [Spec 11.1 & 9.5] Bus Discovery & Safe Init.
        Scans the I2C bus and instantiates detected drivers. Implements 
        the Ghost Driver pattern (Spec 11.8) for missing libraries.
        """
        # Anti-Poison
        if self.id not in [0, 1] or self.sda is None or self.scl is None:
            log.error("SEN", f"Bus {self.prefix} Poisoned Config")
            return

        try:
            # Dropped I2C speed to 100kHz to fix jumper wire capacitance/byte tearing
            self.i2c = machine.I2C(self.id, sda=machine.Pin(self.sda), scl=machine.Pin(self.scl), freq=100000)
            devs = self.i2c.scan()
            log.info("SEN", f"Bus {self.id} ({self.prefix}) Scan: {[hex(x) for x in devs]}")
            
            # [TSL Detection]
            if 0x29 in devs:
                name = f"{self.prefix}_TSL"
                if tsl2591:
                    try: self.sensors[name] = tsl2591.TSL2591(self.i2c)
                    except: self.sensors[name] = ERR_BUG
                else: self.sensors[name] = ERR_MISSING
            
            # [GYRO Detection]
            if 0x68 in devs:
                name = f"{self.prefix}_GYRO"
                if mpu6050:
                    try: self.sensors[name] = mpu6050.MPU6050(self.i2c)
                    except: self.sensors[name] = ERR_BUG
                else: self.sensors[name] = ERR_MISSING
                        
        except Exception as e:
            log.error("SEN", f"Bus {self.id} Setup Fail: {e}")
            self.err_count = 11

    def teardown(self):
        """[Spec 11.4.2] Releases the physical I2C bus resources."""
        if self.i2c:
            for s in self.sensors.values():
                if hasattr(s, 'close'): 
                    try: s.close()
                    except: pass
            self.sensors = {}
            self.i2c = None 

    def read(self, result_dict):
        """
        [Spec 11.8] Exception Propagation & Rosetta Stone Mapping.
        Iterates through sensors, catching OSErrors to identify physical wiring 
        breaks (Code -1) vs Logic Crashes (Code -2). Supports flattening of 
        complex sensors (Spec 11.7.1).
        """
        # --- I2C BUS THROTTLE (20Hz) ---
        # Prevents ultra-fast Core 1 from hammering the slow I2C silicon
        now = time.ticks_ms()
        if time.ticks_diff(now, self.last_read_ms) < 50:
            return False # Skip read, let previous values persist
        self.last_read_ms = now
        
        io_error = False
        
        for name, sensor in self.sensors.items():
            # Handle Static States (Missing drivers or crashed drivers)
            if isinstance(sensor, int):
                result_dict[name] = sensor
                continue

            try:
                val = None
                
                # [Spec 11.7.1] Flattening logic
                if "TSL" in name:
                    full, ir = sensor.get_raw_channels()
                    val = full
                    result_dict[name] = val
                    
                elif "GYRO" in name:
                    vals = sensor.get_values()
                    if isinstance(vals, dict):
                        # [FIX] Clear any old I/O error flags now that the sensor is healthy!
                        if name in result_dict:
                            del result_dict[name]
                            
                        for k, v in vals.items():
                            axis_name = f"{name}_{k.upper()}"
                            
                            # --- LEVEL 2: EMA FILTER ---
                            alpha = 0.3 # 30% New Data, 70% Old Data (Shock Absorber)
                            if axis_name not in self.ema_state:
                                self.ema_state[axis_name] = v
                            else:
                                self.ema_state[axis_name] = (v * alpha) + (self.ema_state[axis_name] * (1.0 - alpha))
                                
                            result_dict[axis_name] = self._apply_filters(axis_name, round(self.ema_state[axis_name], 2))
                        continue
                    else:
                        val = vals
                        result_dict[name] = val
                
                elif hasattr(sensor, 'read'):
                    val = sensor.read()
                    result_dict[name] = val
                
                if val is not None:
                    result_dict[name] = self._apply_filters(name, val)
                        
            except OSError:
                result_dict[name] = ERR_IO
                io_error = True
            except Exception:
                result_dict[name] = ERR_BUG
        
        return io_error

    def _apply_filters(self, name, current_val):
        """
        [Spec 11.5.2] Data Sanitization Filters.
        Implements Physical Limit Checks (Code -7) for saturation/blindness 
        and Zombie Detection (Code -8) to detect frozen silicon registers 
        that still respond to I2C pings.
        """
        # 1. Physical Limit Check (Code -7)
        if current_val > 65000: return ERR_LIMIT

        # 2. Zombie Detection (Code -8)
        if name not in self.history:
            self.history[name] = {"val": current_val, "count": 0, "last_change": time.ticks_ms()}
            return current_val
        
        hist = self.history[name]
        if current_val == hist["val"] and current_val != 0:
            hist["count"] += 1
            # [Spec 11.5.2] Trigger after ~100 consecutive identical polls
            # [FIX] TSL light sensors can be perfectly stable. Increase limit to 1200 (60 seconds)
            zombie_limit = 1200 if "TSL" in name else 100
            if hist["count"] > zombie_limit:
                return ERR_ZOMBIE
        else:
            hist["val"] = current_val
            hist["count"] = 0
            
        return current_val

class SensorManager:
    """
    [Spec 11.4] Master HAL Controller.
    The primary entry point for Core 1 (Machinist) to ingest environmental reality.
    Orchestrates Multi-Bus I2C, PIO pulse counting, and Digital debouncing.
    """
    def __init__(self, config):
        """
        [Spec 11.3] Configuration Ingestion.
        Parses config.json using Schema B (Multi-Bus) or Schema A (Legacy) 
        to establish the hardware map.
        """
        self.config = config.get('sensors', {})
        self.lock = _thread.allocate_lock()
        self.last_results = {}
        
        log.info("SEN", f"SensorManager v1.10 Startup...")

        # 1. PIO Counters [Spec 11.6]
        self.counters = {}
        for ctr in self.config.get('pulse_counters', []):
            try:
                # Anti-Poison: SM ID validation
                sm_id = ctr.get('sm_id', 0)
                if not (0 <= sm_id <= 11):
                    self.last_results[ctr['id']] = ERR_POISONED
                    continue

                c = PulseCounter(ctr['pin'], sm_id, ctr.get('mirror_pin'))
                self.counters[ctr['id']] = c
                self.last_results[f"{ctr['id']}_HZ"] = ERR_STALE
                self.last_results[f"{ctr['id']}_TOTAL"] = 0
            except: pass

        # 2. I2C Buses [Spec 11.4.4]
        self.buses = []
        if 'buses' in self.config:
            for b_cfg in self.config['buses']:
                self.buses.append(BusWrapper(b_cfg))
        elif 'i2c_bus' in self.config:
            # Legacy Schema A Support
            self.buses.append(BusWrapper({
                "id": self.config['i2c_bus'], 
                "sda": self.config['sda_pin'], 
                "scl": self.config['scl_pin'],
                "prefix": "MAIN"
            }))
        
        # 3. Digital Inputs [Spec 11.9.1]
        self.digitals = {}
        for d_cfg in self.config.get('digital_inputs', []):
            try:
                d = DigitalInput(d_cfg['pin'], d_cfg.get('invert', False), d_cfg.get('debounce_ms', 50))
                self.digitals[d_cfg['id']] = d
                self.last_results[d_cfg['id']] = 0
            except: pass
            
        self.consecutive_errors = 0

    def __enter__(self): 
        """[Spec 11.4.2] Clean Room Protocol: Context Manager entry."""
        return self

    def __exit__(self, t, v, tb):
        """[Spec 11.4.2 & 11.2] Clean Room Protocol: Mandatory Resource Teardown."""
        for b in self.buses: b.teardown()
        for c in self.counters.values(): c.close()
        for d in self.digitals.values(): d.close()

    def read_all(self):
        """
        [Spec 11.7.1] The Core 1 Update Step.
        Returns a dictionary of all readings. Implements the I2C Watchdog 
        (Spec 11.4.3), triggering an autonomous Bus Reset if 10 consecutive 
        I/O Failures are detected.
        """
        with self.lock:
            # 1. Pulse Counters
            for name, ctr in self.counters.items():
                hz, total = ctr.get_data()
                self.last_results[f"{name}_HZ"] = hz
                if total != ERR_POISONED: self.last_results[f"{name}_TOTAL"] = total
                
            # 2. I2C Buses [Spec 11.4.3 Self-Healing]
            any_io_err = False
            for bus in self.buses:
                io_err = bus.read(self.last_results)
                if io_err:
                    any_io_err = True
                    bus.err_count += 1
                    # I2C WATCHDOG TRIGGER
                    if bus.err_count > 10:
                        log.warn("SEN", f"Bus {bus.id} Lock Detected (-5). Resetting...")
                        bus.teardown()
                        time.sleep_ms(50) 
                        bus.setup()
                        bus.err_count = 0
                        # Current cycle reports Bus Lock
                        for k in bus.sensors.keys(): self.last_results[k] = ERR_BUS_LOCK
                else:
                    bus.err_count = 0

            self.consecutive_errors = 1 if any_io_err else 0

            # 3. Digital Inputs
            for name, d in self.digitals.items():
                self.last_results[name] = d.read()
                
            return self.last_results.copy()
        
    def get_telemetry_string(self):
        """
        [Spec 4.3.9] The Sensor Dump Serializer.
        Generates the standard Key=Value telemetry payload for MTIP 0x46 SNS reports.
        """
        with self.lock:
            data = self.last_results
            if not data: return "SNS_NONE"
            return ",".join([f"{k}={v}" for k, v in data.items()])

# --- VERBOSE LOGGING EXTENSION ---
"""
[Spec 11.5.1] DIAGNOSTIC LOGGING NOTE:
The 8-Code system allows for remote Forensic Audits via systemic forensic record.
- -1 (I/O FAIL): Physical disconnection or EMI.
- -4 (MISSING): Corrupted OTA or incomplete deployment.
- -5 (BUS_LOCK): Silicon-level I2C state machine freeze.
- -8 (ZOMBIE): Sensor software hang; hardware poke required.
"""