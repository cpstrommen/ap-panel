import pytest

from ap_panel.protocol import (
    MAX_TX_FRAME_BYTES,
    Message,
    Parser,
    ProtocolError,
    config_records,
    encode,
    lamp_commands,
    lcd_command,
)


def test_fragmented_escape_and_coalesced_newlines():
    parser = Parser()
    assert parser.feed(b"\r\n10,AP/,") == []
    assert parser.feed(b"Name/;Slash//;\r\n29,IN,15,0;6,HDG,3;") == [
        Message(10, ("AP,Name;Slash/",)),
        Message(29, ("IN", "15", "0")),
        Message(6, ("HDG", "3")),
    ]


def test_incremental_parser_preserves_empty_fields_and_no_argument_reply():
    parser = Parser()
    data = b"10,;34;10,a,,b;"
    result = []
    for byte in data:
        result.extend(parser.feed(bytes((byte,))))
    assert result == [Message(10, ("",)), Message(34), Message(10, ("a", "", "b"))]


def test_receive_config_can_exceed_transmit_limit():
    config = "8.0.1.0.ENC:" * 100
    assert Parser().feed(("10," + config + ";\r\n").encode()) == [Message(10, (config,))]


@pytest.mark.parametrize("data", [b"x,foo;", b"256;", b"10,a\n;", b"10,\xff;", b";", b"-1;"])
def test_malformed_parser_resets_and_raises(data):
    parser = Parser()
    with pytest.raises(ProtocolError):
        parser.feed(data)
    assert parser.feed(b"34;") == [Message(34)]


def test_receive_limit_applies_across_fragments_including_escapes():
    parser = Parser(max_frame_bytes=10)
    assert parser.feed(b"10,////") == []
    with pytest.raises(ProtocolError, match="receive frame limit"):
        parser.feed(b"////;")


def test_encode_escaped_frame_bound_not_unescaped_length():
    assert encode(25, 0, "a,b;c/d") == b"25,0,a/,b/;c//d;"
    assert len(encode(11, "x" * 90)) == MAX_TX_FRAME_BYTES
    with pytest.raises(ProtocolError, match="at most 94"):
        encode(11, "x" * 91)
    with pytest.raises(ProtocolError, match="at most 94"):
        encode(25, 0, "/" * 80)


@pytest.mark.parametrize("value", ["é", "line\n", "a\x00", "\t"])
def test_encode_rejects_unrepresentable_arguments(value):
    with pytest.raises(ProtocolError, match="printable ASCII"):
        encode(25, 0, value)


def test_config_is_record_chunked_and_rejects_raw_escapes():
    raw = "8.0.1.0.HDG:12.14.15.16.2.IN:10.19.18.17.3.OUT:7.39.20.4.LCD:"
    assert "".join(config_records(raw)) == raw
    assert all(len(encode(11, record)) <= 94 for record in config_records(raw))
    for malformed in (
        "",
        "8.0.1.0.HDG",
        "8.0.1.0.H/DG:",
        "8.0.1.0.H,DG:",
        "8..HDG:",
        "8.0.1.0.HDG::",
    ):
        with pytest.raises(ProtocolError):
            config_records(malformed)


def test_lamp_bytes_are_farthest_first_without_reversing_bits():
    assert lamp_commands(0x010204) == (b"27,0,3,0,254,253,251;", b"27,0,3,1,1,2,4;")
    assert lamp_commands(0) == (b"27,0,3,0,255,255,255;",)
    with pytest.raises(ProtocolError):
        lamp_commands(1 << 24)


def test_lcd_exact_four_rows_index_zero():
    rows = ("A" * 20, "B" * 20, "C" * 20, "D" * 20)
    assert lcd_command(rows) == b"25,0," + "".join(rows).encode() + b";"
    assert len(lcd_command(rows)) == 86
    with pytest.raises(ProtocolError, match="four rows"):
        lcd_command(("short", "" * 20, "" * 20, "" * 20))


@pytest.mark.parametrize("bit", range(24))
def test_mask_serialization_matches_firmware_reverse_read_and_unsigned_cast(bit):
    # Stock OutputShifter::OnSet reads arguments backward into uint8_t[3],
    # and MFOutputShifter changes only selected bits in each stored byte.
    stored = [255, 255, 255]
    for frame in lamp_commands(1 << bit):
        (message,) = Parser().feed(frame)
        module, count, value, *masks = map(int, message.arguments)
        assert (module, count) == (0, 3)
        assert all(0 <= mask <= 255 for mask in masks)
        for index, mask in enumerate(reversed(masks)):
            unsigned = mask & 255
            stored[index] = (
                (stored[index] | unsigned) if value else (stored[index] & (~unsigned & 255))
            )
    assert sum(byte << (8 * index) for index, byte in enumerate(stored)) == 1 << bit


def test_high_bit_masks_are_decimal_unsigned_values():
    assert lamp_commands(0x8080FF) == (b"27,0,3,0,127,127,0;", b"27,0,3,1,128,128,255;")


def test_wire_payload_remains_available_for_raw_eeprom_backup():
    (message,) = Parser().feed(b"10,8.0.1.0.OTHER//ENC:;\r\n")
    assert message.arguments == ("8.0.1.0.OTHER/ENC:",)
    assert message.wire == b"10,8.0.1.0.OTHER//ENC:;"
