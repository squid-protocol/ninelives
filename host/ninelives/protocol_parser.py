# lib/protocol_parser.py - Ninelives MTIP v5 Translation Engine
# ROLE: The Central Translator. Decodes raw serial strings into structured objects.
# COMPLIANCE: Spec 4.3 (Robust Parsing), Spec 19.2 (Telemetry Ingestion)
# VERSION: v1.0.0 - Consolidated Logic Branch.

"""
[Spec 4.3] MTIP v5 Protocol Parser.

This module acts as the authoritative translator for the Ninelives architecture.
It deconstructs raw byte payloads from the hardware fleet into normalized
Python dictionaries, handling both the high-level 'Envelope' (LVL|TAG|MSG)
and the nested 'Body' (Key=Value or JSON).

Architectural Benefits:
1. Consistency: Ensures the Router and GUI see the exact same data structures.
2. Robustness: Tracks character depth to handle nested lists and dictionaries.
3. Type Safety: Automatically attempts JSON-safe type conversion for primitives.
"""

import json
import logging

# Standard industrial logger for parsing exceptions
parser_logger = logging.getLogger("PARSER")


def parse_kv_payload(payload_str):
    """
    [Spec 4.3] Hardened Key-Value and JSON Parser.

    Decodes telemetry strings into typed dictionaries. Handles standard K=V pairs,
    nested lists [], and JSON objects {} by implementing a depth-tracking
    linear scanner.

    Args:
        payload_str (str): The raw protocol string (e.g., "SENS:temp=42,status={'ok':True}").

    Returns:
        dict: A dictionary of parsed and typed values. Returns empty dict on failure.
    """
    items = {}
    if not payload_str:
        return items

    # 1. Pre-process: Strip protocol prefixes (Spec 4.3.1)
    clean_str = payload_str.strip()
    has_prefix = False
    for prefix in ["SENS:", "ACT:", "CFG:", "EVT:", "ALARM:"]:
        if clean_str.startswith(prefix):
            clean_str = clean_str[len(prefix) :].strip()
            has_prefix = True
            break

    # [FIX] If the string didn't have a standard prefix, it might be a raw comma-separated KV list (like Versions/Configs).
    # Just proceed with clean_str as is!

    # 2. Fast Path: Full JSON Blob
    if clean_str.startswith("{") and clean_str.endswith("}"):
        try:
            return json.loads(clean_str)
        except json.JSONDecodeError:
            # If it looks like JSON but fails, strip braces and try manual K-V
            clean_str = clean_str[1:-1]

    # 3. Fast-Path for Flat Telemetry (C-Optimized Split)
    if "{" not in clean_str and "[" not in clean_str:
        tokens = clean_str.split(",")
    else:
        # 4. Robust Linear Scan (Fallback for nested JSON arrays/dicts)
        current_token = []
        depth_brace = 0
        depth_bracket = 0
        in_quote = False
        quote_char = None
        tokens = []

        for char in clean_str:
            # Handle Quoted Strings (Spec 4.3.2)
            if char in ['"', "'"]:
                if not in_quote:
                    in_quote, quote_char = True, char
                elif char == quote_char:
                    in_quote = False

            # Handle Structural Markers
            if not in_quote:
                if char == "{":
                    depth_brace += 1
                elif char == "}":
                    depth_brace -= 1
                elif char == "[":
                    depth_bracket += 1
                elif char == "]":
                    depth_bracket -= 1
                # Only split on commas at depth 0
                elif char == "," and depth_brace == 0 and depth_bracket == 0:
                    tokens.append("".join(current_token))
                    current_token = []
                    continue
            current_token.append(char)

        if current_token:
            tokens.append("".join(current_token))

    # 4. Token Processing and Type Conversion
    for token in tokens:
        token = token.strip()
        if not token or ("=" not in token and ":" not in token):
            continue

        sep = "=" if "=" in token else ":"
        k, v = token.split(sep, 1)
        k = k.strip().strip('"').strip("'")
        v = v.strip()

        try:
            # Normalization for JSON compatibility
            v_fixed = (
                v.replace("'", '"')
                .replace("False", "false")
                .replace("True", "true")
                .replace("None", "null")
            )
            items[k] = json.loads(v_fixed)
        except (json.JSONDecodeError, ValueError):
            # Fallback to raw string if not a valid JSON primitive
            items[k] = v

    return items


def decode_envelope(payload_bytes):
    """
    [Spec 19.2] Protocol Envelope Decoder.

    Deconstructs the standardized MTIP Live Log envelope (LVL|TAG|MSG).
    This function performs the initial binary-to-string decoding and
    structural verification.

    Args:
        payload_bytes (bytes): Raw bytes received from the serial link.

    Returns:
        dict: A structured dictionary containing:
            - 'lvl': Severity character (I, W, E, C, D)
            - 'tag': 5-character source tag
            - 'msg': The raw message string
            - 'data': The fully parsed K-V/JSON object (dict)
    """
    try:
        text = payload_bytes.decode("utf-8", "ignore").strip()
        parts = text.split("|", 2)  # Limit split to preserve complex MSG content

        if len(parts) >= 3:
            lvl, tag, msg = parts[0], parts[1], parts[2]
        else:
            lvl, tag, msg = "?", "RAW", text

        # Recursively parse the message body for structured data
        parsed_data = parse_kv_payload(msg)

        return {"lvl": lvl, "tag": tag, "msg": msg, "data": parsed_data}
    except Exception as e:
        parser_logger.warning(f"Envelope Decode Failed: {e}")
        return {"lvl": "E", "tag": "PRSER", "msg": str(payload_bytes), "data": {}}


def format_telemetry(tag, data_dict, level="I"):
    """
    Helper to pack a dictionary back into a protocol string.
    Useful for diagnostic tools or inter-module communication.

    Args:
        tag (str): 5-char tag.
        data_dict (dict): Data to pack.
        level (str): Log level.

    Returns:
        str: Standard formatted MTIP string.
    """
    kv_pairs = [f"{k}={json.dumps(v)}" for k, v in data_dict.items()]
    msg = ",".join(kv_pairs)
    return f"{level}|{tag}|{msg}"
