import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import test from "node:test";
import * as zlib from "node:zlib";
import JSZip from "jszip";
import initSqlJs from "sql.js";
import { buildMergedApkg } from "../lib/exports";
import type { CardRecord } from "../lib/types";

const wasmPath = fileURLToPath(new URL("../node_modules/sql.js/dist/sql-wasm.wasm", import.meta.url));

test("adds new cards without changing existing scheduling", async () => {
  const SQL = await initSqlJs({ locateFile: () => wasmPath });
  const sourceDb = new SQL.Database();
  sourceDb.run("CREATE TABLE decks (id INTEGER PRIMARY KEY)");
  sourceDb.run("INSERT INTO decks VALUES (123)");
  sourceDb.run("CREATE TABLE col (mod INTEGER, models TEXT)");
  sourceDb.run("INSERT INTO col VALUES (?, ?)", [100, JSON.stringify({ 456: { flds: [{}, {}] } })]);
  sourceDb.run("CREATE TABLE config (key TEXT PRIMARY KEY, val TEXT, usn INTEGER, mtime_secs INTEGER)");
  sourceDb.run("INSERT INTO config VALUES ('nextPos', '2', 0, 0)");
  sourceDb.run("CREATE TABLE notes (id INTEGER PRIMARY KEY, guid TEXT, mid INTEGER, mod INTEGER, usn INTEGER, tags TEXT, flds TEXT, sfld TEXT, csum INTEGER, flags INTEGER, data TEXT)");
  sourceDb.run("INSERT INTO notes VALUES (1, 'old-guid', 456, 1, 0, '', 'Old front\x1fOld back', 'Old front', 1, 0, '')");
  sourceDb.run("CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, did INTEGER, ord INTEGER, mod INTEGER, usn INTEGER, type INTEGER, queue INTEGER, due INTEGER, ivl INTEGER, factor INTEGER, reps INTEGER, lapses INTEGER, left INTEGER, odue INTEGER, odid INTEGER, flags INTEGER, data TEXT)");
  sourceDb.run("INSERT INTO cards VALUES (1, 1, 123, 0, 1, 0, 2, 2, 42, 30, 2500, 8, 0, 0, 0, 0, 0, '{}')");
  const sourceSqlite = sourceDb.export();
  const sourceZip = new JSZip();
  sourceZip.file("collection.anki2", sourceSqlite);
  sourceZip.file("media", "{}");
  const source = await sourceZip.generateAsync({ type: "arraybuffer", compression: "STORE" });
  sourceDb.close();

  const card: CardRecord = {
    id: "selection-1",
    documentId: "doc-1",
    documentName: "Research paper.pdf",
    target: "counterintuitive",
    sentence: "The result was counterintuitive, yet consistent.",
    page: 3,
    translationsRu: ["противоречащий интуиции"],
    replacementRu: "противоречащим интуиции",
    alternativesEn: ["surprising", "unexpected"],
    csvExportedAt: null,
    apkgExportedAt: null,
    createdAt: new Date().toISOString(),
  };
  const output = await buildMergedApkg(source, [card], wasmPath);
  const resultZip = await JSZip.loadAsync(await output.arrayBuffer());
  assert.ok(resultZip.file("media"), "media manifest must be preserved");
  const collection = await resultZip.file("collection.anki2")!.async("uint8array");
  const resultDb = new SQL.Database(collection);
  assert.equal(resultDb.exec("SELECT COUNT(*) FROM notes")[0].values[0][0], 3);
  assert.equal(resultDb.exec("SELECT COUNT(*) FROM cards")[0].values[0][0], 3);
  assert.equal(resultDb.exec("SELECT due FROM cards WHERE id = 1")[0].values[0][0], 42);
  assert.deepEqual(resultDb.exec("SELECT DISTINCT queue FROM cards WHERE id != 1")[0].values, [[0]]);
  assert.match(String(resultDb.exec("SELECT flds FROM notes WHERE id != 1 LIMIT 1")[0].values[0][0]), /counterintuitive/);
  resultDb.close();

  const nativeZstd = zlib as unknown as {
    zstdCompressSync(value: Uint8Array): Uint8Array;
    zstdDecompressSync(value: Uint8Array): Uint8Array;
  };
  const modernZip = new JSZip();
  modernZip.file("collection.anki21b", nativeZstd.zstdCompressSync(sourceSqlite));
  modernZip.file("media", "{}");
  const modernSource = await modernZip.generateAsync({ type: "arraybuffer", compression: "STORE" });
  const modernOutput = await buildMergedApkg(modernSource, [card], wasmPath);
  const modernResultZip = await JSZip.loadAsync(await modernOutput.arrayBuffer());
  const packedCollection = await modernResultZip.file("collection.anki21b")!.async("uint8array");
  assert.equal([...packedCollection.slice(0, 4)].map((byte) => byte.toString(16).padStart(2, "0")).join(""), "28b52ffd");
  const modernDb = new SQL.Database(nativeZstd.zstdDecompressSync(packedCollection));
  assert.equal(modernDb.exec("SELECT COUNT(*) FROM cards")[0].values[0][0], 3);
  assert.equal(modernDb.exec("SELECT due FROM cards WHERE id = 1")[0].values[0][0], 42);
  modernDb.close();
});
