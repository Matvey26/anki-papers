export type DocumentRecord = {
  id: string;
  name: string;
  size: number;
  createdAt: string;
};

export type DeckRecord = DocumentRecord;

export type CardRecord = {
  id: string;
  documentId: string;
  documentName: string;
  target: string;
  sentence: string;
  page: number;
  translationsRu: string[];
  replacementRu: string;
  alternativesEn: string[];
  csvExportedAt: string | null;
  apkgExportedAt: string | null;
  createdAt: string;
};

export type DashboardData = {
  user: { id: string; username: string };
  documents: DocumentRecord[];
  decks: DeckRecord[];
  cards: CardRecord[];
  newCsvCount: number;
  newApkgCount: number;
};
