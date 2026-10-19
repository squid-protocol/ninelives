# pio_programs.py - Shared PIO Assembly Definitions
# STANDARD: Ninelives Shell v1.00
# PURPOSE: High-speed pulse counting and signal mirroring.

"""
[Spec 13.0] High-Speed Tachometry & PIO Strategy.
Defines the shared PIO assembly programs for the RP2040/RP2350 platform.
Offloads high-frequency pulse counting from the Machinist CPU (Spec 11.6) to 
the dedicated Programmable I/O hardware, ensuring 0% CPU overhead and 
deterministic edge detection critical for "Time as Distance" synchronization.
"""

import rp2

# --- PIO PROGRAMS ---

@rp2.asm_pio(in_shiftdir=rp2.PIO.SHIFT_RIGHT, autopush=False, fifo_join=rp2.PIO.JOIN_RX)
def pulse_counter_simple():
    """
    [Spec 13.3.1] The Simple Pulse Counter Assembly Program.
    Implements a Continuous Non-Blocking Push model (Spec 13.3). 
    Utilizes a decrementing 32-bit counter (X) to track rising edges. 
    Enforces Spec 13.3.2 (Non-Blocking Push) and JOIN_RX (Spec compliance) 
    to provide an 8-word deep buffer, preventing stalls if the Python driver 
    is momentarily delayed by GC or network activity.
    """
    # Initialize X to 0xFFFFFFFF (~0)
    # We use decrement (x_dec) because it's a native PIO instruction.
    mov(x, invert(null))
    
    wrap_target()
    
    # 1. Rising Edge Detection
    # Blocks here until the signal transitions 0 -> 1
    wait(0, pin, 0)
    wait(1, pin, 0)
    
    # 2. Pulse Accounting
    # Decrement X. If X is non-zero, jump to "output". 
    # (Effectively always jumps unless we run for 100 years).
    jmp(x_dec, "output")
    
    label("output")
    # 3. Data Transfer
    # Move the current counter value (X) to the Output Shift Register (ISR)
    mov(isr, x)
    
    # 4. Non-Blocking Push (Spec 13.3.2)
    # CRITICAL: push(noblock) drops the data if the FIFO is full.
    # If we used standard 'push()', the PIO would stall if Python was busy.
    push(noblock)
    
    wrap()

@rp2.asm_pio(sideset_init=[rp2.PIO.OUT_LOW], in_shiftdir=rp2.PIO.SHIFT_RIGHT, autopush=False, fifo_join=rp2.PIO.JOIN_RX)
def pulse_counter_with_mirror():
    """
    [Spec 13.2.1] Pulse Counter with Hardware Mirror.
    Provides "Zero-Latency Mirroring" for the primary motor controller (Pico 3).
    Drives a Side-Set Pin (Spec 13.2.1) to replicate the input FG pulse 
    within < 20ns (1 clock cycle). This ensures the Sensor Array (Pico 2) 
    shares the exact same "Distance Clock" as the controller without 
    software-induced lag.
    """
    # Initialize X to 0xFFFFFFFF
    mov(x, invert(null))
    
    wrap_target()
    
    # Wait for Low, set Mirror Pin Low
    wait(0, pin, 0)       .side(0)
    
    # Wait for High, set Mirror Pin High (Spec 13.2.1)
    # Latency: < 20 nanoseconds (1 clock cycle)
    wait(1, pin, 0)       .side(1)
    
    # Count & Push Logic (Same as Simple - Spec 13.3.1)
    jmp(x_dec, "output")
    
    label("output")
    mov(isr, x)
    push(noblock)
    
    wrap()