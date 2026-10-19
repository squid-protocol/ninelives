# 🔌 Writing Custom Drivers (The Humble Component Pattern)

Ninelives uses a strict "Split-Brain" philosophy, and that extends all the way down to the physical silicon. To survive the electrical noise and EMI of real-world kinetic machinery, Ninelives implements the **Humble Component Pattern** for all hardware drivers.

If you are adding a new I2C sensor, a custom BLDC motor controller, or a specialized PIO state machine to an RP2350 limb, your driver has only one job: **Hardware Translation**. 

It translates logical intent (e.g., "Set speed to 50%") into physical voltage, or physical registers into logical data. 

**It does NOT handle errors.** If a motor spike scrambles the I2C bus, do not write a `while` loop to retry the read. Do not swallow the `OSError`. Let the exception crash upward. The central `SensorManager` will catch it, increment the watchdog, and autonomously reset the hardware bus if necessary. You provide the simple driver; the framework provides the industrial armor.

## The 4 Golden Rules of a Ninelives Driver

1. **Fail Upward:** Never use `try/except` to mask an `OSError` during a standard read or write. 
2. **Non-Blocking Execution:** Your driver runs on a deterministic, high-speed loop. Never use `time.sleep()` to wait for an ADC conversion. Read the buffer exactly as it is.
3. **Verify on Boot:** Implement a `ping()` method. The framework needs to know the hardware is physically present before it arms the system.
4. **Clean Up Safely:** Implement a `close()` or `deinit()` method wrapped in a `try/except`. When the framework tears down a bus during a watchdog recovery, the teardown itself must not crash.

---

## 💀 Driver Skeleton Template

Here is the minimum required structure for a Ninelives-compliant sensor driver. Drop your finished `.py` file into the Pico's `/lib` folder and reference it in your `config.json`.

```python
import lib.logging as log

class CustomSensorHumbleDriver:
    """
    Humble Driver for [Sensor Name]. 
    Manages register translation. Propagates errors upward.
    """
    def __init__(self, i2c, address=0x68):
        self.i2c = i2c
        self.address = address
        
        # 1. Verify Physical Connection immediately
        if not self.ping():
            log.error("SEN", f"Device not found at {hex(address)}")
            raise RuntimeError(f"Device not found at {hex(address)}")
            
        # 2. Hardware Setup (Write config registers, wake from sleep, etc.)
        self._write_register(0x6B, 0x00) # Example: Wake command
        log.info("SEN", "Device Init Complete")

    def _write_register(self, reg, value):
        """Internal: Direct I2C memory write."""
        self.i2c.writeto_mem(self.address, reg, bytearray([value]))

    def _read_register(self, reg, length=1):
        """Internal: Direct I2C memory read."""
        return self.i2c.readfrom_mem(self.address, reg, length)

    def ping(self):
        """
        Lightweight connection check. Returns Boolean.
        Usually reads the WHO_AM_I or DEVICE_ID register.
        """
        try:
            val = self._read_register(0x75, 1) # Example ID register
            return val[0] == 0x68
        except OSError:
            return False

    def close(self):
        """
        Standard Cleanup. Put the device to sleep or ground