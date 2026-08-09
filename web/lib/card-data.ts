import type { CardRecord } from "./types";

type CardRow = {
  id: string;
  document_id: string;
  document_name: string;
  target: string;
  sentence: string;
  page: number;
  translations_ru: string;
  replacement_ru: string;
  alternatives_en: string;
  csv_exported_at: string | null;
  apkg_exported_at: string | null;
  created_at: string;
};

export function mapCard(row: CardRow): CardRecord {
  return {
    id: row.id,
    documentId: row.document_id,
    documentName: row.document_name,
    target: row.target,
    sentence: row.sentence,
    page: row.page,
    translationsRu: parseArray(row.translations_ru),
    replacementRu: row.replacement_ru,
    alternativesEn: parseArray(row.alternatives_en),
    csvExportedAt: row.csv_exported_at,
    apkgExportedAt: row.apkg_exported_at,
    createdAt: row.created_at,
  };
}

export function normalizeTarget(value: string): string {
  return value
    .normalize("NFKC")
    .toLocaleLowerCase("en-US")
    .replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "")
    .replace(/\s+/g, " ");
}

export function cleanArray(values: unknown, max: number): string[] {
  if (!Array.isArray(values)) return [];
  return [...new Set(values.map((value) => String(value).trim()).filter(Boolean))].slice(0, max);
}

function parseArray(value: string): string[] {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}
