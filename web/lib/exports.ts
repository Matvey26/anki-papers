"use client";

import type { CardRecord } from "./types";

export type AnkiRow = { sourceId: string; front: string; back: string; tags: string };
let zstdReady: Promise<typeof import("@bokuweb/zstd-wasm")> | null = null;

export function toAnkiRows(cards: CardRecord[]): AnkiRow[] {
  return cards.flatMap((card) => {
    const articleTag = card.documentName.replace(/\.pdf$/i, "").replace(/[^\p{L}\p{N}_:-]+/gu, "_");
    const sentence = escapeHtml(card.sentence);
    const targetPattern = new RegExp(`(${escapeRegExp(escapeHtml(card.target))})`, "i");
    const meaningFront = sentence.replace(targetPattern, "<b>$1</b>");
    const meaningBack = card.translationsRu.map((value) => `• ${escapeHtml(value)}`).join("<br>");
    const replacement = `<b>${escapeHtml(card.replacementRu || card.translationsRu[0])}</b>`;
    let recallFront = sentence.replace(targetPattern, replacement);
    if (card.alternativesEn.length) {
      recallFront += `<br><small>Нельзя использовать: ${card.alternativesEn.map(escapeHtml).join(", ")}</small>`;
    }
    const commonTags = `article::${articleTag || "article"} page::${card.page}`;
    return [
      { sourceId: card.id, front: meaningFront, back: meaningBack, tags: `${commonTags} card::meaning` },
      { sourceId: card.id, front: recallFront, back: `<b>${escapeHtml(card.target)}</b>`, tags: `${commonTags} card::recall` },
    ];
  });
}

export function downloadCsv(cards: CardRecord[]): void {
  const rows = toAnkiRows(cards);
  const csv = [
    ["Front", "Back", "Tags"],
    ...rows.map((row) => [row.front, row.back, row.tags]),
  ].map((row) => row.map(csvCell).join(",")).join("\r\n");
  saveBlob(new Blob(["\ufeff", csv], { type: "text/csv;charset=utf-8" }), `paperdeck-new-${dateStamp()}.csv`);
}

export async function mergeApkg(source: ArrayBuffer, cards: CardRecord[], sourceName: string): Promise<void> {
  const blob = await buildMergedApkg(source, cards);
  const cleanName = sourceName.replace(/\.apkg$/i, "");
  saveBlob(blob, `${cleanName}-updated-${dateStamp()}.apkg`);
}

export async function buildMergedApkg(
  source: ArrayBuffer,
  cards: CardRecord[],
  sqlWasmUrl = "/sql-wasm.wasm",
  zstdWasmUrl = "/zstd.wasm",
): Promise<Blob> {
  const [{ default: JSZip }, { default: initSqlJs }, zstd] = await Promise.all([
    import("jszip"),
    import("sql.js"),
    getZstd(zstdWasmUrl),
  ]);
  const zip = await JSZip.loadAsync(source);
  const collectionName = ["collection.anki21b", "collection.anki21", "collection.anki2"]
    .find((name) => Boolean(zip.file(name)));
  if (!collectionName) throw new Error("В APKG не найдена коллекция Anki.");
  const packed = await zip.file(collectionName)!.async("uint8array");
  let sqliteBytes = packed;
  const compressed = collectionName.endsWith("21b");
  if (compressed) {
    sqliteBytes = zstd.decompress(packed);
  }
  const SQL = await initSqlJs({ locateFile: () => sqlWasmUrl });
  const db = new SQL.Database(sqliteBytes);
  try {
    const deckId = findDeckId(db);
    const noteTypeId = findTwoFieldNoteType(db);
    const existingFronts = new Set<string>();
    const existingResult = db.exec("SELECT flds FROM notes");
    for (const row of existingResult[0]?.values ?? []) {
      existingFronts.add(normalizeFront(String(row[0]).split("\x1f")[0]));
    }
    const rows = toAnkiRows(cards).filter((row) => {
      const normalized = normalizeFront(row.front);
      if (existingFronts.has(normalized)) return false;
      existingFronts.add(normalized);
      return true;
    });
    const maxCard = scalarNumber(db, "SELECT COALESCE(MAX(id), 0) FROM cards");
    const maxNote = scalarNumber(db, "SELECT COALESCE(MAX(id), 0) FROM notes");
    const baseId = Math.max(Date.now(), maxCard + 1, maxNote + 1);
    const nowSeconds = Math.floor(Date.now() / 1000);
    const nextDue = scalarNumber(db, "SELECT COALESCE(MAX(due), 0) + 1 FROM cards WHERE queue = 0");
    db.run("BEGIN IMMEDIATE");
    try {
      for (let index = 0; index < rows.length; index += 1) {
        const row = rows[index];
        const id = baseId + index;
        const guid = await stableGuid(`${row.sourceId}:${row.tags.includes("meaning") ? "meaning" : "recall"}`);
        const fields = `${row.front}\x1f${row.back}`;
        const sortField = plainText(row.front);
        db.run(
          "INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
          [id, guid, noteTypeId, nowSeconds, -1, ` ${row.tags} `, fields, sortField, await checksum(row.front), 0, ""],
        );
        db.run(
          "INSERT INTO cards VALUES (?, ?, ?, 0, ?, -1, 0, 0, ?, 0, 0, 0, 0, 0, 0, 0, 0, '{}')",
          [id, id, deckId, nowSeconds, nextDue + index],
        );
      }
      try {
        db.run("UPDATE config SET val = ?, usn = -1, mtime_secs = ? WHERE key = 'nextPos'", [String(nextDue + rows.length), nowSeconds]);
      } catch {
        // Legacy collections keep this value elsewhere; new cards remain valid.
      }
      db.run("UPDATE col SET mod = ?", [Date.now()]);
      db.run("COMMIT");
    } catch (error) {
      db.run("ROLLBACK");
      throw error;
    }
    const exported = db.export();
    // Copy out of WASM memory before JSZip performs asynchronous work.
    const output = compressed ? Uint8Array.from(zstd.compress(exported, 10)) : exported;
    zip.file(collectionName, output, { compression: "STORE" });
    return await zip.generateAsync({
      type: "blob",
      compression: "STORE",
      mimeType: "application/octet-stream",
    });
  } finally {
    db.close();
  }
}

function getZstd(wasmUrl: string): Promise<typeof import("@bokuweb/zstd-wasm")> {
  if (!zstdReady) {
    zstdReady = import("@bokuweb/zstd-wasm").then(async (module) => {
      // Package type declarations resolve Node signature; Vite uses browser build with URL support.
      // @ts-expect-error Browser entry accepts an optional WASM URL.
      await module.init(wasmUrl);
      return module;
    });
  }
  return zstdReady;
}

function findDeckId(db: import("sql.js").Database): number {
  try {
    return scalarNumber(db, "SELECT id FROM decks ORDER BY id LIMIT 1");
  } catch {
    const result = db.exec("SELECT decks FROM col LIMIT 1");
    const decks = JSON.parse(String(result[0]?.values[0]?.[0] ?? "{}"));
    const id = Object.keys(decks)[0];
    if (!id) throw new Error("В APKG нет колоды.");
    return Number(id);
  }
}

function findTwoFieldNoteType(db: import("sql.js").Database): number {
  try {
    const result = db.exec("SELECT models FROM col LIMIT 1");
    const models = JSON.parse(String(result[0]?.values[0]?.[0] ?? "{}")) as Record<string, { flds?: unknown[] }>;
    const match = Object.entries(models).find(([, model]) => model.flds?.length === 2);
    if (match) return Number(match[0]);
  } catch {
    // Modern normalized collections fall through to their most-used note type.
  }
  return scalarNumber(db, "SELECT mid FROM notes GROUP BY mid ORDER BY COUNT(*) DESC LIMIT 1");
}

function scalarNumber(db: import("sql.js").Database, sql: string): number {
  const result = db.exec(sql);
  const value = result[0]?.values[0]?.[0];
  if (value === undefined || value === null) throw new Error("APKG имеет неподдерживаемую структуру.");
  return Number(value);
}

async function stableGuid(value: string): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value))).slice(0, 9);
  let binary = "";
  digest.forEach((byte) => (binary += String.fromCharCode(byte)));
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
}

async function checksum(value: string): Promise<number> {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-1", new TextEncoder().encode(plainText(value))));
  return (((digest[0] << 24) >>> 0) + (digest[1] << 16) + (digest[2] << 8) + digest[3]) >>> 0;
}

function normalizeFront(value: string): string {
  return plainText(value).normalize("NFKC").toLocaleLowerCase("en-US").replace(/\s+/g, " ").trim();
}

function plainText(value: string): string {
  return value
    .replace(/<[^>]*>/g, "")
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&quot;", '"')
    .replaceAll("&#39;", "'")
    .replaceAll("&amp;", "&");
}

function escapeHtml(value: string): string {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function csvCell(value: string): string {
  return `"${value.replaceAll('"', '""')}"`;
}

function dateStamp(): string {
  return new Date().toISOString().slice(0, 10);
}

function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
}
