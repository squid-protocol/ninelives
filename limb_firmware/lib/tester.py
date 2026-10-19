# lib/tester.py - Hardware Validation Sequencer v1.00
# CHANGES: Replaced print() with lib.logging (Tag: TST)

"""
[Spec 14.2.7] Hardware Validation Sequencer (tester.py).
Provides a standardized sequence for verifying physical wiring and component 
functionality. Operates within the STATE_COMPONENT_TEST (Spec 3.0) context, 
sequentially exercising actuators to identify hardware-level failures during 
QA or post-maintenance validation.
"""

import time
import lib.logging as log 

def run_sequence(act_mgr, mailbox, telemetry, data_lock):
    """
    [Spec 14.2.7] Executes the automated hardware test sequence.
    Iterates through all configured actuators, performing ramp tests for 
    variable-speed devices (BLDC/PWM) and pulse tests for binary devices (Solenoids).
    Maintains the system heartbeat (Spec 2.2) to prevent Watchdog resets during 
    the long-running test loop.
    """
    log.info("TST", "Sequence Start")
    
    # 1. Get sorted list of actuators
    ids = sorted(act_mgr.configs.keys())
    
    last_tick = time.ticks_ms()

    # --- Helper: Wait loop that keeps the physics engine running ---
    def wait_and_update(duration_ms):
        """
        [Spec 10.6.3] Embedded Physics Loop.
        Ensures that real-time physics (ramping) and watchdog heartbeats 
        (Spec 2.2) continue to process while the test sequence is in a 
        waiting state. Monitors for user-initiated abort requests via 
        state transition logic (Spec 3.1).
        """
        nonlocal last_tick
        end_time = time.ticks_add(time.ticks_ms(), int(duration_ms))
        
        while time.ticks_diff(end_time, time.ticks_ms()) > 0:
            now = time.ticks_ms()
            dt = time.ticks_diff(now, last_tick)
            last_tick = now
            
            # 1. Run Physics (Ramping, etc.) [Spec 10.6.3]
            act_mgr.update(dt)
            
            # 2. Feed Watchdog (Heartbeat) [Spec 2.2]
            with data_lock:
                telemetry["last_tick_seen"] = time.ticks_us()
            
            # 3. Check Abort Condition [Spec 3.1]
            if mailbox["system_state"] != "TESTING":
                log.warn("TST", "Sequence Aborted by User")
                return False 
                
            time.sleep_ms(5)
        return True

    # --- Main Sequence ---
    try:
        step_count = 0
        for aid in ids:
            # Check abort before starting next device
            if mailbox["system_state"] != "TESTING": break

            cfg = act_mgr.configs[aid]
            atype = cfg['type']
            
            step_count += 1
            log.info("TST", f"Step {step_count}: Testing {aid} ({atype})")
            
            if atype in ["BLDC", "PWM", "VIBE_DRIVER"]:
                # 1. Ramp UP [Spec 10.7.5]
                act_mgr.set_target(aid, 1.0)
                
                # Calculate time: Ramp Time + 2s Hold (Reduced from 10s for speed)
                ramp_ms = cfg.get('ramp_ms', 1000)
                total_wait = ramp_ms + 2000 
                
                if not wait_and_update(total_wait): break
                
                # 2. Ramp DOWN [Spec 10.7.5]
                act_mgr.set_target(aid, 0.0)
                if not wait_and_update(ramp_ms + 500): break

            elif atype in ["SOLENOID", "DIGITAL"]:
                # 1. Fire ON [Spec 10.7.7]
                act_mgr.set_target(aid, 1.0)
                if not wait_and_update(500): break # 0.5s Pulse
                
                # 2. Fire OFF [Spec 10.7.7]
                act_mgr.set_target(aid, 0.0)
                if not wait_and_update(200): break

        log.info("TST", "Sequence PASS")

    except Exception as e:
        # [Spec 10.5.1] Emergency Halt on logic crash
        log.error("TST", f"FAIL: {e}")
        
    finally:
        # [Spec 10.5.1] Ensure everything is off (Emergency Stop)
        act_mgr.emergency_stop(silent=True)
        
        # [Spec 3.1] Return to IDLE if we finished naturally
        with data_lock:
            if mailbox["system_state"] == "TESTING":
                mailbox["system_state"] = "IDLE"