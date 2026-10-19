# ota.py - Ninelives OTA Manager v1.01
# PURPOSE: Safely write updates to flash and perform atomic swaps.
# STANDARD: Ninelives Shell v1.00

"""
[Spec 4.3.14] OTA System (Over-The-Air Updates).
Implements a Simulated A/B Update architecture (Spec 9.15) for industrial reliability.
By utilizing a temporary staging area (*.new) and atomic swaps, it ensures that 
a power failure during transport never leaves the device in a non-bootable state.
"""

import os
import ubinascii
import hashlib
import lib.logging as log

# --- LEGACY ID MAP ---
# [Spec 4.3.14.3.2] File Identification Map.
# Bandwidth optimization mapping 1-byte IDs to absolute file paths.
LEGACY_MAP = {
    # Core Files
    0x01: "config.json",
    0x02: "app.py",
    0x05: "boot.py",  
    0x03: "lib/meowprotocol.py",
    0x04: "lib/tsl2591.py",
    0x06: "lib/actuators.py",
    0x07: "lib/sensors.py",
    0x08: "lib/diagnostics.py",
    0x09: "lib/tester.py",
    0x0A: "lib/bldc_driver.py",
    0x0B: "lib/logging.py",
    0x0C: "lib/mpu6050.py",
    0x0D: "lib/ota.py",          # Self-update capability
    0x0E: "lib/pio_programs.py",
    0x0F: "lib/vibration_driver.py",
    
    # System State Files
    0x10: "lives.txt",            
    0x11: "boot_attempts.txt"  
}

class OTAManager:
    """
    [Spec 4.3.14.2.2] The OTA Receiver (Gatekeeper).
    Manages the lifecycle of a firmware update from handshake through 
    integrity verification to atomic commitment.
    """
    def __init__(self):
        """
        [Spec 4.3.14.4] Initializes the update session state.
        """
        self.active_filename = ""
        self.temp_filename = ""
        self.expected_checksum = ""
        self.current_chunks = 0
        self.total_chunks = 0
        self.open_file = None
        self.hasher = None 

    def _ensure_path(self, filepath):
        """
        [Spec 11.3] Filesystem Utilities.
        Recursively creates directory structures if missing.
        Prevents "ENOENT" errors when pushing new libraries to /lib/.
        """
        if "/" in filepath:
            path_parts = filepath.split("/")[:-1]
            current_path = ""
            for part in path_parts:
                current_path += part + "/"
                try:
                    os.stat(current_path)
                except OSError:
                    try:
                        os.mkdir(current_path[:-1])
                        log.info("OTA", f"Created dir: {current_path}")
                    except Exception as e:
                        log.error("OTA", f"Mkdir fail: {e}")

    def start_update(self, target_identifier, total_chunks, checksum_hex):
        """
        [Spec 4.3.14.4 Phase 1] Update Handshake (START).
        Resolves the target file (Spec 4.3.14.3.2), opens the RAM-resident 
        staging file, and initializes the SHA-256 hasher for transport verification.
        
        Args:
            target_identifier: Int (Legacy ID) OR String (Dynamic Filename)
            total_chunks: Expected number of packets.
            checksum_hex: Expected SHA-256 result.
        """
        # 1. Resolve Target Filename
        if isinstance(target_identifier, int):
            if target_identifier in LEGACY_MAP:
                self.active_filename = LEGACY_MAP[target_identifier]
            else:
                log.warn("OTA", f"Invalid File ID: {target_identifier}")
                return False, "INVALID_ID"
        elif isinstance(target_identifier, str):
            self.active_filename = target_identifier
        else:
            log.warn("OTA", "Invalid Identifier Type")
            return False, "INVALID_TYPE"

        # 2. Setup State & Temp File (Spec 9.3)
        self.temp_filename = self.active_filename + ".new"
        self.expected_checksum = checksum_hex.lower()
        
        if total_chunks <= 0:
            log.warn("OTA", "Refused: Payload declared 0 chunks.")
            return False, "ZERO_CHUNKS"
            
        self.total_chunks = total_chunks
        self.current_chunks = 0
        
        log.info("OTA", f"Start Update: {self.active_filename} ({total_chunks} chunks)")
        
        # 3. Ensure Directory and Open Stream
        try:
            self._ensure_path(self.temp_filename)
            self.open_file = open(self.temp_filename, "wb")
            self.hasher = hashlib.sha256() 
            return True, "READY"
        except Exception as e:
            log.error("OTA", f"FS Error: {e}")
            return False, f"FS_ERROR_{e}"

    def write_chunk(self, chunk_data):
        """
        [Spec 4.3.14.4 Phase 2] Update Transport (DATA).
        Writes binary chunks to the .new file and updates the streaming 
        SHA-256 hash. Core 0 feeds the Watchdog (Spec 2.2.1.B) during this 
        blocking write operation.
        """
        if not self.open_file:
            return False, "NO_SESSION"
            
        # --- FLASH BOMB PROTECTION ---
        # Prevent runaway transmissions from exhausting the 2MB physical flash limit.
        if self.current_chunks >= self.total_chunks:
            log.error("OTA", f"Overflow Prevented: Rejecting chunk {self.current_chunks + 1} (Max: {self.total_chunks})")
            return False, "OVERFLOW_ERR"
            
        try:
            self.open_file.write(chunk_data)
            self.hasher.update(chunk_data) 
            self.current_chunks += 1
            return True, f"OK_{self.current_chunks}/{self.total_chunks}"
        except Exception as e:
            log.error("OTA", f"Write Error: {e}")
            return False, f"WRITE_ERR_{e}"

    def verify_and_commit(self):
        """
        [Spec 4.3.14.4 Phase 3] Update Commit (END).
        Finalizes transport. Verifies data integrity via SHA-256 (Spec 9.15).
        Performs the Atomic Swap (Spec 9.3) and Paranoid Post-Flight Verification.
        """
        if not self.open_file:
            return False, "NO_SESSION"
            
        # 1. Flush and Close File
        self.open_file.close()
        self.open_file = None
        
        # [Spec 4.3.14.4] Truncation Check
        if self.current_chunks != self.total_chunks:
            log.error("OTA", f"Truncated: Received {self.current_chunks}/{self.total_chunks}")
            try: os.remove(self.temp_filename)
            except: pass
            return False, "TRUNCATED"
        
        # 2. Verify Integrity (In-Memory Stream)
        calc_hash = ubinascii.hexlify(self.hasher.digest()).decode()
        log.debug("OTA", f"Hash Check: {calc_hash} vs {self.expected_checksum}")
        
        if calc_hash != self.expected_checksum:
            log.error("OTA", "Hash Mismatch")
            try: os.remove(self.temp_filename)
            except: pass
            return False, "HASH_FAIL"
            
        # 3. Atomic Swap (Spec 9.3)
        target = self.active_filename
        try:
            # Create Backup if applicable (Spec 15.3)
            if target == "app.py":
                try:
                    if "app.py" in os.listdir():
                        try: os.remove("app.py.bak")
                        except: pass
                        os.rename("app.py", "app.py.bak")
                except: pass
            else:
                try: os.remove(target)
                except: pass 
            
            os.rename(self.temp_filename, target)
            
            # 4. PARANOID POST-FLIGHT VERIFICATION
            # Physically read the file back from disk to ensure LittleFS didn't lie
            try:
                disk_size = os.stat(target)[6]
                if disk_size == 0:
                    log.error("OTA", "Post-Flight Fail: File is 0 bytes")
                    return False, "NAK_STATE_ZERO_BYTE"
                    
                verify_hasher = hashlib.sha256()
                with open(target, "rb") as f:
                    while True:
                        # 256-byte chunks keep the RAM footprint tiny during the read
                        chunk = f.read(256)
                        if not chunk: break
                        verify_hasher.update(chunk)
                        
                disk_hash = ubinascii.hexlify(verify_hasher.digest()).decode()
                if disk_hash != self.expected_checksum:
                    log.error("OTA", "Post-Flight Fail: Disk corruption detected")
                    return False, "NAK_STATE_CORRUPT"
            except OSError:
                log.error("OTA", "Post-Flight Fail: File missing after swap")
                return False, "NAK_STATE_MISSING"
            
            # [Spec 4.3.14.4] Boot History Cleanup (Stability Restoration)
            try: os.remove("boot_attempts.txt")
            except: pass
            
            log.info("OTA", f"Success: {target} verified on disk")
            return True, "COMMIT_OK"
            
        except Exception as e:
            log.error("OTA", f"Swap Error: {e}")
            return False, f"NAK_STATE_SWAP_ERR_{e}"
        
    def abort(self):
        """
        [Spec 4.3.14.3.1] OTA_ABORT Handler.
        Cleans up open file handles and stage files to prevent disk exhaustion (Spec 9.8).
        """
        if self.open_file:
            self.open_file.close()
            self.open_file = None
        try:
            os.remove(self.temp_filename)
        except: pass
        log.warn("OTA", "Update Aborted")