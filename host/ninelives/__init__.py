"""
Ninelives: Lightweight Distributed SCADA & Unbrickable RS-485 Fleet Runtime.
"""

from .protocol import (
    build_packet,
    crc16_ccitt,
    PacketParser,
    MSG_TYPE_CMD_STOP,
    MSG_TYPE_CMD,
    MSG_TYPE_CMD_STS,
    MSG_TYPE_CMD_VER,
    MSG_TYPE_CMD_LOG,
    MSG_TYPE_CMD_RST,
    MSG_TYPE_CMD_ACT,
    MSG_TYPE_CMD_SNS,
    MSG_TYPE_CMD_CFG,
    MSG_TYPE_SET_CFG,
    CMD_SYNC_TIME,
    CMD_CFG_HASH,
    CMD_CLR_STATS,
    MSG_TYPE_ACK,
    MSG_TYPE_NAK,
    MSG_TYPE_EVT,
    MSG_TYPE_STS,
    MSG_TYPE_VRS,
    MSG_TYPE_LOG,
    MSG_TYPE_LIVE_LOG,
    MSG_TYPE_ACT,
    MSG_TYPE_SNS,
    MSG_TYPE_CFG,
    MSG_TYPE_ALARM,
    MSG_TYPE_OTA_START,
    MSG_TYPE_OTA_DATA,
    MSG_TYPE_OTA_END,
    MSG_TYPE_OTA_ABORT,
)
from .protocol_parser import parse_kv_payload, decode_envelope, format_telemetry
from .digital_twin import DigitalTwin, Limb, Actuator, Sensor, HostHealth, GLOBAL_TWIN
from .switchboard import PicoController, operator as FleetOperator
from .telemetry_router import TelemetryRouter, STREAM_ROUTER
from . import machine_states

__all__ = [
    "build_packet",
    "crc16_ccitt",
    "PacketParser",
    "parse_kv_payload",
    "decode_envelope",
    "format_telemetry",
    "DigitalTwin",
    "Limb",
    "Actuator",
    "Sensor",
    "HostHealth",
    "GLOBAL_TWIN",
    "PicoController",
    "FleetOperator",
    "TelemetryRouter",
    "STREAM_ROUTER",
    "machine_states",
]
