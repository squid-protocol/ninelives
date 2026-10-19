# Pico Template: The "Limb" Firmware (Ninelives Shell)

This directory serves as the master firmware blueprint for all RP2040 and RP2350 microcontrollers on the Ninelives SCADA network. 

This firmware implements the **"Humble Component"** pattern. The microcontrollers running this code contain *zero* business logic, routing logic, or machine-specific awareness. They are pure, deterministic hardware executors. Their entire personality and physical layout are dictated by a single `config.json` file. 

Built on bare-metal MicroPython, this shell is designed for **Industrial Reliability**. It assumes power will fluctuate, RS-485 lines will suffer EMI noise, and mechanical vibrations will rattle sensors loose. To survive this, it utilizes a Dual-Core Symmetric Multiprocessing (SMP) architecture, atomic OTA updates, and real-time Fuzzy Logic filtering.

---

## 🧠 Dual-Core Architecture

To guarantee microsecond-level motor timing while simultaneously handling heavy MTIP UART communication, the firmware strictly divides labor:

* **Core 0 ("The Clerk"):** The network and management layer. It parses incoming MTIP packets, manages atomic OTA updates, generates the System Manifest, and pushes asynchronous telemetry and safety alarms (0x48) to the Host Brain.
* **Core 1 ("The Machinist"):** The real-time physics engine. It runs an uninterrupted, high-speed loop (>1kHz) that polls sensors, calculates EMA (Exponential Moving Average) filters, ramps PWM motor duties, and evaluates actuator safety. **Garbage Collection is strictly forbidden on this core** to prevent motor stutter.

---

## 📁 Directory Structure & Roles

### 📄 Root System Files
* **`app.py`:** The main kernel. Instantiates the Actuator/Sensor HALs, spins up the Core 1 physics thread, and enters the Core 0 MTIP event loop.
* **`boot.py` (The Guard Dog):** Runs before the kernel. Clamps all motor pins to ground within milliseconds of power application to prevent "Startup Lurch." It manages the 9-Life stability counter, triggering `.bak` restorations if `app.py` enters a crash loop.
* **`config.json`:** The single source of truth. Defines all PWM pin slice mappings, driver frequencies, I2C bus routes, and closed-loop verification thresholds.
* **`error_codes.md`:** The standardized Forensic Telemetry Catalog. Defines the exact 3-character tags (e.g., `SYS`, `ACT`, `NET`) and severity levels used by the firmware to report hardware crashes or state faults back to the Host.
* **`SYSTEM_MANIFEST.md`:** The baseline versioning document. Used by `diagnostics.py` to allow the Host to remotely audit the exact script versions running on the silicon.

### 📂 `lib/` (Hardware Abstraction & Services)
* **HAL (Hardware Abstraction Layers):** * `actuators.py`: Translates logical intent (0.0 to 1.0) into physical PWM/Digital signals, enforcing brownout-preventing soft-starts and Solenoid thermal fuse limits.
  * `sensors.py`: Ingests raw I2C/PIO/GPIO data, flattens dictionary payloads, and applies Level-2 EMA (Exponential Moving Average) filtering to absorb electrical noise.
* **Drivers:** Specialized humble components (`bldc_driver.py`, `vibration_driver.py`, `mpu6050.py`, `tsl2591.py`). If a specific physical node doesn't need them, they are gracefully bypassed.
* **Transport:** * `meowprotocol.py`: The deterministic MTIP parser featuring Greedy Ingestion, Piggyback Wipes, and payload deduplication.
* **System Services:** * `ota.py`: Manages the simulated A/B update bank. Streams chunks to `.new` files, verifies SHA-256 integrity, and performs atomic renames to prevent corruption during mid-update power loss.
  * `pio_programs.py`: Assembly instructions for the PIO state machines, allowing 0% CPU load tachometer counting for Time-as-Distance calculations.
  * `diagnostics.py` & `logging.py`: System health auditing and priority-routed console/network logging.
  * `tester.py`: Embedded script for performing sequential hardware I/O validation tests on the bench.

---

## 🛡️ Industrial Safeguards

1. **Closed-Loop Bridge (Confidence Decay):** The system refuses to operate blindly. When commanded ON, an actuator enters a `VERIFYING` state. Core 1 waits for physical sensor feedback (e.g., gyro resonance) to prove the motor is actually moving. It uses a **"Leaky Bucket" Fuzzy Logic** algorithm: momentary I2C dropouts from vibration won't trigger an E-STOP, but a sustained loss of confidence drains the bucket and throws a `FAULT:F_STL` (Stall) alarm.
2. **Ghost Mode Fallback:** If `boot.py` detects 0 lives remaining, it bypasses the main application entirely and loads a minimal, RAM-resident rescue shell on a hardcoded broadcast ID. This guarantees the node can always be found and re-flashed by the Host, completely eliminating the need for physical USB intervention.
3. **Cross-Core Watchdog:** Core 0 feeds the hardware Watchdog Timer, but it will *only* do so if Core 1 is actively proving it is alive via a shared-memory heartbeat. If either core freezes or starves, the hardware forces a full system reboot to return to a safe state.
4. **Brownout "Death Gasp":** Core 0 actively monitors the `VSYS` voltage divider. If the rail dips below 4.4V (usually caused by a massive DC motor inrush current), the firmware instantly clamps all outputs to 0 and hibernates the silicon to prevent flash memory corruption.

---

## 🚀 Deployment Instructions
To create a new physical limb on the network:
1. Copy this entire `pico_template` directory to the target Raspberry Pi Pico.
2. Modify the `config.json` to define the connected hardware and set the `device_id` (e.g., `4`).
3. Reboot the Pico. The Host RP5 `telemetry_router` will automatically discover it, audit its manifest, and integrate its hardware into the global Digital Twin.