# boot.py - Ninelives "Guard Dog" v1.02
"""
[Spec 3.0] Bootloader Subsystem.
Acts as the 'Guard Dog' for the Ninelives Shell. Handles hardware initialization, 
safety clamping, boot-loop detection, and provides the self-contained 'Ghost Mode' 
rescue kernel for unbrickable recovery.
"""

import machine
import os
import time
import struct
import binascii
import hashlib
import sys

# !!! IMPORTANT: DEVICE IDENTITY !!!
# This file must be tailored for each specific Pico.
# 'GHOST_DEVICE_ID' sets the identity when config.json is corrupt or missing.
# Update this value before flashing!
GHOST_DEVICE_ID = 1 

# --- LOGGING INIT (INDEPENDENCE FALLBACK) ---
# Spec 14.1: Attempt to load the unified logger. 
# If lib/ is corrupt, fallback to a safe internal class to ensure 
# Ghost Mode can still print without crashing.
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
VERSION = 1.02
LIVES_FILE = "lives.txt"
BOOT_ATTEMPTS_FILE = "boot_attempts.txt" # Tracks consecutive failures
MAX_LIVES = 9
MAX_BOOT_ATTEMPTS = 9 # Rollback after 3 failed boots
my_id = 3 ###Needs to be updated for each pico! ##### WARNING - ISSUE - CHECK - FIX - 

# Spec 9.2 Deviation: Recovery Pin is GPIO 27.
# Holding this pin LOW during boot forces Ghost Mode.
RECOVERY_PIN_NUM = 27 
LED_PIN_NUM = "LED"

# Standard 6: Motor Lockout Pins (Prevent startup lurch)
# These pins are pulled LOW immediately to prevent Mosfets from 
# firing randomly while the main kernel loads.
MOTOR_LOCKOUT_PINS = [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22] 

# --- 2. HARDWARE SETUP ---
# Initialize minimal IO for status indication and recovery
try:
    led = machine.Pin(LED_PIN_NUM, machine.Pin.OUT)
    led.on() # Solid ON = Booting
except: led = None

try:
    recovery_pin = machine.Pin(RECOVERY_PIN_NUM, machine.Pin.IN, machine.Pin.PULL_UP)
except: recovery_pin = None

# --- REPL DETACHMENT (Crucial for Thonny + RP5 co-existence) ---
# UART0 is shared between the REPL (Python Console) and the RS485 Data Bus.
# By default, print() sends text to UART0, which corrupts the binary protocol.
# This block disconnects REPL from UART0, forcing it to USB-CDC only.
try:
    import uos
    uos.dupterm(None, 1) 
    log.info("SYS", "REPL detached from UART0 (USB Only Mode)")
except:
    log.warn("SYS", "Could not detach REPL from UART0")

# --- 3. SAFETY LOCKOUT ---
def perform_safety_lockout():
    """
    [Spec 3.2] Bootloader Safety Lockout (Electrical Clamping).
    Iterates through all known motor pins and forces them LOW (Off).
    This ensures that even if a MOSFET is floating, it is clamped off
    before high-power rails are enabled to prevent 'Startup Lurch'.
    """
    try:
        for p in MOTOR_LOCKOUT_PINS:
            try:
                machine.Pin(p, machine.Pin.OUT).value(0)
            except: pass
    except Exception as e:
        print(f"[BOOT] Lockout Warning: {e}")

# Execute Lockout immediately upon script load
perform_safety_lockout()

# --- 4. UTILITIES ---
def cleanup_temp_files():
    """
    [Spec 9.8] Flash Hygiene (Disk Exhaustion Prevention).
    Scans the filesystem for temporary artifacts (.tmp, .new) left over 
    from interrupted or failed OTA updates and deletes them to maintain disk health.
    """
    try:
        for f in os.listdir():
            if f.endswith('.tmp') or f.endswith('.new'):
                try: os.remove(f); log.info("SYS", f"Cleaned {f}")
                except: pass
    except: pass

def check_crash_log():
    """
    [Spec 12.6] Log Hygiene & Forensics.
    Reads the tail end of the crash log file to provide immediate visibility 
    into the previous reboot cause. Essential for identifying 'Death Loops'.
    """
    try:
        if "crash.log" in os.listdir():
            print("\n[BOOT] !!! PREVIOUS CRASH LOG DETECTED !!!")
            with open("crash.log", "r") as f:
                lines = f.readlines()
                for line in lines[-3:]:
                    print(f"[LOG] {line.strip()}")
            print("----------------------------------------\n")
    except: pass

def read_int_file(filename, default_val=0):
    """
    [Spec 9.3] Atomic Storage Utility (Read).
    Safely reads an integer from a file. If the file is missing or corrupted, 
    it returns the provided default value without crashing the bootloader.
    """
    try:
        if filename in os.listdir():
            with open(filename, "r") as f:
                return int(f.read())
        else:
            return default_val
    except:
        return default_val

def write_int_file(filename, count):
    """
    [Spec 9.3] Atomic Storage Hardening (Write).
    Writes to a .tmp file then renames to the target filename to ensure that 
    a sudden power loss during a write cycle does not result in a corrupted file.
    """
    try:
        with open(filename + ".tmp", "w") as f:
            f.write(str(count))
            f.flush()
        try: os.remove(filename)
        except: pass
        os.rename(filename + ".tmp", filename)
    except: pass

def perform_rollback():
    """
    [Spec 15.2 / 4.3.14.5.2] Automatic Rollback (Boot Loop Protection).
    Renames the current app.py to app.py.bad for forensic analysis and 
    restores the 'Golden Image' app.py.bak to resume machine operation.
    """
    log.warn("SYS", "ROLLBACK TRIGGERED: Restoring Backup...")
    print("[BOOT] !!! ROLLBACK TRIGGERED !!!")
    
    if "app.py.bak" not in os.listdir():
        log.crit("SYS", "Rollback Failed: No Backup Found")
        print("[BOOT] Rollback Failed: app.py.bak missing!")
        return False

    try:
        # Move corrupted app.py to app.py.bad for forensic analysis
        try: os.rename("app.py", "app.py.bad")
        except: pass
        
        # Restore backup
        os.rename("app.py.bak", "app.py")
        log.info("SYS", "Rollback Success: Backup restored")
        print("[BOOT] SUCCESS: Backup Restored. Rebooting...")
        
        # Reset attempts counter so we don't loop forever
        write_int_file(BOOT_ATTEMPTS_FILE, 0)
        
        time.sleep(1)
        machine.reset()
        return True
    except Exception as e:
        log.crit("SYS", f"Rollback Error: {e}")
        return False

def print_manifest():
    """
    [Spec 12.2] Fleet Auditing & Startup Transparency.
    Invokes the diagnostics scanner to print the version information and 
    hashes of all core files on startup for development audit tracking.
    """
    try:
        import lib.diagnostics
        m = lib.diagnostics.SystemManifest()
        print(f"[BOOT] MANIFEST: {m.get_report_string()}")
    except: pass

# --- 5. GHOST MODE DEPENDENCIES (Self-Contained) ---
# CRITICAL: Ghost Mode must NOT import anything from 'lib/'.
# If 'lib/' is corrupted, Ghost Mode is the only way to recover the device.
# Therefore, all necessary Protocol and OTA logic is inlined below.

def ghost_crc16(data_bytes):
    """
    [Spec 4.3.3] CRC-16-CCITT implementation.
    Standardized checksum logic utilized for verifying packet integrity 
    within the isolated Ghost Mode rescue kernel.
    """
    crc = 0xFFFF
    for byte in data_bytes:
        crc ^= (byte << 8)
        for _ in range(8):
            if (crc & 0x8000): crc = (crc << 1) ^ 0x1021
            else: crc = (crc << 1)
            crc &= 0xFFFF
    return crc

def ghost_frame_packet(target_id, source_id, seq_id, msg_type, payload_bytes=b""):
    """
    [Spec 4.3.3] Message Formatting (Wire Format Construction).
    Assembles a binary header and payload into a hex-encoded wire packet 
    wrapped in frame delimiters: <HEX_BODY:CRC>.
    """
    # 1. Pack Header (BBHB -> Target, Source, Seq(2), Type)
    header = struct.pack(">BBHB", target_id, source_id, seq_id, msg_type)
    
    # 2. Combine Header + Payload
    full_binary = header + payload_bytes
    
    # 3. Hex Encode (Uppercase)
    hex_body = binascii.hexlify(full_binary).upper()
    
    # 4. Calculate CRC on the HEX STRING
    crc_val = ghost_crc16(hex_body)
    
    # 5. Frame It
    return f"<{hex_body.decode()}:{crc_val:04X}>\n".encode()

class GhostParser:
    """
    [Spec 4.3.1.1.A] Minimalist Packet Parser (Greedy Ingestion).
    Provides a zero-dependency stream parser that isolates valid packets 
    from raw UART traffic without using standard libraries.
    """
    def __init__(self, local_id):
        """
        [Spec 4.3.14.5.3] Initialization with Identity Fallback.
        """
        self.buffer = b""
        self.max_buffer_size = 600
        self.local_id = local_id

    def parse_stream(self, chunk):
        """
        [Spec 4.3.1.1.A] Stream Processing.
        Ingests bytes, returns valid packets or None. Implements 'Greedy Ingestion' 
        to drain the hardware buffer completely.
        """
        if not chunk: return None
        self.buffer += chunk
        
        # Flush if too big (Anti-Overflow)
        if len(self.buffer) > self.max_buffer_size:
            self.buffer = b""
            return None

        # Look for complete frame delimiters < ... >
        if b'<' in self.buffer and b'>' in self.buffer:
            start = self.buffer.find(b'<')
            end = self.buffer.find(b'>', start)
            
            if end == -1: return None # Incomplete frame
            
            frame_content = self.buffer[start+1:end]
            self.buffer = self.buffer[end+1:] # Consume processed bytes
            
            # Format: HEX_BODY:CRC
            if b':' not in frame_content: return None
            
            try:
                hex_body, crc_str = frame_content.rsplit(b':', 1)
                
                # 1. CRC Check (On Hex Body)
                if ghost_crc16(hex_body) != int(crc_str, 16):
                    return ("NAK", "CRC")
                
                # 2. Decode Binary
                bin_dat = binascii.unhexlify(hex_body)
                if len(bin_dat) < 4: return None
                
                # 3. Unpack Header (Target, Source, Seq(2), Type)
                tid, sid, seq, mtype = struct.unpack(">BBHB", bin_dat[:5])
                
                # 4. ID Filter (Accept MyID, Broadcast 0, or Dynamic FF)
                if tid != self.local_id and tid != 0 and tid != 0xFF: return None
                
                return (tid, sid, seq, mtype, bin_dat[5:])
                
            except:
                return ("NAK", "EXC")
                
        return None

class GhostOTAManager:
    """
    [Spec 4.3.14.5.3] Ghost Mode Recovery (OTA Receiver).
    Provides a self-contained handler for writing Over-The-Air updates 
    directly to flash, bypassing the main kernel entirely during recovery.
    """
    LEGACY_MAP = {
        0x01: "config.json", 
        0x02: "app.py", 
        0x03: "lib/meowprotocol.py",
        0x04: "lib/tsl2591.py", 
        0x05: "boot.py", 
        0x06: "lib/actuators.py",
        0x07: "lib/sensors.py", 
        0x08: "lib/diagnostics.py",
        0x09: "lib/tester.py",
        0x0A: "lib/bldc_driver.py",
        0x0B: "lib/logging.py",
        0x0C: "lib/mpu6050.py",
        0x0D: "lib/ota.py",
        0x0E: "lib/pio_programs.py",
        0x0F: "lib/vibration_driver.py",
    
        # System State Files [NEW in v1.02]
        0x10: "lives.txt",            # Remote Life Management
        0x11: "boot_attempts.txt" 
    }
    
    def __init__(self):
        """Initializes empty state for atomic flash writing."""
        self.f = None; self.name = ""; self.hash = None; self.exp_hash = ""
    
    def start(self, target_id, exp_hash):
        """
        [Spec 4.3.14.1] OTA Phase 1: Preparation.
        Resolves the filename, creates directories, and opens the .new 
        temporary file for streaming binary data.
        """
        # Determine filename
        if isinstance(target_id, int):
            if target_id in self.LEGACY_MAP: self.name = self.LEGACY_MAP[target_id]
            else: return False, "BAD_ID"
        else: self.name = str(target_id)
        
        self.exp_hash = exp_hash.lower()
        
        try:
            # Ensure directory exists before opening file
            if "/" in self.name:
                try: os.mkdir(self.name.rsplit("/", 1)[0])
                except: pass
            
            # Open .new temporary file
            self.f = open(self.name + ".new", "wb")
            self.hash = hashlib.sha256()
            return True, "READY"
        except Exception as e: return False, f"FS_{e}"

    def write(self, data):
        """
        [Spec 4.3.14.1] OTA Phase 2: Ingestion.
        Writes binary data chunks to the temporary staging file and updates 
         the running SHA-256 integrity hash.
        """
        if not self.f: return False, "NO_SESS"
        try:
            self.f.write(data)
            self.hash.update(data)
            return True, "OK"
        except: return False, "WR_ERR"

    def commit(self):
        """
        [Spec 4.3.14.1] OTA Phase 3: Commit (Atomic Swap).
        Verifies the final file hash and renames the .new file to the target 
        filename. Returns DONE if the system is ready for reboot.
        """
        # Finalize and Atomic Swap
        if not self.f: return False, "NO_SESS"
        self.f.close(); self.f = None
        
        # Verify Hash
        calc = binascii.hexlify(self.hash.digest()).decode()
        if calc != self.exp_hash:
            try: os.remove(self.name + ".new")
            except: pass
            return False, "HASH_FAIL"
        
        # Atomic Rename (preserve app.py.bak for automatic rollback)
        try:
            if self.name == "app.py":
                try:
                    if "app.py" in os.listdir():
                        try: os.remove("app.py.bak")
                        except: pass
                        os.rename("app.py", "app.py.bak")
                except: pass
            else:
                try: os.remove(self.name)
                except: pass
            os.rename(self.name + ".new", self.name)
            return True, "DONE"
        except Exception as e: return False, f"MV_{e}"

    def abort(self):
        """
        [Spec 4.3.14.3.1] Manual OTA Session Termination.
        Closes file handles and deletes the incomplete temporary staging file.
        """
        if self.f:
            try: self.f.close()
            except: pass
        try: os.remove(self.name + ".new")
        except: pass
        return True, "ABORTED"

def run_ghost_mode():
    """
    [Spec 4.3.14.5.3] Ghost Mode (The Rescue Kernel).
    A non-returning event loop that runs when Lives == 0. Provides:
    1. SOS LED Pattern.
    2. UART OTA Capability.
    3. Emergency RESET/EXIT control.
    """
    log.crit("SYS", "ENTERING GHOST MODE (INDEPENDENT)")
    print("\n[BOOT] !!! ENTERING GHOST MODE !!!")
    print("[BOOT] Functionality: SOS Blink, OTA Receiver, Reset, Exit to REPL.")
    
    # Re-assert safety lockout
    perform_safety_lockout()
    
    # Init Hardware for Ghost
    uart = machine.UART(0, 115200, tx=machine.Pin(0), rx=machine.Pin(1))
    my_id = GHOST_DEVICE_ID

    try:
        import json
        with open('config.json', 'r') as f: my_id = json.load(f)['system']['device_id']
    except: pass
    
    parser = GhostParser(my_id)
    ota_mgr = GhostOTAManager()
    
    while True:
        # Fast Strobe / Heartbeat Pattern
        if led: led.toggle()
        
        # Poll UART
        while uart.any():
            chunk = uart.read()
            result = parser.parse_stream(chunk)
            
            if result and isinstance(result, tuple) and result[0] != "NAK":
                tgt, src, seq, m_type, m_pay = result
                
                # --- COMMAND: RESET / EXIT ---
                if m_type == 0x10: 
                    if b"RESET" in m_pay:
                        uart.write(ghost_frame_packet(src, my_id, seq, 0x20, b"RST"))
                        time.sleep(0.5); machine.reset()
                    
                    elif b"EXIT" in m_pay:
                        uart.write(ghost_frame_packet(src, my_id, seq, 0x20, b"EXIT_OK"))
                        print("[BOOT] Exiting Ghost Mode -> REPL")
                        if led: led.off()
                        return # Returns to main(), which drops to REPL
                        
                # --- COMMAND: STATUS (0x11) ---
                elif m_type == 0x11: 
                    uart.write(ghost_frame_packet(src, my_id, seq, 0x41, b"S=GHOST,ERR=NO_APP"))
                
                # --- OTA START (0x50) ---
                elif m_type == 0x50:
                    try:
                        # Protocol: [FILE_ID][CHUNKS(2)]?[HASH(64)] or [FF][NAME_LEN][NAME][CHUNKS(2)]?[HASH(64)]
                        fid = m_pay[0]
                        if fid == 0xFF: # Dynamic Name
                            n_len = m_pay[1] 
                            fname = m_pay[2 : 2+n_len].decode()
                            rem = m_pay[2+n_len:]
                            chk = rem[2:].decode() if len(rem) >= 66 else rem.decode()
                            ok, msg = ota_mgr.start(fname, chk)
                        else: # Legacy ID
                            chk = m_pay[3:].decode() if len(m_pay) >= 67 else m_pay[1:].decode()
                            ok, msg = ota_mgr.start(fid, chk)
                        uart.write(ghost_frame_packet(src, my_id, seq, 0x20 if ok else 0x30, msg.encode()))
                    except:
                        uart.write(ghost_frame_packet(src, my_id, seq, 0x30, b"PAR_ERR"))

                # --- OTA DATA (0x51) ---
                elif m_type == 0x51:
                    ok, msg = ota_mgr.write(m_pay)
                    uart.write(ghost_frame_packet(src, my_id, seq, 0x20 if ok else 0x30, msg.encode()))

                # --- OTA END (0x52) ---
                elif m_type == 0x52:
                    ok, msg = ota_mgr.commit()
                    uart.write(ghost_frame_packet(src, my_id, seq, 0x20 if ok else 0x30, msg.encode() if not ok else b"REBOOT"))
                    if ok:
                        time.sleep(1.0)
                        # Standard 1: Restore lives on successful flash
                        write_int_file(LIVES_FILE, MAX_LIVES)
                        # Reset boot attempts on successful flash commit
                        write_int_file(BOOT_ATTEMPTS_FILE, 0)
                        machine.reset()
                                
               # --- OTA ABORT (0x53) ---
                elif m_type == 0x53:
                    ok, msg = ota_mgr.abort()
                    uart.write(ghost_frame_packet(src, my_id, seq, 0x20, msg.encode()))
                        
        time.sleep(0.05)

# --- 6. MAIN ENTRY POINT ---
def main():
    """
    [Spec 1.0 / 15.0] Bootloader Logical Sequence.
    Executes the standard startup sequence: Cleanup -> Safety Lockout -> 
    Life Deduction -> Boot Loop Check -> App Launch. 
    Maintains the 'Unbrickable' lifecycle.
    """
    print(f"\n=== NINELIVES BOOTLOADER v{VERSION} ===")
    log.info("SYS", "Bootloader Start")
    
    # Clean up disk and show forensic data
    cleanup_temp_files()
    check_crash_log()
    
    # Check Recovery Pin (Manual Ghost Mode Trigger)
    if recovery_pin and recovery_pin.value() == 0:
        log.warn("SYS", "Recovery Pin Detected")
        print("[BOOT] RECOVERY PIN HELD: FORCING GHOST MODE")
        lives = 0
    else:
        lives = read_int_file(LIVES_FILE, default_val=MAX_LIVES)
        if lives == 0:
            # Check if file exists but is 0 (death) vs missing (first boot)
            if LIVES_FILE not in os.listdir():
                 lives = MAX_LIVES

    log.info("SYS", f"Lives: {lives}/{MAX_LIVES}")
    print(f"[BOOT] LIVES: {lives}/{MAX_LIVES}")
    
    if lives > 0:
        print_manifest()
        
        # [Spec 15.2] Boot Loop Protection
        # Check attempts before launching
        attempts = read_int_file(BOOT_ATTEMPTS_FILE, default_val=0)
        log.info("SYS", f"Boot Attempts: {attempts}")
        
        if attempts >= MAX_BOOT_ATTEMPTS:
            # We have failed to boot 'app.py' too many times.
            # Try to restore backup.
            if perform_rollback():
                # Rollback resets machine, so we stop here
                return
            else:
                # Rollback failed (no backup). Proceed to Ghost Mode logic logic below.
                log.crit("SYS", "Rollback Failed. Deducting life.")
        
        # Increment attempts counter
        write_int_file(BOOT_ATTEMPTS_FILE, attempts + 1)
        
        # [Spec 1.0] Life Deduction Logic
        # We assume the app MIGHT crash. So we deduct a life NOW.
        # If the app runs stable for 30s (in app.py), it will refill the lives to 9
        # AND reset boot_attempts to 0.
        write_int_file(LIVES_FILE, lives - 1)
        log.info("SYS", "Launching App")
        
        # [Spec 3.3] Soft Reboot Hygiene (Expanded)
        # Force flush ALL custom modules from memory to ensure OTA updates take effect
        # Includes both naked and lib-prefixed imports for bulletproof memory purging.
        flush_list = [
            'app',
            'logging', 'lib.logging',
            'machine_states', 'lib.machine_states',
            'meowprotocol', 'lib.meowprotocol',
            'actuators', 'lib.actuators',
            'sensors', 'lib.sensors',
            'ota', 'lib.ota',
            'diagnostics', 'lib.diagnostics',
            'pio_programs', 'lib.pio_programs',
            'tsl2591', 'lib.tsl2591',
            'mpu6050', 'lib.mpu6050',
            'bldc_driver', 'lib.bldc_driver',
            'vibration_driver', 'lib.vibration_driver',
            'tester', 'lib.tester'
        ]
        
        for mod in flush_list:
            if mod in sys.modules:
                del sys.modules[mod]
        
        print("[BOOT] Importing app module...")

        try:
            import app
            print("[BOOT] App module imported. Executing main()...")
            # Wait a split second to ensure print buffer flushes before potential app crash
            time.sleep(0.1) 
            app.main()
        except ImportError:
            log.crit("SYS", "app.py missing")
            print("[BOOT] app.py not found!")
            run_ghost_mode()
        except Exception as e:
            # Catch immediate crashes during import/main() execution
            log.crit("SYS", f"App Crash: {e}\n")
            print(f"[BOOT] CRITICAL APP CRASH: {e}") 
            try:
                with open("crash.log", "a") as f: f.write(f"{time.ticks_ms()}: BOOT_CRASH: {e}\n")
            except: pass
            run_ghost_mode()
            
    else:
        # Lives == 0. System is unstable.
        log.crit("SYS", "0 Lives Left")
        run_ghost_mode()
        print("[BOOT] REPL Access Granted.")

if __name__ == "__main__":
    main()