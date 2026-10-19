# mpu6050.py - Driver for MPU6050 6-Axis IMU v1.02
# STANDARD: Ninelives Shell v1.00

"""
[Spec 11.0] Sensor Subsystem Specification.
Standardized driver for the MPU6050 6-Axis Inertial Measurement Unit (IMU).
Following the "Humble Component" pattern (Spec 11.8), this driver manages 
register-level communication while propagating hardware errors directly 
to the SensorManager for centralized lifecycle handling.
"""

import time
from micropython import const
import lib.logging as log

# [Spec 9.10] Dependency Versioning.
VERSION = 1.1

# --- REGISTER MAP ---
MPU6050_ADDR            = const(0x68)
MPU6050_REG_SMPLRT_DIV  = const(0x19)
MPU6050_REG_CONFIG      = const(0x1A)
MPU6050_REG_GYRO_CONFIG = const(0x1B)
MPU6050_REG_ACCEL_CONFIG= const(0x1C)
MPU6050_REG_ACCEL_XOUT_H= const(0x3B) # First data register
MPU6050_REG_PWR_MGMT_1  = const(0x6B)
MPU6050_REG_WHO_AM_I    = const(0x75)

# --- CONFIGURATION BITS ---
PWR_SLEEP               = const(0x40)
PWR_RESET               = const(0x80)
PWR_WAKE                = const(0x00)

# [Spec 4.3.9.3] SCALING FACTORS (Raw -> Physical Units)
ACCEL_SCALE_FACTOR      = 16384.0 
GYRO_SCALE_FACTOR       = 131.0

class MPU6050:
    """
    [Spec 11.8] Driver Interface & Error Responsibility.
    Humble Driver for the GY-521/MPU6050. Does not mask OSErrors, 
    allowing the parent SensorManager to perform I2C Bus Watchdog 
    recovery (Spec 11.4.3) if the bus freezes.
    """
    def __init__(self, i2c, address=MPU6050_ADDR):
        """
        [Spec 11.1] Sensor Subsystem Initialization.
        Verifies connectivity (Spec 9.11), wakes the device from sleep, 
        and configures the Digital Low Pass Filter (DLPF) to reduce 
        vibration noise for the kinetic verification bridge (Spec 16.3.2).
        """
        self.i2c = i2c
        self.address = address
        
        # 1. Verify Connection (Spec 9.11)
        if not self.ping():
            log.error("SEN", f"MPU6050 not found at {hex(address)}")
            raise RuntimeError(f"MPU6050 not found at {hex(address)}")
            
        # 2. Wake Up (Reset Sleep bit)
        self._write_register(MPU6050_REG_PWR_MGMT_1, PWR_WAKE)
        time.sleep_ms(10) # Wait for PLL to stabilize
        
        # 3. Configure (Defaults)
        # DLPF (Digital Low Pass Filter) -> 0x03 (44Hz Bandwidth) reduces noise
        self._write_register(MPU6050_REG_CONFIG, 0x03)
        # Sample Rate Div -> 0x00 (1kHz Accel sample rate)
        self._write_register(MPU6050_REG_SMPLRT_DIV, 0x00)
        
        log.info("SEN", "MPU6050 Init Complete")

    def _write_register(self, reg, value):
        """Internal: Direct I2C memory write."""
        self.i2c.writeto_mem(self.address, reg, bytearray([value]))

    def _read_register(self, reg, length=1):
        """Internal: Direct I2C memory read."""
        return self.i2c.readfrom_mem(self.address, reg, length)

    def ping(self):
        """
        [Spec 9.11] Sensor Connectivity Hardening.
        Lightweight connection check via the WHO_AM_I register. 
        Used during startup and self-healing resets.
        """
        try:
            val = self._read_register(MPU6050_REG_WHO_AM_I, 1)
            return val[0] == 0x68
        except OSError:
            return False

    def close(self):
        """
        [Spec 11.4.2] Standard Cleanup Protocol.
        Puts the sensor into low-power Sleep Mode to release the I2C bus 
        and reduce thermal output when the Machinist core is idle.
        """
        try:
            self._write_register(MPU6050_REG_PWR_MGMT_1, PWR_SLEEP)
        except OSError:
            pass # Best effort cleanup per Spec 11.4.2

    def get_raw_values(self):
        """
        [Spec 11.8] Exception Propagation.
        Performs a burst read of 14 bytes (Accel, Temp, Gyro). 
        Propagates OSErrors to trigger the SensorManager I2C Watchdog (Spec 11.4.3).
        """
        # Burst read starting from ACCEL_XOUT_H (0x3B) through GYRO_ZOUT_L (0x48)
        data = self._read_register(MPU6050_REG_ACCEL_XOUT_H, 14)
        
        # Helper to convert two bytes to signed integer
        def bytes_to_int(msb, lsb):
            val = (msb << 8) | lsb
            if val >= 0x8000: return -((65535 - val) + 1)
            return val

        res = {}
        res['ax'] = bytes_to_int(data[0], data[1])
        res['ay'] = bytes_to_int(data[2], data[3])
        res['az'] = bytes_to_int(data[4], data[5])
        res['temp'] = bytes_to_int(data[6], data[7])
        res['gx'] = bytes_to_int(data[8], data[9])
        res['gy'] = bytes_to_int(data[10], data[11])
        res['gz'] = bytes_to_int(data[12], data[13])
        return res

    def get_values(self):
        """
        [Spec 4.3.9.3] Standard Sensor Payload Ranges.
        Returns physically scaled values (G-Force, Degrees/Sec, Celsius).
        Provides the input data for the Kinetic Verification Strategy (Spec 16.3.2).
        """
        # NO try/except here. Let SensorManager handle it per Spec 11.8.
        raw = self.get_raw_values()
        
        # Apply Scaling
        clean = {}
        clean['AX'] = raw['ax'] / ACCEL_SCALE_FACTOR
        clean['AY'] = raw['ay'] / ACCEL_SCALE_FACTOR
        clean['AZ'] = raw['az'] / ACCEL_SCALE_FACTOR
        
        clean['GX'] = raw['gx'] / GYRO_SCALE_FACTOR
        clean['GY'] = raw['gy'] / GYRO_SCALE_FACTOR
        clean['GZ'] = raw['gz'] / GYRO_SCALE_FACTOR
        
        # Temp Formula: (Raw / 340.0) + 36.53
        clean['TEMP'] = (raw['temp'] / 340.0) + 36.53
        
        return clean