# app.py - Ninelives Main Application v1.20 (Resiliency Patch v1.02)
# PURPOSE: Main Event Loop for RP2040/RP2350 (The Limb)

"""
[Spec 4.0] Communication & Command Architecture (MTIP v1.02).
The Ninelives Kernel serves as the primary event coordinator for the Pico "Limb," 
interfacing between the RP5 Brain and the real-time hardware managers.
It manages dual-core synchronization, state transitions, and the MTIP protocol.
"""

import machine
import _thread
import time
import gc
import json
import os
import sys
import struct
import meowprotocol
import actuators
import sensors
import ota
import diagnostics
from machine import WDT, Pin, ADC

# --- LOGGING SETUP (Spec 14.1) ---
try:
    import lib.logging as log
except ImportError:
    class LogFallback:
        """
        [Spec 14.1] Safety Telemetry & Logging Strategy.
        Provides a critical fallback for the central LogManager to ensure 
        system diagnostics are never lost due to filesystem or import failures.
        """
        
        def info(self, t, m): 
            """[Spec 14.1.1] Fallback info output to console."""
            print(f"[INFO] [{t}] {m}")

        def warn(self, t, m): 
            """[Spec 14.1.1] Fallback warning output to console."""
            print(f"[WARN] [{t}] {m}")
            
        def error(self, t, m): 
            """[Spec 14.1.1] Fallback error output to console."""
            print(f"[ERROR] [{t}] {m}")

        def crit(self, t, m): 
            """[Spec 14.1.1] Fallback critical output to console."""
            print(f"[CRIT] [{t}] {m}")

        def debug(self, t, m): 
            """[Spec 14.1.1] Fallback debug output to console."""
            print(f"[DEBUG] [{t}] {m}")
    log = LogFallback()

# --- 1. CONFIGURATION & CONSTANTS ---
STATE_BOOT    = "BOOT"
STATE_IDLE    = "IDLE"
STATE_FLOW    = "FLOW"
STATE_ERROR   = "ERROR"
STATE_OTA     = "OTA"
STATE_GHOST   = "GHOST"

# Default "Anti-Poison" Config (Used if disk config fails)
SAFE_CONFIG = {
    "system": {"device_id": 3, "role": "LIMB"}, 
    "comms": {"uart_bus": 0, "tx_pin": 0, "rx_pin": 1, "baud_rate": 115200, "max_retries": 5},
    "actuators": [],
    "sensors": {},
    "testing": {
        "watchdog_enabled": True,
        "share_actuator_events": False,
        "log_traffic": False,
        "debug_parser": False,
        "health_print_interval_ms": 0,
        "sensor_print_interval_ms": 0,
        "actuator_print_interval_ms": 0,
        "debug_stream_ms": 0
    }
}
system_config = SAFE_CONFIG 

# --- 2. GLOBAL STATE (Shared Memory) ---
# Lock prevents "Torn Reads" where Core 0 reads data while Core 1 is writing it
state_lock = _thread.allocate_lock()
shared_state = {
    "current_state": STATE_BOOT,
    "last_cmd_id": 0,
    "uptime_start": time.time(),
    "id": 3, 
    "error_code": "NONE",
    "lives_refilled": False,
    "boot_attempts_cleared": False,
    "core1_tick": time.ticks_ms(), # Init to avoid immediate WDT kill
    # Metrics
    "loop_avg_us": 0,
    "loop_max_us": 0,
    "min_voltage_latch": 5.0,
    "crc_errors": 0,      
    "checksum_errors": 0, 
    "i2c_errors": 0,      
    "write_count": 0,     
    
    # Windowed Latency History
    "lat_history": [], 
    
    # --- PROTOCOL ADDITIONS (Spec 4.3.2 Distinct Queues) ---
    "next_seq": 1,        
    "tx_safety": {},      # Queue 0x48 (High Priority - Alarms/Errors)
    "tx_events": {},      # Queue 0x40 (Medium Priority - Sensor Events)
    
    # --- GENERIC CORE-1 -> CORE-0 EVENT STAGING ---
    "pending_events": [],
    "recalibrate_requested": False
}

packet_dedupe = {}

# --- 3. HARDWARE MANAGERS ---
act_mgr = None
sns_mgr = None
ota_mgr = None
sys_man = None

adc_vsys = None
sensor_temp = None

# GLOBAL WATCHDOG (Required for OTA Handoff)
wdt = None

# --- [RESILIENCY] MEMORY GUARD (Spec 9.1.C) ---
def memory_guard(threshold_bytes=8192):
    """
    [Spec 9.1.C] Memory Guard Protocol.
    Before any memory-intensive allocation (like JSON serialization or large 
    packet building), the Clerk checks if available heap is above the threshold.
    Returns True if safe, False if memory is critically low.
    """
    gc.collect() # Force cleanup to get real free memory
    free = gc.mem_free()
    if free < threshold_bytes:
        log.warn("SYS", f"Memory Guard Triggered: {free} bytes free (<{threshold_bytes})")
        return False
    return True

def get_mem_percent():
    """Calculates the percentage of the heap that is currently free."""
    f = gc.mem_free()
    a = gc.mem_alloc()
    total = f + a
    return (f / total * 100) if total > 0 else 0

# --- HELPER: SAVE CONFIG ATOMIC (Pretty Stream Writer) ---
def save_config_atomic():
    """
    [Spec 9.9] Flash Write Hygiene (The "Blind Spot").
    Implements the atomic swap rotation (Spec 9.3) to prevent filesystem corruption.
    Enforces the "No-Write" policy during STATE_FLOW (Spec 9.9.B) to maintain 
    microsecond-level motor timing precision.
    """
    global system_config
    
    # [FIX] Flash Hygiene: Strictly forbid writes during FLOW to prevent motor stutter
    with state_lock:
        if shared_state["current_state"] == STATE_FLOW:
            log.warn("CFG", "Save Aborted: Cannot write to Flash in FLOW state")
            return False

    # [RESILIENCY] Check if we have enough RAM to process the config serialization
    if not memory_guard(threshold_bytes=16384): # Require 16KB for safe serialization
        log.error("CFG", "Save Aborted: Insufficient RAM for JSON buffer")
        return False

    tmp_file = "config.json.tmp"
    target_file = "config.json"
    
    try:
        # 1. Generate Compact String (Low Memory)
        compact = json.dumps(system_config)
        
        # 2. Stream Write with formatting
        with open(tmp_file, "w") as f:
            indent = 0
            in_string = False
            escape = False
            
            for char in compact:
                if in_string:
                    f.write(char)
                    if escape:
                        escape = False
                    elif char == '\\':
                        escape = True
                    elif char == '"':
                        in_string = False
                else:
                    if char == '"':
                        f.write(char)
                        in_string = True
                    elif char in ['{', '[']:
                        f.write(char)
                        f.write('\n')
                        indent += 1
                        f.write('    ' * indent)
                    elif char in ['}', ']']:
                        f.write('\n')
                        indent -= 1
                        f.write('    ' * indent)
                        f.write(char)
                    elif char == ',':
                        f.write(char)
                        f.write('\n')
                        f.write('    ' * indent)
                    elif char == ':':
                        f.write(char)
                        f.write(' ')
                    elif char.isspace():
                        pass
                    else:
                        f.write(char)
                        
        # 3. Atomic Swap
        os.rename(tmp_file, target_file)
        
        # 4. Increment write count
        with state_lock: shared_state["write_count"] += 1
        log.info("CFG", "Saved config.json (Atomic/Pretty)")
        return True
    except Exception as e:
        log.error("CFG", f"Save Failed: {e}")
        return False

# --- HELPER: LIFE REFILL (Flash Hygiene) ---
def refill_lives():
    """
    [Spec 1.0] Core Philosophy: "Unbrickable by Design".
    Detects system stability (30s threshold). If the system survives the 
    boot window, the decrementing "Life Counter" is refilled to 9, indicating 
    a known-good firmware state.
    """
    # Spec 9.9: No writes in FLOW to prevent CPU stutter
    with state_lock:
        if shared_state["current_state"] == STATE_FLOW:
            return False

    try:
        # [FIX] Atomic write to prevent file corruption during brownouts
        with open("lives.txt.tmp", "w") as f:
            f.write("9")
            f.flush()
        try: os.remove("lives.txt")
        except: pass
        os.rename("lives.txt.tmp", "lives.txt")
        
        with state_lock: shared_state["write_count"] += 1
        log.info("SYS", "Lives Refilled to 9")
        return True
    except: return False

# --- HELPER: BOOT COUNTER CLEAR (Loop Protection) ---
def clear_boot_attempts():
    """
    [Spec 15.2] Automatic Rollback Logic.
    Resets the boot_attempts.txt counter once industrial stability is achieved.
    This prevents the bootloader (Spec 15.2.1) from triggering an unwanted 
    restoration of app.py.bak.
    """
    try:
        # [FIX] Atomic write to prevent file corruption during brownouts
        with open("boot_attempts.txt.tmp", "w") as f:
            f.write("0")
            f.flush()
        try: os.remove("boot_attempts.txt")
        except: pass
        os.rename("boot_attempts.txt.tmp", "boot_attempts.txt")
        
        with state_lock: shared_state["write_count"] += 1
        log.info("SYS", "Boot Attempts Cleared (Stability Achieved)")
        return True
    except: return False

def get_cpu_temp():
    """
    [Spec 9.19] Active Thermal Safety & Interlock.
    Reads the internal temperature sensor of the RP2350. Used to drive the 
    tiered fever response (Spec 9.19.2) and thermal lockout thresholds.
    """
    try:
        if not sensor_temp: return 0
        reading = sensor_temp.read_u16() * (3.3 / 65535)
        return 27 - (reading - 0.706)/0.001721
    except: return 0

def get_vsys_voltage():
    """
    [Spec 9.4] "Death Gasp" Telemetry (Brownout Handling).
    Performs VSYS sampling via ADC divider. Vital for identifying voltage 
    sags (Spec 7.3 - Metric VM) during high-torque motor starts.
    """
    try:
        if not adc_vsys: return 5.0
        # Pico VSYS divider is 1/3 (GPIO29)
        return adc_vsys.read_u16() * 3.3 / 65535 * 3
    except: return 5.0

# --- HELPER: BROWNOUT CHECK (Death Gasp) ---
def check_brownout():
    """
    [Spec 9.4] Industrial Robustness: Brownout Protocol.
    Detects critical power loss (<4.4V). Triggers immediate actuator halt 
    and safe hibernation to prevent flash corruption and uncontrolled 
    mechanical movement.
    """
    v = get_vsys_voltage()
    
    with state_lock:
        if v < shared_state["min_voltage_latch"]:
            shared_state["min_voltage_latch"] = v

    # [CRITICAL] Immediate Shutdown Threshold
    if v < 4.4:
        log.crit("PWR", f"BROWNOUT DETECTED: {v:.2f}V")
        if act_mgr: act_mgr.emergency_stop()
        while True: machine.idle()
        
    # [WARNING] Yellow Zone (Near Brownout)
    elif v < 4.7:
        pass 

# Generates the 'STS' key-value string for system health reporting
def build_status_string():
    """
    [Spec 7.0] Status & Health Protocol (STS).
    Aggregates mandatory industrial metrics (Spec 7.3) including Uptime, 
    Voltage Sag, Temperature, Loop Jitter (LM), and Bus Errors into 
    the standardized MTIP STS payload.
    Now includes RAM fragmentation metrics.
    """
    uptime = int(time.time() - shared_state["uptime_start"])
    temp_c = int(get_cpu_temp())
    volt = float(get_vsys_voltage())
    ram_p = get_mem_percent()
    
    rst_cause = "PWR" 
    if machine.reset_cause() == machine.WDT_RESET: rst_cause = "WDT"
    
    with state_lock:
        lm = shared_state["loop_max_us"]
        # [TUNE] Decay by 50% instead of resetting to 0
        shared_state["loop_max_us"] = int(lm >> 1) 
        
        vm = shared_state["min_voltage_latch"]
        
        # Calculate Response metrics from Rolling History
        hist = shared_state["lat_history"]
        if hist:
            ra = int(sum(hist) / len(hist))
            rl = max(hist)
        else:
            ra = 0
            rl = 0
        
        ce = shared_state['crc_errors']
        ie = shared_state['i2c_errors']
        wc = shared_state['write_count']
        cse = shared_state['checksum_errors']
        la = shared_state['loop_avg_us']
        curr_st = shared_state['current_state']
        ua_ids = sorted(
            list(shared_state['tx_safety'].keys())
            + list(shared_state['tx_events'].keys())
        )

    status = (
        f"ST={curr_st},"
        f"UPT={uptime},"
        f"V={volt:.2f},"
        f"VM={vm:.2f},"
        f"T={temp_c},"
        f"RAM={ram_p:.1f}%,"
        f"RST={rst_cause},"
        f"LA={la},"
        f"LM={lm},"
        f"CE={ce},"
        f"IE={ie},"
        f"WC={wc},"
        f"RA={ra}," 
        f"RL={rl}," 
        f"CSE={cse}"
    )
    if ua_ids:
        status += ",UA=" + ",".join(str(x) for x in ua_ids)
    return status

# --- HELPER: PRIORITY SEND (Spec 4.3.2) ---
def send_reliable(uart, target, m_type, payload):
    """
    [Spec 4.3.6] Ack-Back & Reliability Strategy.
    Implements persistent event egress with redundant re-broadcast (200ms).
    Separates traffic into the 0x48 Safety Channel (Spec 4.3.2) and 0x40 Event Channel.
    """
    with state_lock:
        seq = shared_state["next_seq"]
        shared_state["next_seq"] = (seq + 1) % 255
        if shared_state["next_seq"] == 0: shared_state["next_seq"] = 1
        my_id = shared_state["id"]
    
    pkt = meowprotocol.build_packet(target, my_id, seq, m_type, payload)
    
    # --- STANDALONE MODE: FIRE AND FORGET ---
    if system_config.get("testing", {}).get("standalone_mode", False):
        uart.write(pkt) 
        return seq
        
    entry = { "pkt": pkt, "ts": time.ticks_ms(), "retries": 0 }

    with state_lock:
        if m_type == meowprotocol.MSG_TYPE_ALARM: # 0x48
            shared_state["tx_safety"][seq] = entry
        else: # 0x40 and others
            shared_state["tx_events"][seq] = entry
    
    uart.write(pkt)
    return seq

# --- 4. CORE 1: THE MACHINIST (Physics Loop) ---
def core1_task():
    """
    [Spec 2.1] Core 1: The Machinist (Real-Time Role).
    """
    global shared_state, act_mgr, sns_mgr
    last_tick = time.ticks_us()
    loop_accum = 0
    loop_count = 0
    local_max_us = 0
    
    # --- OPTIONAL METROLOGY / EVENT PLUGIN ---
    event_monitor = None
    try:
        import lib.breakbeam_monitor as bm
        event_monitor = bm.BreakbeamMonitor(system_config)
    except ImportError:
        try:
            import breakbeam_monitor as bm
            event_monitor = bm.BreakbeamMonitor(system_config)
        except ImportError:
            pass
    
    log.info("SYS", "Core 1 Thread Start")
    
    while True:
        try:
            # [SAFETY FIX 4.1] XIP PAUSE GUARD (Spec 9.18)
            if shared_state["current_state"] == STATE_OTA:
                time.sleep_ms(20)
                continue

            # [FIX] Time Dilation Calculation
            loop_start = time.ticks_us()
            dt_us = time.ticks_diff(loop_start, last_tick)
            last_tick = loop_start
            
            if dt_us <= 0: dt_us = 1
            dt_ms = dt_us / 1000.0
            if dt_ms > 100.0: dt_ms = 100.0
            
            
            # --- START CLOSED-LOOP BRIDGE MELD ---
            # 1. Read Reality (Sensors) [Spec 16.1]
            sensor_data = {} 
            if sns_mgr: 
                try:
                    sensor_data = sns_mgr.read_all()
                    if event_monitor:
                        with state_lock:
                            if shared_state.get("recalibrate_requested", False):
                                event_monitor.reset_calibration()
                                shared_state["recalibrate_requested"] = False
                        evt_payload = event_monitor.update(sensor_data, sns_mgr=sns_mgr)
                        if evt_payload:
                            with state_lock:
                                shared_state["pending_events"].append(evt_payload)
                except Exception as e:
                    pass

            # 2. Update Actuators (Ramping, Physics) [Spec 16.2]
            if act_mgr: 
                # Passing sensor_data enables the VS_FAULT_STALL/RUNAWAY detection logic
                act_mgr.update(dt_ms, sensor_data=sensor_data) 
                
                # Escalation [Spec 16.5]
                if act_mgr.faults:
                    with state_lock:
                        if shared_state["current_state"] != STATE_ERROR:
                            shared_state["current_state"] = STATE_ERROR
                            # Get the first reason from the fault dictionary
                            reason = list(act_mgr.faults.values())[0]
                            shared_state["error_code"] = f"FAULT:{reason}"
                            log.crit("SYS", f"Actuator Fault Escalated: {reason}")
            # --- END CLOSED-LOOP BRIDGE MELD ---

                # --- E-STOP CHECK (Spec 6.0) ---
                estop_val = sensor_data.get("ESTOP_BUTTON")
                if estop_val is None:
                    if loop_count % 100 == 0: 
                         log.warn("SYS", "Safety Sensor ESTOP_BUTTON Not Detected!")
                elif estop_val == 1:
                    with state_lock:
                        if shared_state["current_state"] != STATE_ERROR:
                            shared_state["current_state"] = STATE_ERROR
                            shared_state["error_code"] = "E-STOP TRIGGERED"
                            log.crit("SYS", ">>> E-STOP BUTTON TRIGGERED <<<")
                    if act_mgr: act_mgr.emergency_stop()

                if hasattr(sns_mgr, 'consecutive_errors'):
                    with state_lock:
                        if sns_mgr.consecutive_errors > 0:
                            shared_state["i2c_errors"] += 1
            
            # 3. Heartbeat [Spec 2.2]
            with state_lock:
                shared_state["core1_tick"] = time.ticks_ms()
            
            # 4. Loop Metrics [Spec 7.3]
            loop_end = time.ticks_us()
            loop_time = time.ticks_diff(loop_end, loop_start)
            
            # Accumulate max loop time continuously
            if loop_time > local_max_us:
                local_max_us = loop_time
                
            loop_accum += loop_time
            loop_count += 1
            
            # Sync to Shared State every 50 loops (approx 25ms)
            if loop_count >= 50:
                with state_lock:
                    shared_state["loop_avg_us"] = int(loop_accum / loop_count)
                    if local_max_us > shared_state["loop_max_us"]:
                        shared_state["loop_max_us"] = local_max_us
                
                loop_accum = 0
                loop_count = 0
                local_max_us = 0
            
            time.sleep_us(100)
            
        except Exception as e:
            with state_lock:
                shared_state["error_code"] = f"C1_CRASH:{e}"
            log.crit("SYS", f"Core 1 Crash: {e}")
            time.sleep(1)

# --- 5. CORE 0: THE CLERK (Comms & Logic) ---
def process_packet(uart, packet, log_traffic=False):
    """
    [Spec 4.3.1.1.A] Communication Logic Engine (The Clerk).
    Performs packet parsing and executes logical state transitions.
    Implements the Wipe Interceptor (Spec 4.3.1.1.D) for bandwidth-efficient 
    piggyback acknowledgement and command inventory execution (Spec 4.3.4).
    """
    # [RESILIENCY] Perform Deterministic GC at start of every processing cycle
    # to free memory from the previous transaction.
    gc.collect()

    # [RESILIENCY] Guard against memory exhaustion. If heap is critically low, 
    # abort complex processing to prevent a hard MemoryError.
    if not memory_guard(threshold_bytes=6144): # 6KB threshold for command handling
        log.warn("NET", "Dropping packet: RAM exhaustion protection active")
        return None

    global wdt 
    
    t_start = time.ticks_ms()
    response = None
    
    try:
        target, source, seq, m_type, payload = packet
        
        if log_traffic:
            try:
                p_hex = f"{m_type:02X}"
                p_load = payload.decode() if payload else "NONE"
            except:
                p_load = "BIN"
            log.debug("NET", f"[RX] T:{target} S:{source} Q:{seq} TY:{p_hex} P:{p_load}")

        # --- DEDUPLICATION (Spec 4.3.5) ---
        last_seq = packet_dedupe.get(source)
        if last_seq == seq:
            if log_traffic: log.debug("NET", f"[DEDUPE] Dropping Seq {seq} from {source}")
            return meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_ACK, str(seq))
        packet_dedupe[source] = seq

        # --- WIPE INTERCEPTOR (Piggyback |W=) [Spec 4.3.1.1.D] ---
        if m_type in [meowprotocol.MSG_TYPE_CMD, meowprotocol.MSG_TYPE_CMD_STS]:
            try:
                p_str = payload.decode()
                if "|W=" in p_str:
                    content, wipe_tag = p_str.split("|W=", 1)
                    for tok in wipe_tag.split(","):
                        tok = tok.strip()
                        if not tok:
                            continue
                        try:
                            ack_id = int(tok)
                            with state_lock:
                                if ack_id in shared_state["tx_safety"]:
                                    del shared_state["tx_safety"][ack_id]
                                elif ack_id in shared_state["tx_events"]:
                                    del shared_state["tx_events"][ack_id]
                        except:
                            pass
                    payload = content.encode()
            except: pass

        # --- EXPLICIT ACK (0x20) [Spec 4.3.6] ---
        if m_type == meowprotocol.MSG_TYPE_ACK:
            try:
                ack_id = int(payload.decode())
                with state_lock:
                    if ack_id in shared_state["tx_safety"]:
                        del shared_state["tx_safety"][ack_id]
                    elif ack_id in shared_state["tx_events"]:
                        del shared_state["tx_events"][ack_id]
                return None
            except: pass

        try:
            # --- STATUS REPORT - 0x11 [Spec 4.3.7] ---
            if m_type == meowprotocol.MSG_TYPE_CMD_STS:
                status_str = build_status_string()
                response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_STS, status_str)

            # --- STOP - 0x00 [Spec 4.3.4] ---
            elif m_type == meowprotocol.MSG_TYPE_CMD_STOP:
                if act_mgr: act_mgr.emergency_stop()
                with state_lock: shared_state["current_state"] = STATE_ERROR
                response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_ACK, str(seq))

            # --- COMMAND - 0x10 [Spec 4.3.4] ---
            elif m_type == meowprotocol.MSG_TYPE_CMD:
                cmd_str = payload.decode()
                if cmd_str == "IDLE":
                    with state_lock: shared_state["current_state"] = STATE_IDLE
                    if act_mgr: act_mgr.safe_stop()
                    response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_ACK, str(seq))
                elif cmd_str == "FLOW":
                    # [FIX] State Latch: Cannot enter FLOW from ERROR directly. Must Reset first.
                    if shared_state["current_state"] == STATE_ERROR:
                         response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_NAK_STATE, "MUST_RESET")
                    else:
                        with state_lock: shared_state["current_state"] = STATE_FLOW
                        response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_ACK, str(seq))
                elif cmd_str == "CALIBRATE":
                    with state_lock:
                        shared_state["recalibrate_requested"] = True
                    response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_ACK, str(seq))
                # SAVE CONFIG CMD
                elif cmd_str == "CFG:SAVE":
                    if save_config_atomic():
                        response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_ACK, str(seq))
                    else:
                        response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_NAK, "SAVE_FAIL")
                elif cmd_str.startswith("ACT:") and act_mgr:
                    content = cmd_str[4:]
                    try:
                        if '=' in content: name, val = content.split('=', 1)
                        else: name, val = content.split(':', 1)
                        act_mgr.set_target(name, float(val))
                    except: pass
                    response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_ACK, str(seq))
                else:
                    response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_ACK, str(seq))

            # --- QUERY CRASH LOG - 0x13 [Spec 12.6] ---
            elif m_type == meowprotocol.MSG_TYPE_CMD_LOG:
                log_content = ""
                log_exists = False
                
                try:
                    with open('crash.log', 'r') as f:
                        content = f.read()
                        if len(content) > 512:
                            log_content = content[-512:]
                        else:
                            log_content = content
                        log_exists = True
                except OSError:
                    pass 
                
                if log_exists:
                    response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_LOG, log_content)
                    try:
                        os.remove('crash.log')
                        log.info("SYS", "Crash Log Transmitted & Cleared")
                    except: pass
                else:
                    response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_ACK, "NO_LOG")

            # --- QUERY ACTUATORS - 0x15 [Spec 4.3.8] ---
            elif m_type == meowprotocol.MSG_TYPE_CMD_ACT:
                if act_mgr:
                    if hasattr(act_mgr, 'get_telemetry_string'):
                        data = act_mgr.get_telemetry_string().replace(':', '=')
                    else:
                        data = f"ACT_CNT={len(act_mgr.targets)}"
                    response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_ACT, data)

            # --- QUERY SENSORS - 0x16 [Spec 4.3.9] ---
            elif m_type == meowprotocol.MSG_TYPE_CMD_SNS:
                if sns_mgr:
                    if hasattr(sns_mgr, 'get_telemetry_string'):
                        data = sns_mgr.get_telemetry_string().replace(':', '=')
                    else:
                        data = "SNS_OK"
                    response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_SNS, data)
            
            # --- SYSTEM MANIFEST - 0x12 [Spec 12.4.3] ---
            elif m_type == meowprotocol.MSG_TYPE_CMD_VER:
                if sys_man:
                    data = sys_man.get_report_string()
                else:
                    data = "SYS_MAN=FAIL"
                response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_VRS, data)

            # --- QUERY CONFIG - 0x17 [Spec 4.3.11.3] ---
            elif m_type == meowprotocol.MSG_TYPE_CMD_CFG:
                try:
                    # [RESILIENCY] Protect against large JSON dump RAM spike
                    if memory_guard(10240):
                        dump_str = json.dumps(system_config)
                        response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_CFG, dump_str)
                    else:
                        response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_NAK_STATE, "LOW_RAM_CFG")
                except Exception as e:
                    response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_NAK_SYNTAX, "JSON_ERR")

            # --- LIVE TUNING - 0x18 [Spec 4.3.10] ---
            elif m_type == meowprotocol.MSG_TYPE_SET_CFG:
                response_type = meowprotocol.MSG_TYPE_NAK_SYNTAX
                response_payload = "ERR"

                cmd_str = payload.decode() if payload else ""
                
                if not act_mgr:
                    response_type = meowprotocol.MSG_TYPE_NAK_STATE
                    response_payload = "NO_ACT_MGR"
                elif not cmd_str.startswith("CFG:ACT:"):
                    response_type = meowprotocol.MSG_TYPE_NAK_SYNTAX
                    response_payload = "INVALID_FMT"
                else:
                    content = cmd_str[8:] 
                    try:
                        aid, rest = content.split(':', 1)
                        param, val = rest.split('=', 1)
                        
                        if aid in act_mgr.configs:
                            updated = False
                            if param == 'ramp_ms':
                                act_mgr.configs[aid]['ramp_ms'] = int(float(val))
                                updated = True
                            elif param == 'min_duty':
                                act_mgr.configs[aid]['min_duty'] = float(val)
                                updated = True
                            elif param == 'max_duty':
                                act_mgr.configs[aid]['max_duty'] = float(val)
                                updated = True
                            elif param == 'max_on_ms':
                                act_mgr.configs[aid]['max_on_ms'] = int(float(val))
                                updated = True
                            # [FIX] Added 'freq' to whitelist for atomic save workflow
                            elif param == 'freq':
                                act_mgr.configs[aid]['freq'] = int(float(val))
                                updated = True
                            
                            if updated:
                                # [FIX] Sync back to system_config so CFG:SAVE writes it to disk
                                for a_cfg in system_config.get('actuators', []):
                                    if a_cfg.get('id') == aid:
                                        a_cfg[param] = act_mgr.configs[aid][param]
                                        break
                                        
                                log.info("CFG", f"Updated {aid} {param}={val}")
                                response_type = meowprotocol.MSG_TYPE_ACK
                                response_payload = str(seq)
                                
                            else:
                                log.warn("CFG", f"Ignored unknown param: {param}")
                                response_type = meowprotocol.MSG_TYPE_NAK_SYNTAX 
                                response_payload = "UNKNOWN_PARAM"
                        else:
                            log.warn("CFG", f"Unknown Actuator {aid}")
                            response_type = meowprotocol.MSG_TYPE_NAK_SYNTAX
                            response_payload = "UNKNOWN_ID"      
                    except Exception as e:
                        log.error("CFG", f"Parse Fail: {e}")
                        response_type = meowprotocol.MSG_TYPE_NAK_SYNTAX
                        response_payload = "PARSE_ERR"
                response = meowprotocol.build_packet(source, shared_state["id"], seq, response_type, response_payload)
                
            # --- OTA START - 0x50 [Spec 4.3.14.4] ---
            elif m_type == meowprotocol.MSG_TYPE_OTA_START:
                with state_lock: shared_state["current_state"] = STATE_OTA
                if act_mgr: act_mgr.emergency_stop()
                
                try:
                    fid = payload[0]
                    chunks = 0
                    chk = ""
                    
                    if fid == 0xFF: 
                        n_len = payload[1] 
                        fname = payload[2 : 2+n_len].decode()
                        rem = payload[2+n_len:]
                        if len(rem) >= 66:
                            chunks = struct.unpack(">H", rem[:2])[0]
                            chk = rem[2:].decode()
                        else:
                            chunks = 1
                            chk = rem.decode()
                        ok, msg = ota_mgr.start_update(fname, chunks, chk)
                    else:
                        if len(payload) >= 67:
                            chunks = struct.unpack(">H", payload[1:3])[0]
                            chk = payload[3:].decode()
                        else:
                            chk = payload[1:].decode()
                        
                        ok, msg = ota_mgr.start_update(fid, chunks, chk)
                    
                    if ok:
                        log.warn("OTA", f"OTA START ({chunks} chunks) -> Locked State")
                        time.sleep(0.05)
                        response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_ACK, msg)
                    else:
                        response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_NAK_STATE, msg)
                except Exception as e:
                    log.error("OTA", f"Start Packet Parse Fail: {e}")
                    response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_NAK_SYNTAX, "PAR_ERR")

            # --- OTA DATA - 0x51 [Spec 4.3.14.3.1] ---
            elif m_type == meowprotocol.MSG_TYPE_OTA_DATA:
                if shared_state["current_state"] == STATE_OTA:
                    with state_lock: shared_state['ota_last_activity'] = time.ticks_ms()
                    ok, msg = ota_mgr.write_chunk(payload)
                    if ok and wdt: wdt.feed()
                    response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_ACK if ok else meowprotocol.MSG_TYPE_NAK, msg)
                else:
                    response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_NAK_STATE, "NOT_IN_OTA")
                    
            # --- OTA END - 0x52 [Spec 4.3.14.4 - Phase 3] ---
            elif m_type == meowprotocol.MSG_TYPE_OTA_END:
                if shared_state["current_state"] == STATE_OTA:
                    ok, msg = ota_mgr.verify_and_commit()
                    response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_ACK if ok else meowprotocol.MSG_TYPE_NAK, msg)
                    if ok:
                        time.sleep(0.5)
                        machine.reset()
                else:
                    response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_NAK_STATE, "NOT_IN_OTA")

            # --- OTA ABORT - 0x53 [Spec 4.3.14.3.1] ---
            elif m_type == meowprotocol.MSG_TYPE_OTA_ABORT:
                ota_mgr.abort()
                with state_lock: shared_state["current_state"] = STATE_IDLE
                log.warn("OTA", "OTA Aborted -> State IDLE")
                response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_ACK, "ABORTED")

            # --- RESET - 0x14 [Spec 4.3.4] ---
            elif m_type == meowprotocol.MSG_TYPE_CMD_RST:
                machine.reset()
            
            # --- TIME SYNC - 0x1A [Spec 4.3.13] ---
            elif m_type == 0x1A: # MSG_TYPE_CMD_SYNC_TIME
                # Acknowledge the time sync so Core 0 doesn't timeout
                response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_ACK, str(seq))

            # --- RAM-ONLY BAUD SHIFT - 0x1B [Spec 4.3.14] ---
            elif m_type == 0x1B: # MSG_TYPE_CMD_BAUD
                try:
                    new_baud = int(payload.decode('ascii'))
                    
                    # 1. Build and send the ACK at the CURRENT (old) speed
                    ack_pkt = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_ACK, str(seq))
                    uart.write(ack_pkt)
                    
                    # 2. Give the wire 50ms to physically transmit the bytes before the clock changes
                    time.sleep(0.05) 
                    
                    # 3. Shift the hardware UART instantly
                    uart.init(baudrate=new_baud)
                    log.warn("NET", f"Hardware shifted to {new_baud} bps (RAM Only)")
                    
                    # Prevent the outer function from returning a second response
                    response = None 
                except Exception as e:
                    response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_NAK_SYNTAX, "INVALID_BAUD")

        except Exception as e:
            response = meowprotocol.build_packet(source, shared_state["id"], seq, meowprotocol.MSG_TYPE_NAK_SYNTAX, "ERR")
            
    finally:
        # [FIX] Latency Measurement End
        lat = time.ticks_diff(time.ticks_ms(), t_start)
        with state_lock:
            # Windowed Accumulation (Rolling History)
            h = shared_state["lat_history"]
            h.append(lat)
            if len(h) > 50: # Keep last 50 samples
                h.pop(0)

    return response

def main():
    """
    [Spec 2.1] Core 0: The Clerk (Management Role).
    Handles high-level system logic, MTIP communication (Spec 4.0), and 
    Deterministic Garbage Collection (Spec 9.1). Serves as the primary loop 
    coordinator for the Distributed Symmetric Multiprocessing (SMP) architecture.
    """
    global act_mgr, sns_mgr, ota_mgr, sys_man, shared_state, system_config
    global adc_vsys, sensor_temp, wdt
    
    print("\n[APP] STARTING (UNMASKED VERSION)...")
    
    # 1. Init Hardware
    try:
        led = Pin("LED", Pin.OUT)
        led.on()
    except: led = None
    
    try:
        sensor_temp = machine.ADC(4)
        adc_vsys = machine.ADC(29) 
    except: pass
    
    # 2. Load Config [Spec 8.1]
    try:
        with open('config.json', 'r') as f:
            disk_cfg = json.load(f)
            system_config.update(disk_cfg)
            log.info("SYS", "Loaded config.json")
    except Exception as e:
        log.warn("SYS", f"Config Load Error: {e} (Using SAFE defaults)")

    shared_state["id"] = system_config["system"].get("device_id", 3)
    max_retries = system_config["comms"].get("max_retries", 5)
    
    test_cfg = system_config.get("testing", {})
    log_traffic = test_cfg.get("log_traffic", False)
    
    try:
        log.PRINT_TRAFFIC = log_traffic
    except AttributeError:
        pass
    
    # [STARTUP DIAGNOSTICS]
    log.info("APP-DIAG", f"Config Loaded. ID: {shared_state['id']}")
    log.info("APP-DIAG", f"Debug Settings -> Health:{test_cfg.get('health_print_interval_ms')}ms, Traffic:{log_traffic}")

    # 3. Init Subsystems (PARANOID ISOLATION & TRACEBACK CATCHER)
    log.info("SYS", "Initializing Subsystems...")
    
    import sys
    import io
    boot_errors = []
    
    try: 
        act_mgr = actuators.ActuatorManager(system_config)
    except Exception as e: 
        buf = io.StringIO(); sys.print_exception(e, buf)
        with open("crash.log", "w") as f: f.write(f"ACTUATOR FAIL:\n{buf.getvalue()}")
        log.crit("SYS", f"Actuator Init Fail: {e}")
        boot_errors.append(f"ACT_FAIL: {e}")

    try: 
        sns_mgr = sensors.SensorManager(system_config)
    except Exception as e: 
        buf = io.StringIO(); sys.print_exception(e, buf)
        with open("crash.log", "w") as f: f.write(f"SENSOR FAIL:\n{buf.getvalue()}")
        log.crit("SYS", f"Sensor Init Fail: {e}")
        boot_errors.append(f"SNS_FAIL: {e}")

    try: 
        ota_mgr = ota.OTAManager()
    except Exception as e: 
        log.crit("SYS", f"OTA Init Fail: {e}")
        boot_errors.append(f"OTA_FAIL: {e}")

    try: 
        sys_man = diagnostics.SystemManifest()
    except Exception as e: 
        log.crit("SYS", f"Manifest Init Fail: {e}")
        boot_errors.append(f"MAN_FAIL: {e}")

    # 4. Setup UART [Spec 4.3.1.1]
    u_bus = system_config["comms"].get("uart_bus", 0)
    tx_p = system_config["comms"].get("tx_pin", 0)
    rx_p = system_config["comms"].get("rx_pin", 1)
    baud = system_config["comms"].get("baud_rate", 115200)

    try:
        uart = machine.UART(u_bus, baudrate=baud, tx=Pin(tx_p), rx=Pin(rx_p), timeout=0)
        parser = meowprotocol.PacketParser(shared_state["id"])
        log.info("NET", "UART OK")
        log.info("APP-DIAG", f"UART OK (Bus {u_bus}, {baud})")
    except Exception as e:
        log.error("NET", f"UART FAIL: {e}")
        return 

    # 5. Start Core 1 [Spec 9.14]
    _thread.stack_size(8192) 
    _thread.start_new_thread(core1_task, ())
    
    # 6. Main Loop [Spec 4.3.1]
    log.info("SYS", f"=== PICO {shared_state['id']} ONLINE ===")
    
    if test_cfg.get("standalone_mode", False):
        log.warn("SYS", "STANDALONE MODE ACTIVE - Network Interlocks Disabled")
        
    wdt = WDT(timeout=8000)

    # --- PHASE 2: DEGRADED BOOT ALARM ---
    if boot_errors:
        with state_lock: 
            shared_state["current_state"] = STATE_ERROR
            shared_state["error_code"] = str(boot_errors[0])[:32]
        log.crit("SYS", "BOOT DEGRADED - Entering ERROR State")
    else:
        with state_lock: 
            shared_state["current_state"] = STATE_IDLE
    
    last_health_print = time.ticks_ms()
    last_sensor_print = time.ticks_ms()
    last_act_print = time.ticks_ms()
    last_debug_stream = time.ticks_ms()
    last_led_toggle = time.ticks_ms()
    last_gc = time.ticks_ms()
    
    last_valid_rx_time = time.ticks_ms() # NEW: Network Watchdog Timer
    last_error_code_sent = "NONE" 

    while True:
        try:
            t_ms = time.ticks_ms()

            # --- DEAD MAN'S SWITCH (Spec 2.2) ---
            # [NOTE] During OTA, Core 1 is paused, so shared_state["core1_tick"] stops updating.
            # However, Core 0 feeds the WDT in the OTA_DATA handler, so we skip this check during OTA.
            if shared_state["current_state"] != STATE_OTA:
                with state_lock:
                    c1_lag = time.ticks_diff(t_ms, shared_state["core1_tick"])
                
                # [FIX] Increased threshold to 250ms to allow safe MicroPython Garbage Collection
                if c1_lag < 250:
                    wdt.feed()
                else:
                    log.crit("SYS", f"Core 1 Freeze ({c1_lag}ms) - STARVING WDT")
            
            check_brownout()

            # --- SMART LED (Spec 3.0) ---
            blink_rate = 500 # IDLE (1Hz = 500ms toggle)
            with state_lock:
                curr_state = shared_state["current_state"]
                if curr_state == STATE_FLOW: blink_rate = 250 # FLOW (2Hz = 250ms toggle)
                elif curr_state == STATE_ERROR: blink_rate = 100 # ERROR (5Hz = 100ms toggle)
                elif curr_state == STATE_OTA: blink_rate = 100
            
            if led and time.ticks_diff(t_ms, last_led_toggle) > blink_rate:
                led.toggle()
                last_led_toggle = t_ms
            
            # --- DETERMINISTIC GC (Spec 9.1.B) ---
            if curr_state == STATE_IDLE:
                if time.ticks_diff(t_ms, last_gc) > 1000:
                    gc.collect()
                    last_gc = t_ms

            # --- OTA TIMEOUT WATCHDOG ---
            if shared_state["current_state"] == STATE_OTA:
                if 'ota_last_activity' not in shared_state:
                    shared_state['ota_last_activity'] = t_ms
                
                if time.ticks_diff(t_ms, shared_state['ota_last_activity']) > 10000:
                    log.error("OTA", "Session timed out! Forcing abort.")
                    if ota_mgr: ota_mgr.abort()
                    with state_lock: 
                        shared_state["current_state"] = STATE_IDLE
                        if 'ota_last_activity' in shared_state:
                            del shared_state['ota_last_activity']

            # --- ASYNC ALARM GENERATION (Spec 4.3.6.1) ---
            
            if shared_state["current_state"] == STATE_ERROR and shared_state["error_code"] != "NONE":
                if shared_state["error_code"] != last_error_code_sent:
                    send_reliable(uart, 0, meowprotocol.MSG_TYPE_ALARM, shared_state["error_code"])
                    last_error_code_sent = shared_state["error_code"]
                    if log_traffic: log.debug("NET", f"[TX ALARM] {shared_state['error_code']}")

            # --- ASYNC EVENT GENERATION (Core 1 -> Core 0 Queue) ---
            events_to_send = []
            with state_lock:
                if shared_state["pending_events"]:
                    events_to_send = list(shared_state["pending_events"])
                    shared_state["pending_events"].clear()

            for payload in events_to_send:
                send_reliable(uart, 0, meowprotocol.MSG_TYPE_EVT, payload)
                if log_traffic: log.debug("NET", f"[TX EVT] {payload}")

            # --- BOOT STABILITY CHECK (Spec 1.0) ---
            
            if not shared_state["boot_attempts_cleared"]:
                grace_ms = system_config.get("app", {}).get("boot_grace_ms", 3000)
                if (time.time() - shared_state["uptime_start"]) * 1000 > grace_ms:
                    if clear_boot_attempts():
                        shared_state["boot_attempts_cleared"] = True

            if shared_state["error_code"].startswith("C1_CRASH"):
                if shared_state["current_state"] != STATE_ERROR:
                    with state_lock: shared_state["current_state"] = STATE_ERROR
                    if act_mgr: act_mgr.emergency_stop()
                    log.crit("SYS", "Core 1 Crash -> State ERROR")

            # --- RETRY MONITOR (Spec 4.3.6.2) ---
            def process_queue(queue_dict, queue_name):
                to_delete = []
                trigger_err = False
                
                with state_lock:
                    items = list(queue_dict.items()) 
                
                for seq_id, item in items:
                    if time.ticks_diff(t_ms, item["ts"]) > 200:
                        if item["retries"] < max_retries:
                            uart.write(item["pkt"])
                            with state_lock:
                                if seq_id in queue_dict:
                                    queue_dict[seq_id]["ts"] = t_ms
                                    queue_dict[seq_id]["retries"] += 1
                            if log_traffic: log.debug("NET", f"[TX-RETRY] {queue_name} ID:{seq_id}")
                        else:
                            log.crit("NET", f"Max Retries Exceeded {queue_name} ID {seq_id}")
                            to_delete.append(seq_id)
                            trigger_err = True
                        
                
                if to_delete:
                    with state_lock:
                        for sid in to_delete:
                            if sid in queue_dict: del queue_dict[sid]
                return trigger_err

            err_safe = process_queue(shared_state["tx_safety"], "SAFE")
            err_evt = process_queue(shared_state["tx_events"], "EVT")

            if err_safe or err_evt:
                 with state_lock:
                    if shared_state["error_code"] != "Events not confirmed":
                        shared_state["current_state"] = STATE_ERROR
                        shared_state["error_code"] = "Events not confirmed"
                        if act_mgr: act_mgr.emergency_stop()
                        log.crit("NET", "State -> ERROR : Events not confirmed")

            # --- TRAFFIC GATING ---
            queues_empty = (len(shared_state["tx_safety"]) == 0 and len(shared_state["tx_events"]) == 0)

            # --- DEBUG PRINTS ---
            if True: 
                h_int = test_cfg.get("health_print_interval_ms", 0)
                if h_int > 0 and time.ticks_diff(t_ms, last_health_print) > h_int:
                    log.debug("DBG-STS", build_status_string())
                    last_health_print = t_ms

                s_int = test_cfg.get("sensor_print_interval_ms", 0)
                if s_int > 0 and sns_mgr and time.ticks_diff(t_ms, last_sensor_print) > s_int:
                    if hasattr(sns_mgr, 'get_telemetry_string'):
                        log.debug("DBG-SNS", sns_mgr.get_telemetry_string())
                    else:
                        log.debug("DBG-SNS", str(sns_mgr.read_all()))
                    last_sensor_print = t_ms

                a_int = test_cfg.get("actuator_print_interval_ms", 0)
                if a_int > 0 and act_mgr and time.ticks_diff(t_ms, last_act_print) > a_int:
                    if hasattr(act_mgr, 'current'):
                        log.debug("DBG-ACT", f"Targets: {act_mgr.targets} Current: {act_mgr.current}")
                    else:
                        log.debug("DBG-ACT", "Active")
                    last_act_print = t_ms

                d_int = test_cfg.get("debug_stream_ms", 0)
                if d_int > 0 and time.ticks_diff(t_ms, last_debug_stream) > d_int:
                    log.debug("DBG", f"Heartbeat Tick: {t_ms}")
                    last_debug_stream = t_ms

            if not shared_state["lives_refilled"]:
                if time.time() - shared_state["uptime_start"] > 10:
                    if refill_lives():
                        shared_state["lives_refilled"] = True
            
            # --- RX LOOP (Greedy Ingestion & Priority Sorting) [Spec 4.3.1.1.A] ---
            incoming_batch = []
            while uart.any():
                chunk = uart.read()
                if not chunk: break
                
                # Accumulate all packets currently in buffer
                incoming_batch.extend(parser.parse_stream(chunk))
            
            if incoming_batch:
                # [RESILIENCY] Pre-Ingestion Flush: Clear heap before allocating 
                # objects for a large incoming batch.
                gc.collect()

                # Sort: Safety (0x00/0x48) > Command (0x10) > Data
                # Packet tuple: (target, source, seq, m_type, payload)
                def get_prio(p):
                    mt = p[3]
                    if mt == meowprotocol.MSG_TYPE_CMD_STOP or mt == meowprotocol.MSG_TYPE_ALARM: return 0 # Critical
                    if mt == meowprotocol.MSG_TYPE_CMD: return 1 # Command
                    return 2 # Bulk Data
                
                incoming_batch.sort(key=get_prio)

                for pkt in incoming_batch:
                    last_valid_rx_time = t_ms # NEW: Feed the Network Watchdog
                    resp = process_packet(uart, pkt, log_traffic=log_traffic)
                    if resp:
                        uart.write(resp)
                        if log_traffic:
                            try:
                                log.debug("TX", resp.decode())
                            except:
                                log.debug("TX", str(resp))
            
            # --- NEW: AUTONOMOUS NETWORK WATCHDOG ---
            # If we haven't received a valid packet in 60 seconds, we are deaf.
            # Intentionally crash the loop so boot.py deducts a life.
            if test_cfg.get("network_watchdog_enabled", True) and not test_cfg.get("standalone_mode", False):
                if time.ticks_diff(t_ms, last_valid_rx_time) > 60000:
                    raise Exception("Network Timeout - Deaf to RS485")
                        
            time.sleep_ms(1)
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.crit("SYS", f"Loop Crash: {e}")
            time.sleep(1)

if __name__ == "__main__":
    main()