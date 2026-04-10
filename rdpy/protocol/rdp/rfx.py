#
# Non-progressive RemoteFX (RFX) codec decoder (MS-RDPRFX).
# Used for RDPGFX_CODECID_CAVIDEO (0x0003) in WIRE_TO_SURFACE_PDU_1/2.
# Ported from grdp Go implementation (plugin/rdpgfx/rfx.go).
#

import struct
import rdpy.core.log as log
from rdpy.protocol.rdp.rfx_progressive import (
    RFX_TILE_SIZE, RfxQuant, _decode_component, _place_tile_abs,
)

# Block type codes (MS-RDPRFX 2.2.2.1)
WBT_SYNC = 0xCCC0
WBT_CODEC_VERSIONS = 0xCCC1
WBT_CHANNELS = 0xCCC2
WBT_CONTEXT = 0xCCC3
WBT_FRAME_BEGIN = 0xCCC4
WBT_FRAME_END = 0xCCC5
WBT_REGION = 0xCCC6
WBT_EXTENSION = 0xCCC7

# Sub-block types inside TILESET
CBT_REGION = 0xCAC1
CBT_TILESET = 0xCAC2
CBT_TILE = 0xCAC3


class RfxDecoder:
    """Non-progressive RemoteFX tile decoder for CaVideo (0x0003)."""

    def decode(self, data, left, top, surf_data, width, height):
        """Decode RFX data, rendering tiles onto surf_data at (left, top).

        Returns list of (x, y, w, h) bounding rectangles in surface coords.
        """
        rects = []
        quants = None
        offset = 0

        while offset + 6 <= len(data):
            block_type, block_len = struct.unpack_from('<HI', data, offset)

            if block_len < 6 or offset + block_len > len(data):
                log.debug("RFX: invalid block type=0x%04X len=%d offset=%d dataLen=%d" %
                          (block_type, block_len, offset, len(data)))
                break

            # Blocks 0xCCC3-0xCCC7 have 2 extra header bytes (codecId+channelId)
            header_len = 6
            if WBT_CONTEXT <= block_type <= WBT_EXTENSION:
                header_len = 8

            if block_len < header_len:
                break

            content = data[offset + header_len:offset + block_len]

            if block_type in (WBT_SYNC, WBT_CODEC_VERSIONS, WBT_CHANNELS,
                              WBT_CONTEXT, WBT_FRAME_BEGIN, WBT_FRAME_END):
                pass  # infrastructure blocks
            elif block_type == WBT_REGION:
                rects = self._parse_region(content, left, top)
            elif block_type == WBT_EXTENSION:
                quants = self._decode_tileset(content, left, top,
                                              surf_data, width, height)
            else:
                log.debug("RFX: unknown block 0x%04X len=%d" %
                          (block_type, block_len))

            offset += block_len

        if not rects and quants is not None:
            rects = [(left, top, width - left, height - top)]

        return rects

    def _parse_region(self, data, left, top):
        """Extract rectangles from a WBT_REGION block."""
        if len(data) < 7:
            return []

        num_rects = struct.unpack_from('<H', data, 1)[0]
        if num_rects == 0:
            return []

        needed = 3 + num_rects * 8 + 4
        if len(data) < needed:
            return []

        rects = []
        off = 3
        for _ in range(num_rects):
            rx, ry, rw, rh = struct.unpack_from('<HHHH', data, off)
            rects.append((left + rx, top + ry, rw, rh))
            off += 8

        region_type = struct.unpack_from('<H', data, off)[0]
        if region_type != CBT_REGION:
            log.debug("RFX: unexpected regionType 0x%04X (expected CBT_REGION 0xCAC1)" %
                      region_type)

        return rects

    def _decode_tileset(self, data, left, top, surf_data, width, height):
        """Parse and decode all tiles from a WBT_EXTENSION/TILESET block."""
        if len(data) < 14:
            return None

        subtype = struct.unpack_from('<H', data, 0)[0]
        if subtype != CBT_TILESET:
            log.debug("RFX: expected CBT_TILESET (0xCAC2), got 0x%04X" % subtype)
            return None

        num_quant = data[6]
        num_tiles = struct.unpack_from('<H', data, 8)[0]

        off = 14

        # Parse quantization tables (5 bytes each)
        if off + num_quant * 5 > len(data):
            return None
        quants = []
        for _ in range(num_quant):
            quants.append(RfxQuant(data[off:off + 5]))
            off += 5

        # Decode tiles
        for _ in range(num_tiles):
            if off + 6 > len(data):
                break
            tile_block_type, tile_block_len = struct.unpack_from('<HI', data, off)

            if tile_block_type != CBT_TILE:
                log.debug("RFX: expected CBT_TILE (0xCAC3), got 0x%04X" %
                          tile_block_type)
                break
            if tile_block_len < 19 or off + tile_block_len > len(data):
                break

            tile_content = data[off + 6:off + tile_block_len]
            self._decode_tile(tile_content, quants, left, top,
                              surf_data, width, height)
            off += tile_block_len

        return quants

    def _decode_tile(self, data, quants, left, top, output, out_w, out_h):
        """Decode a single non-progressive RFX tile (CBT_TILE).

        Format: quantIdxY(1) quantIdxCb(1) quantIdxCr(1)
                xIdx(2) yIdx(2) YLen(2) CbLen(2) CrLen(2)
                YData CbData CrData
        """
        if len(data) < 13:
            return

        quant_idx_y = data[0]
        quant_idx_cb = data[1]
        quant_idx_cr = data[2]
        x_idx, y_idx, y_len, cb_len, cr_len = struct.unpack_from('<HHHHH', data, 3)

        off = 13
        y_data = data[off:off + y_len] if y_len > 0 and off + y_len <= len(data) else None
        off += y_len
        cb_data = data[off:off + cb_len] if cb_len > 0 and off + cb_len <= len(data) else None
        off += cb_len
        cr_data = data[off:off + cr_len] if cr_len > 0 and off + cr_len <= len(data) else None

        q_y = self._get_quant(quants, quant_idx_y)
        q_cb = self._get_quant(quants, quant_idx_cb)
        q_cr = self._get_quant(quants, quant_idx_cr)

        y_pixels = _decode_component(y_data, q_y)
        cb_pixels = _decode_component(cb_data, q_cb)
        cr_pixels = _decode_component(cr_data, q_cr)

        # Tile pixel position = WTS1 offset + tile grid index * 64
        tile_x = left + x_idx * RFX_TILE_SIZE
        tile_y = top + y_idx * RFX_TILE_SIZE
        _place_tile_abs(y_pixels, cb_pixels, cr_pixels,
                        tile_x, tile_y, output, out_w, out_h)

    @staticmethod
    def _get_quant(quants, idx):
        if idx < len(quants):
            return quants[idx]
        return RfxQuant(b'\x66\x66\x66\x66\x66')
