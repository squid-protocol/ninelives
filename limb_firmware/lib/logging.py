# logging.py - Ninelives Telemetry Logging v1.00
# STANDARD: Ninelives Shell v2.2

"""
[Spec 14.0] Ninelives Telemetry & Logging Strategy.
Provides a centralized, thread-safe log buffering system. 
All internal system messages are routed through this module to ensure 
consistency across the fleet and to avoid raw print() statements in 
production code (Spec 14.1).
"""

import _thread
import collections

# --- SEVERITY LEVELS (Spec 14.1.1) ---
LEVEL_DEBUG = "D"
LEVEL_INFO  = "I"
LEVEL_WARN  = "W"
LEVEL_ERROR = "E"
LEVEL_CRIT  = "C"

# --- CONFIGURATION ---
MAX_MSG_LEN = 64
QUEUE_SIZE  = 20

# Global flag to enable local printing (Thonny visibility)
# Default is False. Modified by app.py at runtime.
PRINT_TRAFFIC = False

class LogManager:
    """
    [Spec 14.1] Centralized, thread-safe log buffering engine.
    Manages a fixed-size deque to prevent memory exhaustion while 
    ensuring that critical logs reach the RP5 Brain via MTIP.
    """
    def __init__(self):
        """
        [Spec 14.1] Initializes the log queue and SMP lock.
        Uses a collections.deque with a fixed size (Spec 14.1) to auto-discard 
        oldest messages if the queue overflows.
        """
        self.queue = collections.deque((), QUEUE_SIZE)
        self.lock = _thread.allocate_lock()
        
    def _push(self, level, tag, msg):
        """
        [Spec 14.1] Internal: Formats and pushes log entries to the queue.
        Enforces message truncation to MAX_MSG_LEN (Spec 14.1) to maintain 
        low-bandwidth UART compliance and handles optional local console output.
        """
        safe_msg = str(msg)
        if len(safe_msg) > MAX_MSG_LEN:
            uart_msg = safe_msg[:MAX_MSG_LEN-3] + "..."
        else:
            uart_msg = safe_msg
            
        entry = (level, tag, uart_msg)
        
        # 1. Push to Queue for RP5
        with self.lock:
            # If full, deque auto-discards oldest due to maxlen
            self.queue.append(entry)

        # 2. Optional: Print to Local Console
        # Access the module-level global variable directly
        if PRINT_TRAFFIC:
            print(f"[{level}][{tag}] {safe_msg}")

    def has_msg(self):
        """
        [Spec 4.3.2] Checks if the log queue contains pending messages.
        Used by the Clerk Core to determine if Log Channel (0x44) traffic 
        should be prioritized.
        """
        with self.lock:
            return len(self.queue) > 0

    def pop(self):
        """
        [Spec 14.1] Retrieves and removes the oldest log entry.
        Thread-safe extraction for the UART transmission loop.
        """
        with self.lock:
            if len(self.queue) > 0:
                return self.queue.popleft()
            return None

    # --- PUBLIC API (Spec 14.1.1) ---
    def debug(self, tag, msg): 
        """[Spec 14.1.1] D: High-volume development data."""
        self._push(LEVEL_DEBUG, tag, msg)
        
    def info(self, tag, msg): 
        """[Spec 14.1.1] I: Routine state changes."""
        self._push(LEVEL_INFO, tag, msg)
        
    def warn(self, tag, msg): 
        """[Spec 14.1.1] W: Non-critical issues."""
        self._push(LEVEL_WARN, tag, msg)
        
    def error(self, tag, msg): 
        """[Spec 14.1.1] E: Functional failures."""
        self._push(LEVEL_ERROR, tag, msg)
        
    def crit(self, tag, msg): 
        """[Spec 14.1.1] C: Safety failures."""
        self._push(LEVEL_CRIT, tag, msg)

# Global Singleton
logger = LogManager()

# Helper aliases
debug = logger.debug
info = logger.info
warn = logger.warn
error = logger.error
crit = logger.crit
has_msg = logger.has_msg
pop = logger.pop