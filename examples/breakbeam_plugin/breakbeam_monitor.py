# breakbeam_monitor.py - Pluggable Statistical Breakbeam & Metrology Plugin
# PURPOSE: Isolates Meow-Turtle breakbeam tripwire and part metrology from the generic Ninelives Limb Kernel.

"""
[Spec 16.1] Statistical Breakbeam & Spatial Birth Certificate Plugin.
Implements Asymmetric Gated EMA, 20-sample noise-floor calibration, and
temporal debounce for high-speed part singulation using a TSL2591 light sensor
and PIO pulse counter.
"""

import time

try:
    import lib.logging as log
except ImportError:
    class _FallbackLog:
        def info(self, t, m): print(f"[INFO] [{t}] {m}")
        def warn(self, t, m): print(f"[WARN] [{t}] {m}")
        def error(self, t, m): print(f"[ERROR] [{t}] {m}")
    log = _FallbackLog()


class BreakbeamMonitor:
    """
    Stateful breakbeam metrology processor run inside Core 1 (The Machinist).
    Only activates on limbs where a `MAIN_TSL` (or configured sensor key) is present.
    """

    def __init__(self, system_config, sensor_key="MAIN_TSL", odometer_key="global_tick_TOTAL"):
        tuning = system_config.get("tuning", {})
        self.sensor_key = sensor_key
        self.odometer_key = odometer_key

        self.beam_min_sigma = tuning.get("beam_min_sigma", 15.0)
        self.beam_alpha = tuning.get("beam_alpha", 0.001)
        self.beam_gate_sigma = tuning.get("beam_downward_gate_sigma", 1.5)
        self.beam_trip_sigma = tuning.get("beam_tripwire_sigma", 6.0)
        self.beam_sustain_ms = tuning.get("beam_sustain_ms", 80)

        self.beam_mean = 0.0
        self.beam_threshold = 0.0
        self.beam_stddev = 0.0
        self.beam_break_start_ms = 0

        self.calibrating = True
        self.calib_samples = []
        self.was_broken = False
        self.start_ms = 0
        self.start_pulse = 0

    def reset_calibration(self):
        """Triggers a fresh 20-sample calibration window."""
        self.calib_samples = []
        self.calibrating = True
        self.beam_break_start_ms = 0
        self.was_broken = False

    def update(self, sensor_data, sns_mgr=None):
        """
        Processes the latest sensor_data dictionary on Core 1.
        Returns a formatted MTIP event payload string when a part exits the beam,
        or None if no event occurred.
        """
        tsl_val = sensor_data.get(self.sensor_key)
        if tsl_val is None or tsl_val <= 0:
            return None

        # 1. Startup / Requested Calibration Phase (20 Samples)
        if self.calibrating:
            self.calib_samples.append(tsl_val)
            if len(self.calib_samples) >= 20:
                self.beam_mean = sum(self.calib_samples) / 20.0
                variance = sum((x - self.beam_mean) ** 2 for x in self.calib_samples) / 20.0
                self.beam_stddev = variance ** 0.5

                dynamic_noise_floor = max(
                    self.beam_stddev, self.beam_min_sigma, self.beam_mean * 0.05
                )
                self.beam_threshold = self.beam_mean - (
                    self.beam_trip_sigma * dynamic_noise_floor
                )

                log.info(
                    "SEN",
                    f"Beam Calibrated. Mean: {int(self.beam_mean)}, StdDev: {int(self.beam_stddev)}, Thresh: {int(self.beam_threshold)}",
                )

                if sns_mgr:
                    with sns_mgr.lock:
                        sns_mgr.last_results["BEAM_MEAN"] = int(self.beam_mean)
                        sns_mgr.last_results["BEAM_THRESH"] = int(self.beam_threshold)

                self.calibrating = False
            return None

        # 2. Active Run Phase
        dynamic_noise_floor = max(
            self.beam_stddev, self.beam_min_sigma, self.beam_mean * 0.05
        )
        beam_gate_floor = self.beam_mean - (self.beam_gate_sigma * dynamic_noise_floor)
        self.beam_threshold = self.beam_mean - (
            self.beam_trip_sigma * dynamic_noise_floor
        )

        # --- ASYMMETRIC GATED EMA ---
        if tsl_val > self.beam_mean or tsl_val >= beam_gate_floor:
            self.beam_mean = (tsl_val * self.beam_alpha) + (
                self.beam_mean * (1.0 - self.beam_alpha)
            )
            if sns_mgr:
                with sns_mgr.lock:
                    sns_mgr.last_results["BEAM_MEAN"] = int(self.beam_mean)
                    sns_mgr.last_results["BEAM_THRESH"] = int(self.beam_threshold)

        # --- TEMPORAL TRIPWIRE LOGIC ---
        now_ms = time.ticks_ms()
        if tsl_val < self.beam_threshold:
            if self.beam_break_start_ms == 0:
                self.beam_break_start_ms = now_ms
        else:
            self.beam_break_start_ms = 0

        is_broken = False
        if self.beam_break_start_ms > 0:
            if time.ticks_diff(now_ms, self.beam_break_start_ms) >= self.beam_sustain_ms:
                is_broken = True

        # Falling & Rising Edge Metrology Logic
        if is_broken and not self.was_broken:
            self.was_broken = True
            self.start_ms = now_ms
            self.start_pulse = sensor_data.get(self.odometer_key, 0)
            return None

        if not is_broken and self.was_broken:
            self.was_broken = False
            end_ms = now_ms
            end_pulse = sensor_data.get(self.odometer_key, 0)

            length_ms = time.ticks_diff(end_ms, self.start_ms)
            center_ms = self.start_ms + (length_ms // 2)

            length_pulses = end_pulse - self.start_pulse
            center_pulse = self.start_pulse + (length_pulses // 2)

            log.info(
                "SEN",
                f"PART MEASURED! CenterPulse: {center_pulse}, Duration: {length_ms}ms",
            )
            return f"PART_DETECTED,center_pulse={center_pulse},center_ms={center_ms},len_ms={length_ms}"

        return None
