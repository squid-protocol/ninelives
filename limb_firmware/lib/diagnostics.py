# diagnostics.py - Ninelives System Manifest & Reporting v1.02
# STANDARD: Ninelives Shell v1.00

"""
[Spec 12.1] Diagnostic Version Subsystem Overview.
Provides a robust, crash-proof mechanism to audit the exact version of every 
source file running on the device. Following the "Header-First" philosophy (Spec 12.2), 
it avoids importing modules to check versions, preventing system crashes due 
to syntax errors in corrupted library files.
"""

import os
import json
try:
    import lib.logging as log
except ImportError:
    # Fallback if logging lib is broken or missing
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

class SystemManifest:
    """
    [Spec 12.4.1] SystemManifest Class.
    Responsible for performing safe file I/O operations to scan system files 
    and build a comprehensive version manifest for fleet auditing.
    """
    def __init__(self):
        """
        [Spec 12.4.1] Initializes the manifest scanner.
        Defines the mandatory scan targets (Spec 12.4.3) synced with the 
        OTA file identification map.
        """
        self.versions = {}
        # Files to scan. Key = Report Label, Val = Filename
        # Synced with ota.py LEGACY_MAP (Spec 4.3.14.3.2)
        self.scan_targets = {
            'BOOT': 'boot.py',
            'MAIN': 'app.py',
            'LIB_MEOW': 'lib/meowprotocol.py',   # MicroProtocol
            'LIB_ACT': 'lib/actuators.py',     # Actuator HAL
            'LIB_SENS': 'lib/sensors.py',      # Sensor HAL
            'LIB_DIAG': 'lib/diagnostics.py',  # Self
            'LIB_OTA': 'lib/ota.py',           # OTA Manager
            'LIB_LOG': 'lib/logging.py',       # Logger
            'LIB_TEST': 'lib/tester.py',       # Hardware Tester
            'LIB_MPU': 'lib/mpu6050.py',       # Gyro Driver
            'LIB_VIBE': 'lib/vibration_driver.py', # Vibration Driver
            'LIB_BLDC': 'lib/bldc_driver.py',  # Conveyor Driver
            'LIB_PIO': 'lib/pio_programs.py',  # PIO Assembly
            'LIB_TSL': 'lib/tsl2591.py'        # Light Sensor
        }

    def _parse_py_header(self, filename):
        """
        [Spec 12.3] File Header Standard Scanner.
        Scans the first 5 lines of a python file to identify version strings 
        without execution. Supports Style 1 (Comment Tag vX.X) and Style 2 
        (VERSION = X constant).
        
        Returns:
            str: The version string, "MISSING", "UNKNOWN", or "READ_ERR".
        """
        try:
            # Check if file exists first to avoid exception spam
            try:
                os.stat(filename)
            except OSError:
                return "MISSING" # Signal that file is physically absent

            with open(filename, 'r') as f:
                for _ in range(5): # [Spec 12.4.1] Only scan the header
                    line = f.readline()
                    if not line: break
                    
                    # Style 1: Comment Tag "# ... v3.9" (Spec 12.3 Format A)
                    if " v" in line and line.strip().startswith("#"):
                        parts = line.split(" v")
                        if len(parts) > 1:
                            # Extract "3.9" from "3.9 (Hardened)..."
                            ver = parts[1].split()[0].strip()
                            return ver
                            
                    # Style 2: Code Constant "VERSION = 4" (Spec 12.3 Format B)
                    if "VERSION =" in line:
                        parts = line.split("=")
                        if len(parts) > 1:
                            # Strip quotes and whitespace
                            return parts[1].strip().strip('"').strip("'")
                        
            return "UNKNOWN"
        except Exception as e:
            log.warn("SYS", f"Manifest Read Err: {filename}")
            return "READ_ERR"

    def _parse_config(self):
        """
        [Spec 8.1] Configuration Version Extraction.
        Retrieves the 'system version' key from config.json to ensure 
        configuration schema alignment with firmware.
        """
        try:
            with open('config.json', 'r') as f:
                c = json.load(f)
                return c.get('system', {}).get('version', 'ERR_KEY')
        except:
            log.error("SYS", "Config Load Failed")
            return "ERR_JSON"

    def scan(self):
        """
        [Spec 12.4.1] Fleet Auditing Logic.
        Iterates through all defined scan targets to build the internal 
        version registry. Gracefully handles missing dependencies to provide 
        transparency during startup (Spec 12.2).
        """
        self.versions = {}
        
        # Scan Python Files
        for label, fname in self.scan_targets.items():
            ver = self._parse_py_header(fname)
            # We include MISSING/UNKNOWN to help debug what is absent
            if ver: 
                self.versions[label] = ver
        
        # Scan Config
        self.versions['CFG'] = self._parse_config()
        
        return self.versions

    def get_report_string(self):
        """
        [Spec 12.5] Report Format.
        Generates a compact, comma-separated Key-Value string (e.g., MAIN=1.2,BOOT=3.9) 
        optimized for low-bandwidth UART transmission during CMD_VER (Spec 12.4.3) requests.
        """
        self.scan()
        # Sort for consistent output
        sorted_items = sorted(self.versions.items())
        return ",".join([f"{k}={v}" for k,v in sorted_items])

if __name__ == "__main__":
    man = SystemManifest()
    print(man.get_report_string())