# lib/rp5_logger.py - Host Logging & Rotation (v1.5 - Spec Compliance Update)
# PURPOSE: Standardized logging with daily rotation to prevent disk exhaustion.
# COMPLIANCE: Core 0 Spec Section 9 (Rotation, Manifest, Formatting)
# CHANGES:
#   - v1.5: Refined rotation suffix to align with Spec 9.1 naming convention.
#   - v1.5: Optimized scan_local_versions() to strictly check first 5 lines (Spec 9.2).
#   - v1.4: Added scan_local_versions() for Boot Manifest audit logic.

"""
[Spec 9.0] Ninelives Host Logging System.

The Host Logger is responsible for maintaining a forensic record of the RP5 Brain's
internal state and cross-domain decisions. It implements a standardized
logging strategy that ensures high observability without compromising disk
stability or system performance.

Key Architectural Roles:
1. Standardized Formatting (Spec 9.1): Enforces a uniform [TIMESTAMP][LEVEL][MODULE] pattern.
2. Resource Management (Spec 9.1): Implements daily log rotation and a 7-day retention policy.
3. Boot Manifest Audit (Spec 9.3): Automatically generates a manifest of local file versions at startup.
4. Compliance Tracking (Spec 9.2): Identifies and logs version headers from all core modules.
"""

import logging
import os
import sys
import glob
import re
import queue
from logging.handlers import TimedRotatingFileHandler, QueueHandler, QueueListener

# --- CONFIGURATION CHECKS ---
try:
    import config.debug as dbg

    INITIAL_DEBUG = getattr(dbg, "DEBUG_GLOBAL", True)
except ImportError:
    INITIAL_DEBUG = True

# Ensure logs directory exists (Spec 9.1)
LOG_DIR = "logs"
if not os.path.exists(LOG_DIR):
    try:
        os.makedirs(LOG_DIR)
    except Exception as e:
        # Fallback to current directory if permission denied
        print(f"[CRITICAL] Could not create log directory: {e}")
        LOG_DIR = "."

# Log Configuration Constants (Spec 9.1)
LOG_FILE = os.path.join(LOG_DIR, "core0.log")
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logger():
    """
    [Spec 9.1] Configures the central ROOT logger infrastructure.

    Initializes a multi-destination logging system:
    1. TimedRotatingFileHandler: Writes to disk with daily rotation and 7-day backup limits.
    2. StreamHandler: Outputs to stdout for real-time terminal monitoring.

    :return: A logger instance specifically for the Core0 root context.
    """
    root_logger = logging.getLogger()

    # Set default level based on industrial debug configuration
    level = logging.DEBUG if INITIAL_DEBUG else logging.INFO
    root_logger.setLevel(level)

    # Clean existing handlers to prevent duplicate entries during hot-reloads
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    handlers_list = []

    # 1. Disk Egress (Spec 9.1: Daily Rotation, Keep 7 Days)
    try:
        file_handler = TimedRotatingFileHandler(
            LOG_FILE, when="midnight", interval=1, backupCount=7, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        # Custom suffix for rotated files: core0.log.YYYY-MM-DD
        file_handler.suffix = "%Y-%m-%d"
        handlers_list.append(file_handler)
    except Exception as e:
        print(f"[WARN] Failed to setup file logging: {e}")

    # 2. Terminal Egress for Real-time Monitoring
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    handlers_list.append(console_handler)

    # --- NON-BLOCKING I/O FIX ---
    # Decouple synchronous SD card/SSD writes from the asyncio event loop
    log_queue = queue.Queue(-1)
    queue_handler = QueueHandler(log_queue)
    root_logger.addHandler(queue_handler)

    listener = QueueListener(log_queue, *handlers_list, respect_handler_level=True)
    listener.start()

    return logging.getLogger("Core0")


# Initialize Root Logger immediately on import to capture bootstrap events
logger = setup_logger()


def set_debug_mode(enabled=True):
    """
    [Spec 10.5] Dynamic Debug Control.

    Toggles the logging severity level for the entire application at runtime.
    Propagates changes to all attached handlers (File and Console).

    :param enabled: If True, sets level to DEBUG. Otherwise, sets to INFO.
    """
    root_logger = logging.getLogger()
    level = logging.DEBUG if enabled else logging.INFO
    root_logger.setLevel(level)

    # Update all active handlers to the new level
    for handler in root_logger.handlers:
        handler.setLevel(level)

    if enabled:
        root_logger.debug(f"--- LOGGER DEBUG MODE ENABLED (Global={enabled}) ---")


# ==============================================================================
# SECTION 3: BOOT MANIFEST LOGIC (Spec 9.3)
# ==============================================================================


def scan_local_versions():
    """
    [Spec 9.3] Forensic Boot Manifest Generation.

    Scans the root and lib directories for Python source files containing
    version headers (vX.X). Strictly limits scanning to the first 5 lines
    of each file per Spec 9.2.

    Output is logged using the [AUDIT] [Manifest] tag to provide a permanent
    forensic record of the exact code versions executing at time of boot.
    """
    # Regex strictly matches vX.X or vX.XX format as per Spec 9.2
    version_pattern = re.compile(r"v(\d+\.\d+)")

    # Scan root and lib directory (Spec 9.3)
    files = glob.glob("*.py") + glob.glob("lib/*.py")

    audit_logger = logging.getLogger("Diagnostics")
    audit_logger.info("Scanning local file versions for Boot Manifest...")

    found_count = 0
    for file_path in files:
        try:
            filename = os.path.basename(file_path)
            with open(file_path, "r", encoding="utf-8") as f:
                # Strictly limit scan to the first 5 lines (Spec 9.2) to prevent false positives
                for _ in range(5):
                    line = f.readline()
                    if not line:
                        break

                    match = version_pattern.search(line)
                    if match:
                        # Log using the [AUDIT] [Manifest] tag defined in Spec 9.3
                        audit_logger.info(
                            f"[AUDIT] [Manifest] {filename}: v{match.group(1)}"
                        )
                        found_count += 1
                        break
        except Exception:
            pass  # Skip unreadable files silently; audit will catch missing entries

    if found_count == 0:
        audit_logger.warning("[AUDIT] No version headers found. Manifest is empty.")
    else:
        audit_logger.info(f"Version Audit Complete. {found_count} files identified.")
