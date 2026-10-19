# Ninelives: Lightweight Distributed SCADA & Unbrickable RS-485 Fleet Runtime

**Ninelives** is a fault-tolerant, bare-metal robotics and physical automation middleware that bridges the gap between cheap 3.3V/5V consumer microcontrollers (Raspberry Pi Pico / RP2350) and $10,000+ industrial PLC stacks.

It assumes every wire is unshielded, every motor will spike the power rail, and every I2C bus will eventually lock up—and handles all of it autonomously at the protocol and silicon level.

---

## Repository Structure

```text
ninelives/
├── pyproject.toml                 # Host Python package (`pip install -e .`)
├── host/
│   └── ninelives/                 # Runs on the Host Brain (Raspberry Pi 5 / Linux)
│       ├── protocol.py            # MTIP wire framing (<HEX_BODY:CRC>), CRC-16-CCITT, Zombie recovery
│       ├── protocol_parser.py     # Key=Value & JSON envelope translator
│       ├── switchboard.py         # Async RS-485 Fleet Operator, W-Protocol piggybacking, RTT/LQI
│       ├── digital_twin.py        # Passive memory-resident mirror of Limbs, Sensors, and Actuators
│       ├── telemetry_router.py    # Black-box flight recorder & forensic log router
│       └── cli/
│           ├── ota_flasher.py     # `ninelives-flash` (Chunked SHA-256 Mass OTA over RS-485)
│           └── ghost_rescue.py    # `ninelives-rescue` (Ghost Mode diagnostic & recovery bridge)
├── limb_firmware/                 # Generalized MicroPython firmware for RP2040/RP2350 Limbs
│   ├── boot.py                    # 9-Lives counter, app.py.bak rollback, Ghost Mode rescue kernel
│   ├── app.py                     # Dual-Core SMP Kernel (Core 0: Clerk, Core 1: Machinist)
│   ├── config.example.json        # Declarative pin & component role definition
│   └── lib/
│       ├── meowprotocol.py        # Limb MTIP stream parser + Jump-the-Line priority sorting
│       ├── sensors.py             # Humble Component HAL + 50ms I2C bus teardown/reset watchdog
│       ├── actuators.py           # Closed-loop verification + soft-start ramping
│       ├── ota.py                 # Atomic staging & SHA-256 verification
│       ├── pio_programs.py        # Zero-CPU RP2040/RP2350 PIO pulse counting
│       ├── diagnostics.py
│       └── logging.py
└── examples/
    ├── how_to_make_a_driver.md    # Guide to the Humble Component Pattern
    └── breakbeam_plugin/          # Example Core 1 metrology plugin (from Meow Turtle)
        └── breakbeam_monitor.py
```

---

## Core Highlights

1. **MTIP Wire Protocol (`<HEX_BODY:CRC>`):**
   * **Hex-Encoded ASCII Framing:** Eliminates delimiter collisions without complex byte-stuffing while remaining human-readable on a serial monitor.
   * **Start-Bit / "Zombie" Recovery:** Accepts `?` (`0x3F`) alongside `<` (`0x3C`) to recover packets when voltage droop shifts the start bit.
   * **Poisoned Buffer & False-Start Protection:** Drops false `<` start bits caused by motor EMI without corrupting subsequent frames.
   * **Jump-the-Line Priority Preemption:** Emergency Stop (`0x00`) and Safety Alarms (`0x48`) jump ahead of bulk telemetry and OTA packets.
   * **The W-Protocol:** Piggybacks event-buffer wipe acknowledgments (`|W=seq1,seq2`) onto standard heartbeat polls to save RS-485 bandwidth.

2. **Unbrickable Limb Kernel (`limb_firmware/`):**
   * **9-Lives Bootloader & Ghost Mode:** Decrements a life counter on boot and refills it only after sustained stability. If a bad OTA update causes a boot loop, `boot.py` automatically rolls back to `app.py.bak` or drops into **Ghost Mode**—an independent rescue listener that accepts OTA patches over RS-485 even when `app.py` is completely broken.
   * **The Humble Component Pattern:** Sensor drivers contain zero error-handling (`try/except` is forbidden on reads). When EMI scrambles the I2C bus, the exception crashes upward into `SensorManager`, which counts consecutive errors and autonomously tears down and re-initializes the physical I2C silicon in under 50ms.

---

## Quick Start

### 1. Install Host Package (RP5 / Linux)
```bash
pip install -e .
```

### 2. Flash a Limb (RP2040 / RP2350)
Copy the contents of `limb_firmware/` to a MicroPython-flashed Pico, customize `config.json` from `config.example.json`, and connect it to your RS-485 bus.

### 3. Fleet CLI Utilities
* **Mass OTA Flash over RS-485:**
  ```bash
  ninelives-flash -t A -f limb_firmware/app.py
  ```
* **Rescue a Limb from Ghost Mode:**
  ```bash
  ninelives-rescue
  ```
