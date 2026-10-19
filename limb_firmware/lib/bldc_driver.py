# bldc_driver.py - v1.02 Driver for 36PG-3650BL Conveyor Motor 
# CHANGES: Added Runtime Logging for Direction and Start/Stop events.
#          Wrapped deinit in try/except for crash safety (Spec Compliance).

"""
[Spec 10.9] Specialized Driver Standards (HAL Extensions).
The BLDCDriver acts as a "Humble Component" (Spec 10.9.1), responsible for 
the physical translation of intent into voltage for conveyor motors.
It manages PWM speed control and GPIO direction logic without internal timing loops.
"""

import machine
import lib.logging as log

class BLDCDriver:
    """
    [Spec 10.9.1] The "Humble Component" Pattern.
    Specialized hardware driver for Brushless DC motors. Handles hardware-specific 
    mapping and directional state memory.
    """
    def __init__(self, pwm_pin_num, dir_pin_num, invert_dir=False, min_duty=0.0, max_duty=1.0, freq=20000):
        """
        [Spec 10.9.5] Configuration Whitelisting.
        Initializes the physical hardware pins. Sets the carrier frequency 
        to 20kHz (Standard for BLDC) and establishes deadzone floor/ceiling 
        parameters (Spec 10.7.1).
        """
        try:
            # 1. Setup PWM (Speed)
            self.pwm_pin = machine.Pin(pwm_pin_num, machine.Pin.OUT)
            self.pwm = machine.PWM(self.pwm_pin)
            self.pwm.freq(freq)
            self.pwm.duty_u16(0)
            
            # 2. Setup Direction
            self.dir_pin = machine.Pin(dir_pin_num, machine.Pin.OUT)
            self.dir_invert = invert_dir
            self._current_dir_val = -1     # <--- MOVED UP: Initialize memory BEFORE using it!
            self.set_direction(False) # Default Forward
            
            # 3. Parameters
            self.min_u16 = int(min_duty * 65535)
            self.max_u16 = int(max_duty * 65535)
            self.current_val = 0.0
            self.last_state_moving = False # [Spec 10.9.3] State tracking for logging
            
            # Validation
            if self.max_u16 < self.min_u16:
                log.warn("ACT", f"BLDC config error: Max < Min. Defaulting Max to 100%.")
                self.max_u16 = 65535

            log.info("ACT", f"BLDC Init: PWM={pwm_pin_num} DIR={dir_pin_num} F={freq}Hz")
            
        except Exception as e:
            # [Spec 10.9.4] Error Propagation
            log.error("ACT", f"BLDC Init Fail: {e}")
            raise e

    def set_speed(self, value):
        """
        [Spec 10.9.2] Mandatory Interface: set_speed(value).
        Accepts normalized float 0.0 to 1.0. Maps logic to hardware registers 
        while enforcing state memory logging (Spec 10.9.3) to prevent bus spam.
        """
        val = max(0.0, min(1.0, float(value)))
        self.current_val = val
        
        # [Spec 10.9.3] State Change Logging (Anti-Spam)
        is_moving = val > 0.01
        if is_moving != self.last_state_moving:
            if is_moving:
                log.debug("ACT", f"BLDC Start ({val:.2f})")
            else:
                log.debug("ACT", "BLDC Stop")
            self.last_state_moving = is_moving

        if val <= 0.01:
            self.pwm.duty_u16(0)
        else:
            # [Spec 10.7.1] Deadzone mapping (min_duty to max_duty)
            span = self.max_u16 - self.min_u16
            duty = self.min_u16 + int(val * span)
            self.pwm.duty_u16(duty)

    def set_direction(self, reverse):
        """
        [Spec 10.9.3] Directional & State Memory.
        Filters direction requests by comparing against software memory,
        protecting against physical voltage sag / EMI noise on the OUT pad.
        """
        logic = not reverse if self.dir_invert else reverse
        new_pin_val = 1 if logic else 0
        
        # Compare against software memory, NEVER physical pad voltage
        if self._current_dir_val != new_pin_val:
            self.dir_pin.value(new_pin_val)
            self._current_dir_val = new_pin_val
            dir_str = "REV" if reverse else "FWD"
            log.info("ACT", f"BLDC Dir: {dir_str}")

    def stop(self):
        """
        [Spec 10.9.2] Mandatory Interface: stop().
        Immediate hardware halt. Bypasses internal ramping to ensure 
        failsafe operation during emergency events.
        """
        self.set_speed(0)

    def deinit(self):
        """
        [Spec 10.9.2] Mandatory Interface: deinit().
        Releases PWM resources and grounds outputs. Wrapped in try/except 
        (Spec 10.9.2 - Safety) to prevent cleanup crashes.
        """
        try:
            self.pwm.duty_u16(0)
            self.pwm.deinit()
            log.info("ACT", "BLDC Deinit")
        except Exception as e:
            # [Spec 10.9.4] Error Propagation
            log.error("HW", f"BLDC Deinit Error: {e}")