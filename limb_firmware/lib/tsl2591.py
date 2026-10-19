# This file must be saved as "tsl2591.py" in the /lib folder.
# STANDARD: Ninelives Shell v2.2
# UPDATE: v2.1 (Hardware Config Lockout Fix)

import time
from micropython import const

# Dependency Versioning (Std 9.10)
VERSION = 2

# TSL2591 registers
TSL2591_REGISTER_ENABLE = const(0x00)
TSL2591_REGISTER_CONFIG = const(0x01)
TSL2591_REGISTER_DEVICE_ID = const(0x12)
TSL2591_REGISTER_STATUS = const(0x13)
TSL2591_REGISTER_CHAN0_LOW = const(0x14)
TSL2591_REGISTER_CHAN1_LOW = const(0x16)

# TSL2591 commands
TSL2591_COMMAND_BIT = const(0xA0)
TSL2591_ENABLE_POWERON = const(0x01)
TSL2591_ENABLE_AEN = const(0x02)
TSL2591_ENABLE_AIEN = const(0x10)
TSL2591_ENABLE_NPIEN = const(0x80)

# Constants
TSL2591_GAIN_LOW = const(0x00)
TSL2591_GAIN_MED = const(0x10)
TSL2591_GAIN_HIGH = const(0x20)
TSL2591_GAIN_MAX = const(0x30)

class TSL2591:
    def __init__(self, i2c, address=0x29):
        """
        Initialize the sensor. Checks connection immediately.
        """
        self.i2c = i2c
        self.address = address
        
        # Verify connection (Std 9.5 Safe Init)
        if not self.ping():
             raise RuntimeError(f"TSL2591 not found at {hex(address)}")
            
        self._integration_time_val = 0x00 # Hardware standard for 100ms
        self._gain_val = TSL2591_GAIN_MAX 
        
        # 1. Turn chip on, but leave ADC off so the config register is unlocked
        self._write_register(TSL2591_REGISTER_ENABLE, TSL2591_ENABLE_POWERON)
        
        # 2. Write the config (Silicon will now accept the MAX GAIN)
        self._write_register(TSL2591_REGISTER_CONFIG, self._integration_time_val | self._gain_val)
        
        # 3. Turn the ADC on to start taking readings
        self.enable()

    def _write_register(self, register, value):
        cmd = TSL2591_COMMAND_BIT | register
        self.i2c.writeto_mem(self.address, cmd, bytearray([value]))

    def _read_register(self, register, length=1):
        cmd = TSL2591_COMMAND_BIT | register
        data = self.i2c.readfrom_mem(self.address, cmd, length)
        if length == 1:
            return data[0]
        return data

    def ping(self):
        """
        Lightweight connection check (Std 9.11).
        Returns True if ID register is readable and correct.
        """
        try:
            device_id = self._read_register(TSL2591_REGISTER_DEVICE_ID)
            return device_id == 0x50
        except OSError:
            return False

    def enable(self):
        """Enable the device in Continuous Mode."""
        self._write_register(TSL2591_REGISTER_ENABLE,
                             TSL2591_ENABLE_POWERON | TSL2591_ENABLE_AEN | TSL2591_ENABLE_AIEN | TSL2591_ENABLE_NPIEN)

    def disable(self):
        """Places silicon in low power mode."""
        self._write_register(TSL2591_REGISTER_ENABLE, 0x00)

    def close(self):
        """Standard Cleanup Method."""
        try:
            self.disable()
        except:
            pass

    def set_gain(self, gain_val):
        """Safely update gain by pausing the ADC."""
        self._write_register(TSL2591_REGISTER_ENABLE, TSL2591_ENABLE_POWERON)
        self._gain_val = gain_val
        self._write_register(TSL2591_REGISTER_CONFIG, self._integration_time_val | self._gain_val)
        self.enable()

    def set_timing(self, integration_time_val):
        """Safely update timing by pausing the ADC."""
        self._write_register(TSL2591_REGISTER_ENABLE, TSL2591_ENABLE_POWERON)
        self._integration_time_val = integration_time_val
        self._write_register(TSL2591_REGISTER_CONFIG, self._integration_time_val | self._gain_val)
        self.enable()

    def get_raw_channels(self):
        """
        Reads the latest available data from the double-buffered registers.
        CRITICAL UPDATE: This function is now NON-BLOCKING.
        """
        data = self._read_register(TSL2591_REGISTER_CHAN0_LOW, 4)
        
        # --- HARDWARE INTEGRITY VERIFICATION (Byte-Tear Detection) ---
        if (data[0] == data[1] and data[2] == data[3] and data[0] in [0x47, 0x2A]):
            return (-5, -5) 
            
        ch0 = (data[1] << 8) | data[0]
        ch1 = (data[3] << 8) | data[2]
        return (ch0, ch1)
    
    def get_full_luminosity(self):
        """Legacy wrapper for get_raw_channels."""
        c0, c1 = self.get_raw_channels()
        return (c1 << 16) | c0