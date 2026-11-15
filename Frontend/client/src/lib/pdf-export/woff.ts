/**
 * WOFF 1.0 to SFNT (TrueType/OpenType) conversion.
 *
 * The PDF engine embeds raw SFNT fonts only, while the app already ships
 * Inter as WOFF. A WOFF 1.0 file is the same table set with each table
 * optionally zlib-compressed, so unpacking it avoids bundling a second copy
 * of the typeface.
 */

const WOFF_SIGNATURE = 0x774f4646; // "wOFF"
const WOFF_HEADER_BYTES = 44;
const WOFF_TABLE_ENTRY_BYTES = 20;
const SFNT_HEADER_BYTES = 12;
const SFNT_TABLE_ENTRY_BYTES = 16;
const MAX_TABLES = 256;
const MAX_SFNT_BYTES = 16 * 1024 * 1024;

export type InflateZlib = (compressed: Uint8Array, expectedLength: number) => Promise<Uint8Array>;

async function inflateWithDecompressionStream(compressed: Uint8Array, expectedLength: number): Promise<Uint8Array> {
  const stream = new Blob([compressed as BlobPart]).stream().pipeThrough(new DecompressionStream("deflate"));
  const inflated = new Uint8Array(await new Response(stream).arrayBuffer());
  if (inflated.byteLength !== expectedLength) {
    throw new Error("WOFF table length does not match its directory entry");
  }
  return inflated;
}

export async function woffToSfnt(
  woff: Uint8Array,
  inflate: InflateZlib = inflateWithDecompressionStream,
): Promise<Uint8Array> {
  if (woff.byteLength < WOFF_HEADER_BYTES) throw new Error("WOFF file is truncated");
  const view = new DataView(woff.buffer, woff.byteOffset, woff.byteLength);
  if (view.getUint32(0) !== WOFF_SIGNATURE) throw new Error("Not a WOFF 1.0 font");

  const flavor = view.getUint32(4);
  const tableCount = view.getUint16(12);
  if (tableCount === 0 || tableCount > MAX_TABLES) throw new Error("WOFF table count is out of range");
  if (woff.byteLength < WOFF_HEADER_BYTES + tableCount * WOFF_TABLE_ENTRY_BYTES) {
    throw new Error("WOFF table directory is truncated");
  }

  const tables = Array.from({ length: tableCount }, (_, index) => {
    const entry = WOFF_HEADER_BYTES + index * WOFF_TABLE_ENTRY_BYTES;
    return {
      tag: view.getUint32(entry),
      offset: view.getUint32(entry + 4),
      compressedLength: view.getUint32(entry + 8),
      originalLength: view.getUint32(entry + 12),
      checksum: view.getUint32(entry + 16),
    };
  });

  let sfntLength = SFNT_HEADER_BYTES + tableCount * SFNT_TABLE_ENTRY_BYTES;
  for (const table of tables) {
    if (table.compressedLength > table.originalLength || table.offset + table.compressedLength > woff.byteLength) {
      throw new Error("WOFF table entry is out of bounds");
    }
    sfntLength += (table.originalLength + 3) & ~3;
    if (sfntLength > MAX_SFNT_BYTES) throw new Error("WOFF font is too large");
  }

  const sfnt = new Uint8Array(sfntLength);
  const output = new DataView(sfnt.buffer);
  const entrySelector = Math.floor(Math.log2(tableCount));
  const searchRange = 2 ** entrySelector * 16;
  output.setUint32(0, flavor);
  output.setUint16(4, tableCount);
  output.setUint16(6, searchRange);
  output.setUint16(8, entrySelector);
  output.setUint16(10, tableCount * 16 - searchRange);

  let offset = SFNT_HEADER_BYTES + tableCount * SFNT_TABLE_ENTRY_BYTES;
  for (const [index, table] of tables.entries()) {
    const stored = woff.subarray(table.offset, table.offset + table.compressedLength);
    const data = table.compressedLength < table.originalLength ? await inflate(stored, table.originalLength) : stored;
    const entry = SFNT_HEADER_BYTES + index * SFNT_TABLE_ENTRY_BYTES;
    output.setUint32(entry, table.tag);
    output.setUint32(entry + 4, table.checksum);
    output.setUint32(entry + 8, offset);
    output.setUint32(entry + 12, table.originalLength);
    sfnt.set(data, offset);
    offset += (table.originalLength + 3) & ~3;
  }
  return sfnt;
}
