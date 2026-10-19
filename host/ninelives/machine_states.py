# lib/machine_states.py - Standardized State Constants (v5.30)
# PURPOSE: Defines the allowable operational modes for the System and Picos.
# COMPLIANCE: Core 0 Spec Sections 7.1, 13.1, and MTIP Wire Format 4.3.8.
# ROLE: Pure definition file. No logic or imports allowed.

"""
[Spec 13.1] Ninelives Standardized Machine States.

This module serves as the central registry for all state constants used across
the Ninelives architecture. It ensures that the Digital Twin, System Coordinator,
and hardware fleet share a unified language for operational modes, verification,
and safety severities.

Sections:
1. System States (Spec 13.1): High-level operational modes of the RP5 Brain.
2. Connection States (Spec 20.0): Connectivity status within the Digital Twin.
3. Pico States (Spec 13.1): Reported states from the distributed hardware nodes.
4. Verification Spectrum (Spec 3.2): Actuator confirmation badges for closed-loop logic.
5. Operational Profiles (Spec 14.0): Master keys for sorting 'Recipes'.
6. Alarm Severities (Spec 18.3): Priority levels for the system-wide safety manager.
"""

# ==============================================================================
# SECTION 1: SYSTEM STATES (Global Logic)
# ==============================================================================
# Defines the high-level operational mode of the RP5 Core 0 (Spec 13.1).

STATE_BOOT = "BOOT"  # [Spec 7.1] Startup & Fleet Handshake
STATE_IDLE = "IDLE"  # Powered, Verified, Safe (Actuators OFF)
STATE_DEV = "DEV"  # [Spec 13.1] Manual Overrides & Tuning Enabled
STATE_ARMING = "ARMING"  # [Spec 7.2] Applying Profile -> Transitioning to READY
STATE_READY = "READY"  # Fleet ACKed config, awaiting FLOW trigger
STATE_FLOW = "FLOW"  # [Spec 13.1] Run Mode: Kinetic Sorting Active
STATE_ERROR = "ERROR"  # [Spec 15.1] E-STOP / Critical Fault Lockout

# --- 1.2 SPECIAL & MAINTENANCE MODES ---
STATE_OTA = "OTA"  # [Spec 14.5] Firmware Update transaction in progress
STATE_GHOST = "GHOST"  # [Spec 14.5.3] Partner Simulation / Safe Mode
STATE_TESTING = "TESTING"  # [Spec 12.5] STATE_COMPONENT_TEST: Calibration Routine

# ==============================================================================
# SECTION 2: CONNECTION STATES
# ==============================================================================
# Defines the connectivity status of a Limb in the Digital Twin (Spec 20).

STATE_OFFLINE = "OFFLINE"  # [Spec 15.1] Link lost or Watchdog timed out
STATE_LINKED = "LINKED"  # Physical link detected; handshake incomplete

# ==============================================================================
# SECTION 3: PICO STATES (Remote)
# ==============================================================================
# Target and Reported states for the Ninelives.shell firmware (Spec 13.1).

PICO_STATE_BOOT = "BOOT"
PICO_STATE_IDLE = "IDLE"
PICO_STATE_DEV = "DEV"
PICO_STATE_READY = "READY"
PICO_STATE_FLOW = "FLOW"
PICO_STATE_TESTING = "TESTING"
PICO_STATE_ERR = "ERROR"

# ==============================================================================
# SECTION 4: VERIFICATION SPECTRUM (Spec 3.2 & 4.3.8)
# ==============================================================================
# Distinguishes between 'Commanded Intent' and 'Reported Reality'.
# Single-character tags match the MTIP Wire Format (Section 4.3.8).

# 4.1 Unverified
ACT_UNMEASURED = "U"  # Open Loop: Command issued, no feedback yet
ACT_VERIFYING = "V"  # [Spec 3.2] Transient: Confirming frequency/pulse

# 4.2 Confirmed
ACT_CONFIRMED_ON = "ON"  # [Spec 3.2] Closed Loop: Sensors prove activity
ACT_CONFIRMED_OFF = "OFF"  # [Spec 3.2] Closed Loop: Sensors prove silence

# 4.3 Faults (Critical)
ACT_FAULT_STALL = "F_STL"  # [Spec 15.5] Command ON, Sensor OFF (Jam/Stall)
ACT_FAULT_RUNAWAY = "F_RUN"  # [Spec 15.5] Command OFF, Sensor ON (Circuit Short)
ACT_FAULT_THERMAL = "F_HOT"  # [Spec 15.4] Hardware Silicon Fuse Tripped

# ==============================================================================
# SECTION 5: OPERATIONAL PROFILES (Recipes)
# ==============================================================================
# Defined in config/profiles.json, utilized by app.py (Spec 14).

PROFILE_SCAN = "SCAN_NEW_BUCKET"  # [Spec 12.1] Cataloging Mode
PROFILE_SORT = "PRECISION_SORT"  # [Spec 12.2] High-Accuracy Sorting
PROFILE_AUDIT = "CONFIRM_INVENTORY"  # [Spec 12.4] Verification Mode

# ==============================================================================
# SECTION 6: ALARM SEVERITIES (Safety Levels)
# ==============================================================================
# Mapped from Pico Log Levels in alarm_manager.py (Spec 18.3).

SEVERITY_WARNING = "WARNING"  # [Spec 18.3] System continues; banner alert
SEVERITY_PAUSE = "PAUSE"  # [Spec 15.4.1.2] Forces IDLE state; requires ACK
SEVERITY_CRITICAL = "CRITICAL"  # [Spec 15.1] Red Alert: Immediate E-STOP
