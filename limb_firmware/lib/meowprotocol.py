# meowprotocol.py - Ninelives MTIP v1.02
# CHANGES: 
# - ADDED: MSG_TYPE_CMD_SYNC_TIME (0x1A) for Time Bridge.
# - ADDED: Start Bit Recovery (Supports '?' as well as '<').
# - FIXED: Enforced Big Endian (>BBHB) in build_packet to match Standard 4.3.
# - Retained Hex-Encoded Wire Format <HEX_BODY:CRC>
# - Retained Elastic Loop Priority Sorting

"""
[Spec 4.3] The meowprotocol Standard (MTIP v1.02).
Defines the Message Transfer Interface Protocol (MTIP) for distributed robotic systems.
It specifies message formatting, CRC-16-CCITT validation, and multi-priority 
queueing for industrial reliability.
"""

import struct
import binascii
import lib.logging as log

VERSION = 5.6

# --- MESSAGE TYPE DEFINITIONS (Spec 4.3.4) ---
MSG_TYPE_CMD_STOP = 0x00 # Safety: Emergency Stop
MSG_TYPE_CMD      = 0x10 # Generic Command
MSG_TYPE_CMD_STS  = 0x11 # Heartbeat
MSG_TYPE_CMD_VER  = 0x12 # Query Version
MSG_TYPE_CMD_LOG  = 0x13 # Query Log
MSG_TYPE_CMD_RST  = 0x14 # System Reset
MSG_TYPE_CMD_ACT  = 0x15 # Query Actuators
MSG_TYPE_CMD_SNS  = 0x16 # Query Sensors
MSG_TYPE_CMD_CFG  = 0x17 # Query Config
MSG_TYPE_SET_CFG  = 0x18 # Set Config
MSG_TYPE_CMD_SYNC_TIME = 0x1A # [Spec 4.3.13] Global Time Sync
MSG_TYPE_CMD_BAUD = 0x1B # [Spec 4.3.14] RAM-Only UART Shift

MSG_TYPE_ACK      = 0x20
MSG_TYPE_NAK      = 0x30
MSG_TYPE_NAK_SYNTAX = 0x31
MSG_TYPE_NAK_BUSY   = 0x32
MSG_TYPE_NAK_STATE  = 0x33
MSG_TYPE_NAK_RANGE  = 0x34
MSG_TYPE_NAK_AUTH   = 0x35

MSG_TYPE_EVT      = 0x40
MSG_TYPE_STS      = 0x41
MSG_TYPE_VRS      = 0x42
MSG_TYPE_LOG      = 0x43
MSG_TYPE_LIVE_LOG = 0x44
MSG_TYPE_ACT      = 0x45
MSG_TYPE_SNS      = 0x46
MSG_TYPE_CFG      = 0x47
MSG_TYPE_ALARM    = 0x48

MSG_TYPE_OTA_START = 0x50
MSG_TYPE_OTA_DATA  = 0x51
MSG_TYPE_OTA_END   = 0x52
MSG_TYPE_OTA_ABORT = 0x53

SYNC_BYTE = b'\x3F' # '?'

# --- CRC LOGIC (Spec 4.3.3: CRC on HEX STRING) ---
def crc16_ccitt(data_bytes):
    """
    [Spec 4.3.3] Message Formatting & Integrity.
    Calculates the CRC-16-CCITT checksum on the Uppercase Hex String body.
    Initial: 0xFFFF, Poly: 0x1021.
    """
    crc = 0xFFFF
    for byte in data_bytes:
        crc ^= (byte << 8)
        for _ in range(8):
            if (crc & 0x8000): crc = (crc << 1) ^ 0x1021
            else: crc = (crc << 1)
            crc &= 0xFFFF
    return crc

# --- ELASTIC LOOP PRIORITY ---
def priority_sort(packet_list):
    """
    [Spec 4.3.1.1.A] The "Jump-the-Line" Sort.
    Sorts a batch of incoming packets in memory by their MSG_TYPE priority level.
    Priority 0: Critical (0x00, 0x48)
    Priority 1: Action (Commands)
    Priority 2: Bulk (Data/OTA)
    """
    def get_priority(pkt):
        # pkt: (target, source, seq, type, payload)
        m_type = pkt[3]
        if m_type == MSG_TYPE_CMD_STOP or m_type == MSG_TYPE_ALARM: 
            return 0 # CRITICAL
        if m_type < 0x20: 
            return 1 # CMD
        return 2 # DATA
    
    packet_list.sort(key=get_priority)

# --- PACKET BUILDER ---
def build_packet(target, source, seq, msg_type, payload=""):
    """
    [Spec 4.3.3] Wire Format Construction.
    Concatenates binary fields, hex-encodes the result, calculates CRC, and 
    wraps the body in the standard <HEX_BODY:CRC> frame.
    Forces Big Endian (>) packing for the 5-byte header.
    """
    # 1. Prepare Payload Bytes
    if isinstance(payload, str):
        payload_bytes = payload.encode('ascii')
    elif isinstance(payload, bytes):
        payload_bytes = payload
    else:
        payload_bytes = str(payload).encode('ascii')

    # 2. Pack Header (Binary): T, S, Q, Type
    # [Spec 4.3.3] Header: [TARGET][SOURCE][SEQ_HIGH][SEQ_LOW][TYPE]
    header = struct.pack(">BBHB", target, source, seq, msg_type)
    
    # 3. Hex Encode Body (Header + Payload) -> Uppercase
    hex_body = binascii.hexlify(header + payload_bytes).upper()
    
    # 4. Calculate CRC on the HEX STRING
    crc_val = crc16_ccitt(hex_body)
    
    # 5. Frame It: <HEX_BODY:CRC>
    return f"<{hex_body.decode('ascii')}:{crc_val:04X}>".encode('ascii')

# --- PACKET PARSER ---
class PacketParser:
    """
    [Spec 4.3.1.1] Elastic Loop Processing: The RX Side.
    Implements greedy ingestion and start-bit recovery to extract MTIP frames 
    from a continuous byte stream.
    """
    def __init__(self, device_id):
        """
        [Spec 4.3.1.1] Initializes the stream parser with target device ID.
        """
        self.device_id = device_id
        self.buffer = b""
        
    def parse_stream(self, chunk):
        """
        [Spec 4.3.1.1.A] Stage 1 & 2: Buffer Drain & Stream Parsing.
        Consumes the entire hardware buffer and identifies frames using '<' or 
        '?' as start delimiters for bit-shift recovery.
        Returns a list of priority-sorted packets.
        """
        packets = []
        if not chunk: return packets
        
        self.buffer += chunk
        
        # Greedy Ingestion: Find all complete frames
        # [Spec 4.3.3] Start Bit Recovery logic
        while True:
            # Find earliest possible start char
            start_bracket = self.buffer.find(b'<')
            start_qm = self.buffer.find(b'?')
            
            start = -1
            if start_bracket != -1 and start_qm != -1:
                start = min(start_bracket, start_qm)
            elif start_bracket != -1:
                start = start_bracket
            elif start_qm != -1:
                start = start_qm
            
            # If no start delimiter found, stop
            if start == -1: break
                
            # Find end delimiter
            end = self.buffer.find(b'>', start)
            
            if end == -1: 
                # --- POISONED BUFFER TRAP FIX ---
                # MTIP packets should never exceed 512 bytes. If we have scanned 
                # past 512 bytes from the start character without seeing an end bracket, 
                # the start character was electrical noise. Drop the false start!
                if len(self.buffer) - start > 512:
                    log.warn("NET", "Poisoned Buffer: Dropping false start bit")
                    self.buffer = self.buffer[start+1:]
                    continue
                else:
                    # Genuine fragment, waiting for the rest of the data
                    break 

            # If a newer start delimiter ('<' or '?') appears before '>', the earlier
            # start character was electrical noise—advance to the newer start delimiter.
            next_bracket = self.buffer.find(b'<', start + 1, end)
            next_qm = self.buffer.find(b'?', start + 1, end)
            next_start = -1
            if next_bracket != -1 and next_qm != -1:
                next_start = min(next_bracket, next_qm)
            elif next_bracket != -1:
                next_start = next_bracket
            elif next_qm != -1:
                next_start = next_qm

            if next_start != -1:
                log.warn("NET", "Poisoned Buffer: Skipping false start before valid frame")
                self.buffer = self.buffer[next_start:]
                continue

            # Extract content between Start char and >
            frame_content = self.buffer[start+1:end]
            
            # Advance buffer
            self.buffer = self.buffer[end+1:]
            
            parsed = self._decode_hex_frame(frame_content)
            if parsed:
                packets.append(parsed)
                
        # Safety Flush (Buffer Overflow Protection)
        if len(self.buffer) > 4096:
            log.error("NET", "RX Buffer Overflow - Flushed")
            self.buffer = b""
            
        priority_sort(packets)
        return packets

    def _decode_hex_frame(self, raw_content):
        """
        [Spec 4.3.3] Wire Format Decoding.
        Splits HEX_BODY and CRC, validates checksum, and unhexlifies the body.
        Unpacks the Big-Endian header and filters by device_id or broadcast (0).
        """
        try:
            # Expected format: HEX_BODY:CRC
            if b':' not in raw_content: 
                log.warn("NET", "Malformed Frame: No Delimiter")
                return None
            
            hex_body, crc_str = raw_content.rsplit(b':', 1)
            
            # 1. Validate CRC (calculated on hex_body)
            try:
                if crc16_ccitt(hex_body) != int(crc_str, 16):
                    log.warn("NET", "CRC Fail")
                    return None
            except ValueError:
                log.warn("NET", "CRC Invalid Hex")
                return None
                
            # 2. Decode Hex back to Binary
            try:
                binary = binascii.unhexlify(hex_body)
            except Exception:
                log.warn("NET", "Hex Decode Fail")
                return None

            if len(binary) < 5: 
                log.warn("NET", "Packet Truncated")
                return None
            
            # 3. Unpack Header
            target, source, seq, m_type = struct.unpack(">BBHB", binary[:5])            
            
            # 4. Filter Target
            if target != self.device_id and target != 0:
                return None
                
            # 5. Extract Payload
            payload = binary[5:]
            
            return (target, source, seq, m_type, payload)
            
        except Exception as e:
            # Catch-all for unexpected parsing crashes
            log.error("NET", f"Parser Crash: {e}")
            return None