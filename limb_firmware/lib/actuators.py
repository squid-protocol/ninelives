# actuators.py - Ninelives Actuator HAL v1.07
# PURPOSE: Hardware Abstraction Layer for Motors, Solenoids, and Vibrators.
#
# Reference: Spec Section 10 & checklist_actuators.odt
# Target Hardware: RP2040 / RP2350 (MicroPython)

"""
[Spec 10.1] Actuator Subsystem Specification Overview.
The ActuatorManager serves as the unified interface for all kinetic and high-power 
components (Motors, Solenoids, LEDs). It is designed to decouple application 
logic (Core 0) from hardware physics (Core 1), ensuring jitter-free timing, 
brownout protection, and fail-safe shutdowns.
"""

import machine
import _thread
import time
import lib.logging as log 

# --- OPTIONAL DRIVERS ---
# These are external libraries that handle specialized hardware communication.
# If these files are missing, the system will log an error but won't crash 
# unless an actuator of that type is initialized.
try: 
    import vibration_driver
except ImportError: 
    vibration_driver = None

try: 
    import bldc_driver
except ImportError: 
    bldc_driver = None

# --- VERIFICATION STATES (Spec 4.0 / 10.8.2) ---
# These states represent the "Digital Twin" status of the physical hardware.
# They are transmitted to the RP5 as part of the ACT telemetry packet.
VS_UNMEASURED    = "U"       # [Spec 10.8.2] Default: Command changed, sensor unconfirmed
VS_VERIFYING     = "V"       # [Spec 10.8.2] Transient: Waiting for timer/settling
VS_CONFIRMED_ON  = "ON"      # [Spec 10.8.2] Success: Movement matches ON command
VS_CONFIRMED_OFF = "OFF"     # [Spec 10.8.2] Success: Silence matches OFF command
VS_FAULT_STALL   = "F_STL"   # [Spec 10.8.2] Fault: Command ON, Sensor OFF (Jam)
VS_FAULT_RUNAWAY = "F_RUN"   # [Spec 10.8.2] Fault: Command OFF, Sensor ON (Welded Relay)
VS_FAULT_THERMAL = "F_HOT"   # [Spec 10.8.2] Fault: Solenoid Thermal Fuse Tripped

# --- PWM SLICE MAPPING (HARDWARE CONSTANTS) ---
# [Spec 10.7.2] Frequency Conflict Protection.
# The RP2040/RP2350 shares a PWM generator (Slice) between pairs of pins.
# Crucially, all pins sharing a Slice MUST share the same frequency.
PWM_SLICE_MAP = {
    0: 0, 1: 0,   # Slice 0 (Primary)
    2: 1, 3: 1,   # Slice 1 (Primary)
    4: 2, 5: 2,   # Slice 2 (Primary)
    6: 3, 7: 3,   # Slice 3 (Primary)
    8: 4, 9: 4,   # Slice 4 (Primary)
    10: 5, 11: 5, # Slice 5 (Primary)
    12: 6, 13: 6, # Slice 6 (Primary)
    14: 7, 15: 7, # Slice 7 (Primary)
    
    # Aliased Allocation (Must match parent slice freq)
    16: 0, 17: 0, # Slice 0 (Aliased)
    18: 1, 19: 1, # Slice 1 (Aliased)
    20: 2, 21: 2, # Slice 2 (Aliased)
    22: 3,        # Slice 3 (Aliased)
    26: 5, 27: 5, # Slice 5 (Aliased)
    28: 6         # Slice 6 (Aliased)
}

class ActuatorManager:
    """
    [Spec 10.2] Core Actuator Subsystem Logic.
    Implements Abstraction, Soft-Start, and Fail-Safe protocols.
    Uses the Double-Buffer Pattern (Spec 10.4) for SMP thread safety.
    """
    
    def __init__(self, config):
        """
        [Spec 10.6.1] API Initialization.
        Parses configuration, initializes PWM slices/GPIO, and enforces 
        Frequency Conflict Protection (Spec 10.7.2). Sets initial state 
        to failsafe values (Spec 10.2).
        """
        # --- THREAD SAFETY (Spec 10.7.4) ---
        # We use a lock to prevent "Torn Reads" where Core 1 (Machinist) tries to read
        # a target value at the same microsecond Core 0 (Clerk) is writing it.
        self.lock = _thread.allocate_lock()
        
        # --- DATA STRUCTURES (Spec 10.4.1) ---
        self.devices = {}       # Hardware Objects (PWM, Pin, or Driver instance)
        self.targets = {}       # Desired State (0.0 to 1.0) - Written by Core 0
        self.current = {}       # Physical State (0.0 to 1.0) - Managed by Core 1
        self.configs = {}       # Static Configuration Cache (Ramping, Limits)
        
        # [VERIFICATION & HEALTH] (Spec 10.8)
        self.verify_states = {} # Current Status String (U, V, ON, F_STL...)
        self.verify_timers = {} # Time accumulator (ms) for settling/quieting logic
        self.confidence = {}    # Leaky bucket confidence score (0.0 to 100.0)
        
        # [THERMAL FUSE TRACKING] (Spec 10.7.7)
        self.on_time_ms = {}    # Tracks how many ms a device has been continuously active
        self.faults = {}        # Latching faults: { aid: "REASON_CODE" }
        
        # Tracks assigned frequency for each hardware slice to prevent conflicts.
        self.slice_frequencies = {} 
        
        # --- CONFIGURATION PARSING (Spec 10.1 & 10.3) ---
        log.info("ACT", "Initializing Actuator Subsystem...")
        
        for act_cfg in config.get('actuators', []):
            try:
                aid = act_cfg['id']
                pin_num = act_cfg['pin']
                atype = act_cfg['type']
            except KeyError as e:
                log.error("ACT", f"Skipping Malformed Actuator Config: Missing {e}")
                continue

            # [Spec 10.7.7] Solenoid Thermal Fuse Default Safety
            # Solenoids risk fire if left on too long. Enforce a software limit.
            if atype == "SOLENOID" and 'max_on_ms' not in act_cfg:
                act_cfg['max_on_ms'] = 750 # 750ms safe limit by default
            
            # [Spec 8.1] Store config reference for real-time physics lookups
            self.configs[aid] = act_cfg
            
            # [Spec 10.2] Safe Start: Initialize both buffers to failsafe values
            failsafe = act_cfg.get('failsafe_val', 0.0)
            self.targets[aid] = failsafe
            self.current[aid] = failsafe
            
            # Initialize state tracking dictionaries
            self.verify_states[aid] = VS_UNMEASURED
            self.verify_timers[aid] = 0
            self.on_time_ms[aid] = 0
            self.confidence[aid] = 100.0 # [LEVEL 1] Start with full confidence

            try:
                # --- PWM CONFLICT CHECK (Spec 10.7.2) ---
                if atype in ["PWM", "BLDC", "VIBE_DRIVER"]:
                    if pin_num not in PWM_SLICE_MAP:
                        log.error("ACT", f"Pin {pin_num} not in PWM Map! Skipping {aid}.")
                        continue
                    
                    slice_id = PWM_SLICE_MAP[pin_num]
                    req_freq = act_cfg.get('freq', 1000)
                    
                    if slice_id in self.slice_frequencies:
                        assigned_freq = self.slice_frequencies[slice_id]
                        if assigned_freq != req_freq:
                            log.warn("ACT", f"Slice {slice_id} Conflict! Forcing {assigned_freq}Hz.")
                            req_freq = assigned_freq
                    else:
                        self.slice_frequencies[slice_id] = req_freq

                # --- HARDWARE INSTANTIATION (Spec 10.9) ---
                
                # [CASE A] Specialized Vibration Driver
                if atype == "VIBE_DRIVER":
                    if not vibration_driver:
                        log.error("ACT", f"Missing lib/vibration_driver.py for {aid}")
                        continue
                    self.devices[aid] = vibration_driver.VibrationDriver(
                        pin_num, 
                        min_duty=act_cfg.get('min_duty', 0.0),
                        max_duty=act_cfg.get('max_duty', 1.0),
                        freq=req_freq 
                    )

                # [CASE B] BLDC Motor (Conveyor)
                elif atype == "BLDC":
                    if not bldc_driver:
                        log.error("ACT", f"Missing lib/bldc_driver.py for {aid}")
                        continue
                    dir_pin = act_cfg.get('dir_pin')
                    if dir_pin is None:
                        log.error("ACT", f"BLDC {aid} missing 'dir_pin'")
                        continue
                    self.devices[aid] = bldc_driver.BLDCDriver(
                        pwm_pin_num=pin_num,
                        dir_pin_num=dir_pin,
                        invert_dir=act_cfg.get('invert_dir', False),
                        min_duty=act_cfg.get('min_duty', 0.0)
                    )
                    log.info("ACT", f"Init BLDC {aid}")

                # [CASE C] Standard PWM Output (Fans, LEDs)
                elif atype == "PWM":
                    pin = machine.Pin(pin_num, machine.Pin.OUT)
                    pwm = machine.PWM(pin)
                    pwm.freq(req_freq) 
                    pwm.duty_u16(0)
                    self.devices[aid] = pwm
                    log.info("ACT", f"Init PWM {aid} @ {req_freq}Hz")
                    
                # [CASE D] Digital Output (Solenoids, Relays)
                elif atype in ["DIGITAL", "SOLENOID"]:
                    pin = machine.Pin(pin_num, machine.Pin.OUT)
                    pin.value(0)
                    self.devices[aid] = pin
                    log.info("ACT", f"Init {atype} {aid} on P{pin_num}")
                
                else:
                    log.error("ACT", f"Unknown Actuator Type: {atype}")

            except Exception as e:
                log.error("ACT", f"Critical Init Fail for {aid}: {e}")

    # --- CONTEXT MANAGER GUARD (Spec 10.5 & 10.7.6) ---
    def __enter__(self):
        """[Spec 10.7.6] Context Guard entry point."""
        return self
    
    def __exit__(self, t, v, tb): 
        """
        [Spec 10.5] "Smart Exit" Protocol implementation.
        Differentiates between Crash/E-Stop (Spec 10.5.1) and Safe Stop (Spec 10.5.2).
        Ensures inescapable shutdown regardless of exception state.
        """
        if t is not None:
            # A crash occurred
            self.emergency_stop()
            log.crit("ACT", f"Safety Shutdown: {v}")
        else:
            # Clean exit
            self.safe_stop()
        return False

    # --- PUBLIC API (Spec 10.6) ---
    
    def set_target(self, aid, value):
        """
        [Spec 10.6.2] Thread-Safe Intent Update.
        Called by Core 0. Clamps logical float (0.0-1.0) and updates targets buffer.
        Triggers verification resets and handles fault clearing (Spec 10.7.7).
        """
        if aid not in self.targets: 
            return
        
        # Clamp logic to physical bounds
        val = max(0.0, min(1.0, float(value)))
        
        with self.lock:
            # [Spec 10.7.7] Check for Fault Reset (Latch Clearing)
            # If device is faulted, user must set to 0.0 to acknowledge and reset.
            if val == 0.0 and aid in self.faults:
                old_fault = self.faults[aid]
                del self.faults[aid]
                log.info("ACT", f"Fault Cleared [{old_fault}]: {aid}")

            # [Spec 10.8.2] VERIFICATION RESET
            # When a new target is set, we move back to UNMEASURED state.
            # This triggers settle/quiet timers (Spec 16.4.1, 16.4.2).
            if abs(self.targets[aid] - val) > 0.01:
                self.verify_states[aid] = VS_UNMEASURED
                self.verify_timers[aid] = 0
            
            self.targets[aid] = val

    def get_telemetry_string(self):
        """
        [Spec 10.8.4] Digital Twin Telemetry Serializer.
        Generates the mandatory _ST suffix status badges for the RP5 Digital Twin.
        Encapsulates both Command Intent and Verified Reality.
        """
        parts = []
        with self.lock:
            t_copy = self.targets.copy()
            v_copy = self.verify_states.copy()
            
        for aid, val in t_copy.items():
            parts.append(f"{aid}={val:.2f}")
            if aid in v_copy:
                parts.append(f"{aid}_ST={v_copy[aid]}")
        return ",".join(parts)

    def safe_stop(self):
        """
        [Spec 10.5.2] Case B: Clean Shutdown.
        Gracefully ramps all actuators to 0.0 to prevent mechanical shock loads.
        Waits for ramp duration before final hardware clamping.
        """
        log.info("ACT", "Performing Safe Stop...")
        with self.lock:
            for aid in self.targets: 
                self.targets[aid] = 0.0
        
        # Give the physics loop 100ms to ramp down
        time.sleep_ms(100) 
        self.emergency_stop(silent=True)

    def emergency_stop(self, silent=False):
        """
        [Spec 10.5.1] Case A: Emergency Stop (E-Stop).
        Priority 1 Shutdown. Acquires SMP lock, bypasses ramping, and 
        forces all hardware pins to failsafe values instantly.
        """
        if not silent: 
            log.crit("ACT", ">>> EMERGENCY STOP <<<")
        
        with self.lock:
            for aid in self.targets:
                self.targets[aid] = 0.0
                self.current[aid] = 0.0
                dev = self.devices.get(aid)
                if dev:
                    try:
                        if hasattr(dev, 'stop'): 
                            dev.stop() 
                        else:
                            # Respect active-low hardware during an E-Stop
                            inverted = self.configs.get(aid, {}).get('invert', False)
                            safe_val_pwm = 65535 if inverted else 0
                            safe_val_dig = 1 if inverted else 0
                            
                            if hasattr(dev, 'duty_u16'): 
                                dev.duty_u16(safe_val_pwm) 
                            else: 
                                dev.value(safe_val_dig) 
                    except: pass
   
    # --- VERIFICATION LOGIC (Spec 10.8 & 16.0) ---
    def update_verification(self, aid, dt_ms, sensor_data):
        """
        [Spec 16.0] Closed-Loop Bridge Architecture.
        Performs Intent vs Reality comparison. Implements Transient Management 
        (Spec 16.4) via settle_ms and quiet_ms timers. Latch Stall (F_STL) 
        and Runaway (F_RUN) faults.
        """
        cfg = self.configs[aid].get('verification')
        if not cfg: return 

        # [Spec 16.4] State Management: Settling & Quiet Time
        self.verify_timers[aid] += dt_ms
        
        # [Spec 16.4.1] Settle Time: Wait before enforcing stall checks
        if self.verify_states[aid] == VS_UNMEASURED:
            settle = cfg.get('settle_ms', 500)
            if self.verify_timers[aid] < settle:
                return 
            self.verify_states[aid] = VS_VERIFYING

        # 2. Reality Check
        strategy = cfg.get('strategy')
        sensor_key = cfg.get('sensor_id')
        threshold = cfg.get('threshold', 1)
        
        is_moving = False
        val = 0

        try:
            if strategy == "TACHOMETER":
                # [Spec 13.4] Check both raw and Hz versions
                val = sensor_data.get(sensor_key, sensor_data.get(f"{sensor_key}_HZ", 0))
                
                # [Spec 11.5] If sensor reports an error code (< 0), ignore this frame
                if val < 0: return
                
                if val >= threshold: 
                    is_moving = True

            elif strategy == "GYRO_NOISE":
                if sensor_data.get(f"{sensor_key}_GYRO", 0) == -1: return 
                gx = abs(sensor_data.get(f"{sensor_key}_GYRO_X", sensor_data.get(f"{sensor_key}_GYRO_GX", 0)))
                gy = abs(sensor_data.get(f"{sensor_key}_GYRO_Y", sensor_data.get(f"{sensor_key}_GYRO_GY", 0)))
                gz = abs(sensor_data.get(f"{sensor_key}_GYRO_Z", sensor_data.get(f"{sensor_key}_GYRO_GZ", 0)))
                val = gx + gy + gz
                if val > threshold: is_moving = True

            # --- NEW: DIRECTIONAL ISOLATION STRATEGIES ---
            elif strategy == "GYRO_Z_ONLY":
                if sensor_data.get(f"{sensor_key}_GYRO", 0) == -1: return
                gz = abs(sensor_data.get(f"{sensor_key}_GYRO_Z", sensor_data.get(f"{sensor_key}_GYRO_GZ", 0)))
                val = gz
                if val > threshold: is_moving = True
        except:
            return 

        # [Spec 16.2] Core Verification Truth Table
        is_powered = self.current[aid] > 0.05 
        new_state = self.verify_states[aid]

        # --- LEVEL 1: LEAKY BUCKET ---
        if is_powered:
            if is_moving:
                self.confidence[aid] = min(100.0, self.confidence[aid] + 15.0) # Fill fast on good data
            else:
                self.confidence[aid] = max(0.0, self.confidence[aid] - 2.0)  # Drain slow to absorb glitches

        # --- STATE EVALUATION ---
        if is_powered and self.confidence[aid] > 20.0:
            new_state = VS_CONFIRMED_ON
            self.verify_timers[aid] = 0 # Reset "Quiet" timer
        elif not is_powered and not is_moving:
            new_state = VS_CONFIRMED_OFF
            self.verify_timers[aid] = 0 # Reset "Quiet" timer
            self.confidence[aid] = 100.0 # Reset bucket for the next cycle
        
        # --- POTENTIAL FAULT DETECTED (Spec 16.5) ---
        elif is_powered and self.confidence[aid] <= 0.0:
            # STALL: Bucket completely drained. Wait for settle_ms before latching.
            if self.verify_timers[aid] > cfg.get('settle_ms', 500):
                new_state = VS_FAULT_STALL
        
        elif not is_powered and is_moving:
            # [Spec 16.4.2] RUNAWAY: Moving but not powered. 
            quiet_time = cfg.get('quiet_ms', 1000)
            if self.verify_timers[aid] > quiet_time:
                new_state = VS_FAULT_RUNAWAY

        # 4. Fault Latching
        if new_state in [VS_FAULT_STALL, VS_FAULT_RUNAWAY]:
            if aid not in self.faults:
                log.crit("ACT", f"FAULT CONFIRMED: {aid} -> {new_state} (Val: {val})")
                with self.lock:
                    self.faults[aid] = new_state

        self.verify_states[aid] = new_state
   

    # --- PHYSICS LOOP (Core 1) ---
    
    def update(self, dt_ms, sensor_data={}):
        """
        [Spec 10.6.3 & 10.4.2] Core 1 High-Speed Physics Step.
        Advances the physical simulation including soft-start ramping (Spec 10.7.5).
        Enforces Solenoid Thermal Fusing (Spec 10.7.7) and Escalation overrides (Spec 16.5).
        Performs hardware write abstraction for heterogeneous drivers.
        """
        local_targets = {}
        
        # [Spec 10.7.4] Thread-Safe Copy of Targets
        with self.lock: 
            local_targets = self.targets.copy()
            
        for aid, dev in self.devices.items():
            cfg = self.configs[aid]
            target = local_targets.get(aid, 0.0)
            curr = self.current.get(aid, 0.0)
            
            # --- Verification Step (Spec 16.0) ---
            self.update_verification(aid, dt_ms, sensor_data)
            
            # --- [Spec 10.7.7] SOLENOID THERMAL FUSE CHECK ---
            # Protects solenoids from burning up if software hangs or operator error.
            max_on = cfg.get('max_on_ms', 0)
            if max_on > 0:
                if curr > 0.01:
                    self.on_time_ms[aid] += dt_ms
                    if self.on_time_ms[aid] > max_on:
                        if aid not in self.faults:
                            with self.lock:
                                self.faults[aid] = VS_FAULT_THERMAL
                                self.targets[aid] = 0.0 
                            self.verify_states[aid] = VS_FAULT_THERMAL
                            log.error("ACT", f"Thermal Trip: {aid} (> {max_on}ms)")
                else:
                    self.on_time_ms[aid] = 0 # Reset timer when OFF
            
            # --- [Spec 16.5] HARDWARE OVERRIDE IF FAULTED ---
            # If any fault exists (Stall, Runaway, Thermal), we force the output to 0.
            if aid in self.faults:
                target = 0.0
                self.current[aid] = 0.0 
                new_val = 0.0
            
            # --- [Spec 10.7.5] Ramping Logic (Soft-Start) ---
            # Limits inrush current to prevent brownouts.
            else:
                if abs(curr - target) > 0.001:
                    ramp_time = cfg.get('ramp_ms', 0)
                    if ramp_time <= 0: 
                        new_val = target 
                    else:
                        step = (dt_ms / ramp_time)
                        if curr < target:
                            new_val = min(target, curr + step)
                        else:
                            new_val = max(target, curr - step)
                    self.current[aid] = new_val
                else:
                    new_val = target

            # --- HARDWARE WRITE ABSTRACTION (Spec 10.1) ---
            try:
                # [CASE A] BLDC Motor Driver (Spec 10.9)
                if bldc_driver and isinstance(dev, bldc_driver.BLDCDriver):
                    dev.set_speed(new_val)
                
                # [CASE B] Specialized Vibration Driver (Spec 10.9)
                elif vibration_driver and isinstance(dev, vibration_driver.VibrationDriver):
                    dev.set_speed(new_val)
                
                # [CASE C] Standard PWM (Fan, LED)
                elif isinstance(dev, machine.PWM):
                    output = new_val 
                    # [Spec 10.7.3] Logic Inversion check
                    if cfg.get('invert', False): 
                        output = 1.0 - output

                    # [Spec 10.7.1] Deadzone Compensation (min_duty)
                    min_d = cfg.get('min_duty', 0.0)
                    max_d = cfg.get('max_duty', 1.0)
                    
                    if output > 0.001:
                        # Map logical 0-1 range to physical min-max duty
                        scaled = min_d + (output * (max_d - min_d))
                        dev.duty_u16(int(scaled * 65535))
                    else: 
                        dev.duty_u16(0)
                
                # [CASE D] Digital Pin (Relay, Solenoid)
                elif hasattr(dev, 'value'):
                    val_bool = 1 if new_val > 0.5 else 0
                    if cfg.get('invert', False): 
                        val_bool = 1 - val_bool
                    dev.value(val_bool)
                    
            except Exception as e:
                # Catch hardware write errors to prevent the entire Core 1 thread from crashing.
                pass

    # --- DIAGNOSTICS & AUDIT (Spec 7.0 & 12.0) ---
    
    def perform_health_audit(self):
        """
        [Spec 10.8.4 & 12.0] Component Health Audit.
        Scans all actuators and returns digital twin status summaries.
        Utilized for the System Manifest and forensic logic audits.
        """
        report = {}
        for aid in self.configs:
            report[aid] = {
                "state": self.verify_states.get(aid, VS_UNMEASURED),
                "faulted": aid in self.faults,
                "current_power": self.current.get(aid, 0.0)
            }
        return report