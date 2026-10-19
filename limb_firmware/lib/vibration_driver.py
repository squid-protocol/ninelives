# vibration_driver.py - v1.02
# STANDARD: Ninelives Shell v1.00

"""
[Spec 10.9] Specialized Driver Standards (HAL Extensions).
Hardware driver for vibratory motors. Following the "Humble Component" pattern 
(Spec 10.9.1), it provides the physical interface for the Kinetic Verification 
Strategy (Spec 16.3.2), translating logical intent into high-frequency PWM 
resonance.
"""

from machine import Pin, PWM
import lib.logging as log

class VibrationDriver:
    """
    [Spec 10.9.1] Humble Component Driver.
    Manages a single PWM channel for kinetic vibration. Relies on the parent 
    ActuatorManager for timing and ramping (Spec 10.6.3).
    """
    def __init__(self, pin_id, freq=100, min_duty=0, max_duty=65535):
        """
        [Spec 10.9.5] Configuration Whitelisting.
        Initializes the PWM hardware and handles the auto-scaling logic 
        between logical floats and physical 16-bit integers.
        """
        self.pin_id = pin_id
        self.freq = freq
        
        # [FIX] AUTO-SCALING LOGIC
        # Actuators.py passes floats (0.0-1.0), but defaults are ints (65535).
        # We normalize everything to 16-bit integers (0-65535) here.
        if max_duty <= 1.0:
            self.max_duty = int(max_duty * 65535)
        else:
            self.max_duty = int(max_duty)

        if min_duty <= 1.0 and min_duty > 0: # Ensure we don't scale 0 passed as int
            self.min_duty = int(min_duty * 65535)
        else:
            self.min_duty = int(min_duty)
        
        # [Spec 10.9.3] State tracking for anti-spam logging
        self._last_speed = 0.0

        if self.max_duty < self.min_duty:
            log.warn("CFG", f"Vibe Config Error: Max({self.max_duty}) < Min({self.min_duty})")

        try:
            self.pwm = PWM(Pin(pin_id))
            self.pwm.freq(self.freq)
            self.pwm.duty_u16(0)
            log.info("HW", f"Vibe Init: Pin={pin_id}, Freq={freq}, Range=[{self.min_duty}-{self.max_duty}]")
        except Exception as e:
            # [Spec 10.9.4] Error Propagation
            log.error("HW", f"Vibe Init Failed: {e}")
            raise e

    def set_freq(self, freq):
        """
        [Spec 10.3.1] Hardware Tuning.
        Updates the resonant frequency of the motor. Used for mechanical 
        optimization of the kinetic feedback loop.
        """
        try:
            self.freq = freq
            self.pwm.freq(self.freq)
            log.info("CFG", f"Vibe Freq Set: {freq}")
        except Exception as e:
            log.warn("CFG", f"Vibe Freq Fail: {e}")

    def set_speed(self, speed_percent):
        """
        [Spec 10.9.2] Mandatory Interface: set_speed(value).
        Sets vibration intensity (0.0 to 1.0). Maps logical range to physical 
        16-bit duty cycle while enforcing state-change logging (Spec 10.9.3).
        """
        speed_percent = max(0.0, min(1.0, speed_percent))

        # [Spec 10.9.3] LOGGING STRATEGY (Anti-Spam)
        if speed_percent == 0.0 and self._last_speed > 0.0:
            log.debug("ACT", "Vibe Stop")
        elif speed_percent > 0.0 and self._last_speed == 0.0:
            log.debug("ACT", f"Vibe Start: {speed_percent:.2f}")
        
        self._last_speed = speed_percent

        # [FIX] Math is now performed in 16-bit Integer Space
        duty_span = self.max_duty - self.min_duty
        duty_val = int(self.min_duty + (duty_span * speed_percent))

        # Hard cut-off for 0 speed
        if speed_percent == 0.0:
            duty_val = 0

        self.pwm.duty_u16(duty_val)

    def stop(self):
        """
        [Spec 10.9.2] Mandatory Interface: stop().
        Immediate hardware halt. Bypasses internal ramping to ensure 
        failsafe operation during emergency events.
        """
        self.set_speed(0.0)

    def deinit(self):
        """
        [Spec 10.9.2] Mandatory Interface: deinit().
        Releases hardware resources (PWM slice) and grounds the output pin. 
        Wrapped in safety handlers (Spec 10.9.2).
        """
        try:
            self.pwm.duty_u16(0)
            self.pwm.deinit()
            log.info("ACT", "Vibe Deinit")
        except Exception as e:
            # [Spec 10.9.4] Error Propagation
            log.error("HW", f"Vibe Deinit Error: {e}")