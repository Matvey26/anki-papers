"use client";

import { ArrowLeft, ChevronLeft, ChevronRight, Minus, Plus, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type { PDFDocumentProxy, TextItem } from "pdfjs-dist/types/src/display/api";
import type { DocumentRecord } from "@/lib/types";

type SelectedWord = { target: string; sentence: string; page: number };
type WordBox = { text: string; left: number; top: number; width: number; height: number; angle: number };

export function PdfReader({
  document,
  savedWords,
  onClose,
  onWord,
}: {
  document: DocumentRecord;
  savedWords: Set<string>;
  onClose: () => void;
  onWord: (selection: SelectedWord) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [page, setPage] = useState(1);
  const [zoom, setZoom] = useState(1);
  const [boxes, setBoxes] = useState<WordBox[]>([]);
  const [pageText, setPageText] = useState("");
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [status, setStatus] = useState("Открываем статью…");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const pdfjs = await import("pdfjs-dist");
        pdfjs.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";
        const response = await fetch(`/api/documents/${document.id}/file`);
        if (!response.ok) throw new Error("PDF не загрузился.");
        const loaded = await pdfjs.getDocument({ data: await response.arrayBuffer() }).promise;
        if (!cancelled) {
          setPdf(loaded);
          setStatus("");
        }
      } catch (error) {
        if (!cancelled) setStatus(error instanceof Error ? error.message : "Не удалось открыть PDF.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [document.id]);

  const renderPage = useCallback(async () => {
    const canvas = canvasRef.current;
    const stage = stageRef.current;
    if (!pdf || !canvas || !stage) return;
    const pdfPage = await pdf.getPage(page);
    const base = pdfPage.getViewport({ scale: 1 });
    const available = Math.max(280, stage.clientWidth - 32);
    const scale = Math.min(2.2, available / base.width) * zoom;
    const viewport = pdfPage.getViewport({ scale });
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(viewport.width * ratio);
    canvas.height = Math.floor(viewport.height * ratio);
    canvas.style.width = `${viewport.width}px`;
    canvas.style.height = `${viewport.height}px`;
    const context = canvas.getContext("2d");
    if (!context) return;
    await pdfPage.render({ canvas, canvasContext: context, viewport, transform: [ratio, 0, 0, ratio, 0, 0] }).promise;
    const text = await pdfPage.getTextContent();
    const nextBoxes: WordBox[] = [];
    const chunks: string[] = [];
    for (const raw of text.items) {
      if (!("str" in raw)) continue;
      const item = raw as TextItem;
      chunks.push(item.str, item.hasEOL ? "\n" : " ");
      const transform = multiply(viewport.transform, item.transform);
      const fontHeight = Math.max(8, Math.hypot(transform[2], transform[3]));
      const totalWidth = Math.max(item.width * scale, fontHeight * item.str.length * 0.34);
      const chars = Math.max(1, item.str.length);
      const parts = [...item.str.matchAll(/[\p{L}\p{N}][\p{L}\p{N}'’-]*/gu)];
      for (const part of parts) {
        const offset = part.index ?? 0;
        nextBoxes.push({
          text: part[0],
          left: transform[4] + (totalWidth * offset) / chars,
          top: transform[5] - fontHeight,
          width: Math.max(8, (totalWidth * part[0].length) / chars),
          height: fontHeight * 1.18,
          angle: Math.atan2(transform[1], transform[0]),
        });
      }
    }
    setBoxes(nextBoxes);
    setPageText(chunks.join("").replace(/[ \t]+/g, " ").replace(/\s*\n\s*/g, " ").trim());
    setSize({ width: viewport.width, height: viewport.height });
  }, [page, pdf, zoom]);

  useEffect(() => {
    void renderPage();
    const observer = new ResizeObserver(() => void renderPage());
    if (stageRef.current) observer.observe(stageRef.current);
    return () => observer.disconnect();
  }, [renderPage]);

  const choose = (word: string) => {
    onWord({ target: word, sentence: findSentence(pageText, word), page });
  };

  return (
    <div className="reader-shell">
      <header className="reader-bar">
        <button className="icon-button reader-back" onClick={onClose} aria-label="Вернуться в библиотеку">
          <ArrowLeft size={20} />
        </button>
        <div className="reader-title">
          <strong>{document.name.replace(/\.pdf$/i, "")}</strong>
          <span>Нажмите на слово, чтобы сделать карточку</span>
        </div>
        <div className="reader-controls" aria-label="Навигация по PDF">
          <button className="icon-button" onClick={() => setZoom((value) => Math.max(0.7, value - 0.15))} aria-label="Уменьшить">
            <Minus size={17} />
          </button>
          <span className="zoom-value">{Math.round(zoom * 100)}%</span>
          <button className="icon-button" onClick={() => setZoom((value) => Math.min(2, value + 0.15))} aria-label="Увеличить">
            <Plus size={17} />
          </button>
        </div>
        <button className="icon-button reader-close" onClick={onClose} aria-label="Закрыть">
          <X size={20} />
        </button>
      </header>
      <main className="pdf-stage" ref={stageRef}>
        {status ? <div className="reader-status">{status}</div> : null}
        <div className="pdf-page" style={{ width: size.width, height: size.height }}>
          <canvas ref={canvasRef} />
          <div className="word-layer" aria-label={`Текст страницы ${page}`}>
            {boxes.map((box, index) => {
              const saved = savedWords.has(normalize(box.text));
              return (
                <button
                  key={`${box.left}-${box.top}-${index}`}
                  className={`word-hit ${saved ? "is-saved" : ""}`}
                  style={{
                    left: box.left,
                    top: box.top,
                    width: box.width,
                    height: box.height,
                    transform: `rotate(${box.angle}rad)`,
                  }}
                  onClick={() => choose(box.text)}
                  title={saved ? `${box.text} — уже в карточках` : `Добавить «${box.text}»`}
                  aria-label={saved ? `${box.text}, уже добавлено` : `Добавить слово ${box.text}`}
                />
              );
            })}
          </div>
        </div>
      </main>
      <footer className="page-dock">
        <button className="icon-button" disabled={page <= 1} onClick={() => setPage((value) => value - 1)} aria-label="Предыдущая страница">
          <ChevronLeft size={20} />
        </button>
        <label>
          <span className="sr-only">Номер страницы</span>
          <input value={page} min={1} max={pdf?.numPages ?? 1} type="number" onChange={(event) => setPage(Math.min(pdf?.numPages ?? 1, Math.max(1, Number(event.target.value))))} />
          <span>из {pdf?.numPages ?? "—"}</span>
        </label>
        <button className="icon-button" disabled={!pdf || page >= pdf.numPages} onClick={() => setPage((value) => value + 1)} aria-label="Следующая страница">
          <ChevronRight size={20} />
        </button>
      </footer>
    </div>
  );
}

function multiply(a: number[], b: number[]): number[] {
  return [
    a[0] * b[0] + a[2] * b[1],
    a[1] * b[0] + a[3] * b[1],
    a[0] * b[2] + a[2] * b[3],
    a[1] * b[2] + a[3] * b[3],
    a[0] * b[4] + a[2] * b[5] + a[4],
    a[1] * b[4] + a[3] * b[5] + a[5],
  ];
}

function findSentence(text: string, target: string): string {
  const lower = text.toLocaleLowerCase("en-US");
  const index = lower.indexOf(target.toLocaleLowerCase("en-US"));
  if (index < 0) return text.slice(0, 280);
  const left = Math.max(text.lastIndexOf(". ", index), text.lastIndexOf("? ", index), text.lastIndexOf("! ", index));
  const candidates = [text.indexOf(". ", index), text.indexOf("? ", index), text.indexOf("! ", index)].filter((value) => value >= 0);
  const right = candidates.length ? Math.min(...candidates) + 1 : Math.min(text.length, index + 220);
  return text.slice(left >= 0 ? left + 2 : Math.max(0, index - 100), right).trim();
}

function normalize(value: string): string {
  return value.normalize("NFKC").toLocaleLowerCase("en-US");
}
