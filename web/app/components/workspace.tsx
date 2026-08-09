"use client";

import {
  BookOpen,
  Check,
  ChevronRight,
  Clock3,
  Download,
  FileArchive,
  FileText,
  Layers3,
  Library,
  LoaderCircle,
  LogOut,
  Plus,
  Search,
  Sparkles,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { downloadCsv, mergeApkg } from "@/lib/exports";
import type { CardRecord, DashboardData, DocumentRecord } from "@/lib/types";
import { PdfReader } from "./pdf-reader";

type View = "library" | "cards" | "exports";
type WordDraft = { documentId: string; target: string; sentence: string; page: number };

export function Workspace() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [needsAuth, setNeedsAuth] = useState(false);
  const [view, setView] = useState<View>("library");
  const [reader, setReader] = useState<DocumentRecord | null>(null);
  const [draft, setDraft] = useState<WordDraft | null>(null);
  const [toast, setToast] = useState("");
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (response.status === 401) {
      setNeedsAuth(true);
      setData(null);
    } else if (response.ok) {
      setData(await response.json() as DashboardData);
      setNeedsAuth(false);
    }
    setLoading(false);
  }, []);

  // Initial remote-state synchronization.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 3400);
    return () => window.clearTimeout(timer);
  }, [toast]);

  if (loading) return <LoadingScreen />;
  if (needsAuth || !data) return <AuthScreen onDone={refresh} />;
  if (reader) {
    return (
      <>
        <PdfReader
          document={reader}
          savedWords={new Set(data.cards.map((card) => normalize(card.target)))}
          onClose={() => setReader(null)}
          onWord={(selection) => setDraft({ ...selection, documentId: reader.id })}
        />
        {draft ? (
          <CardComposer
            draft={draft}
            onClose={() => setDraft(null)}
            onSaved={async () => {
              setDraft(null);
              setToast("Карточки сохранены");
              await refresh();
            }}
          />
        ) : null}
        {toast ? <Toast text={toast} /> : null}
      </>
    );
  }

  return (
    <div className="app-shell">
      <Sidebar
        view={view}
        data={data}
        onView={setView}
        onLogout={async () => {
          await fetch("/api/auth/logout", { method: "POST" });
          await refresh();
        }}
      />
      <main className="main-panel">
        {view === "library" ? (
          <LibraryView data={data} onOpen={setReader} onRefresh={refresh} onToast={setToast} />
        ) : view === "cards" ? (
          <CardsView data={data} onRefresh={refresh} onToast={setToast} />
        ) : (
          <ExportsView data={data} onRefresh={refresh} onToast={setToast} />
        )}
      </main>
      <MobileNav view={view} counts={data} onView={setView} />
      {toast ? <Toast text={toast} /> : null}
    </div>
  );
}

function AuthScreen({ onDone }: { onDone: () => Promise<void> }) {
  const [register, setRegister] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    const response = await fetch(`/api/auth/${register ? "register" : "login"}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: form.get("username"), password: form.get("password") }),
    });
    const result = await response.json() as { error?: string };
    if (!response.ok) setError(result.error ?? "Что-то пошло не так.");
    else await onDone();
    setBusy(false);
  };

  return (
    <main className="auth-page">
      <section className="auth-story">
        <Brand />
        <div className="auth-copy">
          <span className="eyebrow">Читайте. Замечайте. Запоминайте.</span>
          <h1>Статья сегодня.<br /><em>Слово навсегда.</em></h1>
          <p>Удобная PDF-читалка превращает незнакомые слова в новые карточки Anki — без повторов и ручной возни.</p>
        </div>
        <div className="auth-sample" aria-hidden="true">
          <div className="sample-line wide" />
          <div className="sample-line" />
          <div className="sample-text">The result was <mark>counterintuitive</mark>, yet remarkably consistent.</div>
          <div className="sample-note"><Sparkles size={15} /> нелогичный · противоречащий интуиции</div>
        </div>
      </section>
      <section className="auth-form-wrap">
        <form className="auth-card" onSubmit={submit}>
          <div className="mobile-brand"><Brand /></div>
          <span className="eyebrow">{register ? "Новый профиль" : "С возвращением"}</span>
          <h2>{register ? "Начать читать" : "Войти в Paperdeck"}</h2>
          <p>{register ? "Почта не нужна. Только логин и пароль." : "Ваши статьи и карточки ждут."}</p>
          <label>Логин<input name="username" autoComplete="username" minLength={3} maxLength={32} required placeholder="например, matvey" /></label>
          <label>Пароль<input name="password" type="password" autoComplete={register ? "new-password" : "current-password"} minLength={6} required placeholder="минимум 6 символов" /></label>
          {error ? <div className="form-error">{error}</div> : null}
          <button className="primary-button full" disabled={busy}>
            {busy ? <LoaderCircle className="spin" size={18} /> : null}
            {register ? "Создать профиль" : "Войти"}
          </button>
          <button type="button" className="text-button" onClick={() => { setRegister(!register); setError(""); }}>
            {register ? "Уже есть профиль? Войти" : "Нет профиля? Зарегистрироваться"}
          </button>
        </form>
      </section>
    </main>
  );
}

function Sidebar({ view, data, onView, onLogout }: { view: View; data: DashboardData; onView: (view: View) => void; onLogout: () => void }) {
  return (
    <aside className="sidebar">
      <Brand />
      <nav>
        <NavButton active={view === "library"} icon={<Library size={19} />} label="Библиотека" onClick={() => onView("library")} />
        <NavButton active={view === "cards"} icon={<Layers3 size={19} />} label="Карточки" badge={data.cards.length} onClick={() => onView("cards")} />
        <NavButton active={view === "exports"} icon={<Download size={19} />} label="Выгрузки" badge={data.newCsvCount || undefined} onClick={() => onView("exports")} />
      </nav>
      <div className="sidebar-tip">
        <Sparkles size={18} />
        <strong>Никаких дублей</strong>
        <span>Уже сохранённые слова повторно не выгружаются.</span>
      </div>
      <div className="profile-row">
        <span className="avatar">{data.user.username.slice(0, 1).toUpperCase()}</span>
        <div><strong>{data.user.username}</strong><span>{data.documents.length} статей</span></div>
        <button className="icon-button" onClick={onLogout} aria-label="Выйти"><LogOut size={17} /></button>
      </div>
    </aside>
  );
}

function LibraryView({ data, onOpen, onRefresh, onToast }: { data: DashboardData; onOpen: (doc: DocumentRecord) => void; onRefresh: () => Promise<void>; onToast: (text: string) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [uploading, setUploading] = useState(false);
  const documents = data.documents.filter((document) => document.name.toLocaleLowerCase("ru-RU").includes(query.toLocaleLowerCase("ru-RU")));
  const upload = async (file?: File) => {
    if (!file) return;
    setUploading(true);
    const form = new FormData();
    form.set("kind", "pdf");
    form.set("file", file);
    const response = await fetch("/api/uploads", { method: "POST", body: form });
    const result = await response.json() as { error?: string };
    onToast(response.ok ? "Статья добавлена" : result.error ?? "Загрузка не удалась");
    if (response.ok) await onRefresh();
    setUploading(false);
    if (input.current) input.current.value = "";
  };
  return (
    <div className="content-wrap">
      <PageHeader eyebrow="Моя библиотека" title={<>Что читаем <em>сегодня?</em></>} action={
        <button className="primary-button" onClick={() => input.current?.click()} disabled={uploading}>
          {uploading ? <LoaderCircle className="spin" size={18} /> : <Plus size={18} />} Добавить PDF
        </button>
      } />
      <input ref={input} className="sr-only" type="file" accept="application/pdf,.pdf" onChange={(event) => void upload(event.target.files?.[0])} />
      <div className="stats-strip">
        <Stat value={data.documents.length} label="статей" />
        <Stat value={data.cards.length} label="слов собрано" />
        <Stat value={data.newCsvCount} label="новых карточек" accent />
        <div className="search-box"><Search size={18} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Найти статью" aria-label="Найти статью" /></div>
      </div>
      {documents.length ? (
        <section className="document-grid">
          {documents.map((document, index) => {
            const count = data.cards.filter((card) => card.documentId === document.id).length;
            return (
              <button className={`document-card tone-${index % 4}`} key={document.id} onClick={() => onOpen(document)}>
                <div className="document-paper">
                  <span className="pdf-label">PDF</span>
                  <FileText size={42} strokeWidth={1.3} />
                  <div className="paper-lines"><i /><i /><i /></div>
                </div>
                <div className="document-info">
                  <strong>{document.name.replace(/\.pdf$/i, "")}</strong>
                  <span>{formatBytes(document.size)} · {formatDate(document.createdAt)}</span>
                  <div className="document-meta"><span><Layers3 size={15} /> {count} слов</span><span>Читать <ChevronRight size={15} /></span></div>
                </div>
              </button>
            );
          })}
        </section>
      ) : (
        <button className="empty-upload" onClick={() => input.current?.click()}>
          <span><Upload size={25} /></span>
          <strong>{query ? "Ничего не найдено" : "Добавьте первую статью"}</strong>
          <p>{query ? "Попробуйте другой запрос." : "PDF до 50 МБ. Он останется только в вашем профиле."}</p>
        </button>
      )}
    </div>
  );
}

function CardsView({ data, onRefresh, onToast }: { data: DashboardData; onRefresh: () => Promise<void>; onToast: (text: string) => void }) {
  const [query, setQuery] = useState("");
  const cards = data.cards.filter((card) => `${card.target} ${card.sentence} ${card.translationsRu.join(" ")}`.toLocaleLowerCase("ru-RU").includes(query.toLocaleLowerCase("ru-RU")));
  return (
    <div className="content-wrap">
      <PageHeader eyebrow="Словарный запас" title={<>Собранные <em>слова</em></>} />
      <div className="cards-toolbar">
        <div className="search-box"><Search size={18} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Слово или перевод" aria-label="Искать карточки" /></div>
        <span>{data.cards.length} слов · {data.cards.length * 2} карточек</span>
      </div>
      <section className="cards-list">
        {cards.map((card) => (
          <article className="vocab-card" key={card.id}>
            <div className="word-column"><strong>{card.target}</strong><span>{card.translationsRu.join(" · ")}</span></div>
            <p>{highlightWord(card.sentence, card.target)}</p>
            <div className="card-flags">
              <span>{card.documentName.replace(/\.pdf$/i, "")} · стр. {card.page}</span>
              <span className={card.csvExportedAt ? "done" : "new"}>{card.csvExportedAt ? <Check size={13} /> : <Clock3 size={13} />}{card.csvExportedAt ? "CSV" : "новое"}</span>
            </div>
            <button className="icon-button danger" aria-label={`Удалить ${card.target}`} onClick={async () => {
              await fetch(`/api/cards/${card.id}`, { method: "DELETE" });
              onToast("Карточка удалена");
              await onRefresh();
            }}><Trash2 size={16} /></button>
          </article>
        ))}
        {!cards.length ? <div className="empty-state"><Layers3 size={30} /><strong>Здесь появятся выбранные слова</strong><span>Откройте PDF и нажмите на незнакомое слово.</span></div> : null}
      </section>
    </div>
  );
}

function ExportsView({ data, onRefresh, onToast }: { data: DashboardData; onRefresh: () => Promise<void>; onToast: (text: string) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState<"csv" | "apkg" | "upload" | null>(null);
  const [deckId, setDeckId] = useState(data.decks[0]?.id ?? "");
  const deck = data.decks.find((item) => item.id === deckId);

  const mark = async (channel: "csv" | "apkg", cards: CardRecord[]) => {
    await fetch("/api/cards/mark-exported", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel, ids: cards.map((card) => card.id) }),
    });
    await onRefresh();
  };
  const getNewCards = async (channel: "csv" | "apkg") => {
    const response = await fetch(`/api/cards?channel=${channel}`);
    const payload = await response.json() as { cards: CardRecord[] };
    return payload.cards;
  };
  const exportCsv = async () => {
    setBusy("csv");
    const cards = await getNewCards("csv");
    if (!cards.length) onToast("Новых карточек для CSV пока нет");
    else {
      downloadCsv(cards);
      await mark("csv", cards);
      onToast(`Скачано ${cards.length * 2} новых карточек`);
    }
    setBusy(null);
  };
  const exportApkg = async () => {
    if (!deck) return;
    setBusy("apkg");
    try {
      const cards = await getNewCards("apkg");
      if (!cards.length) onToast("Новых карточек для APKG пока нет");
      else {
        const response = await fetch(`/api/decks/${deck.id}/file`);
        if (!response.ok) throw new Error("Не удалось получить исходную колоду.");
        await mergeApkg(await response.arrayBuffer(), cards, deck.name);
        await mark("apkg", cards);
        onToast(`В колоду добавлено до ${cards.length * 2} карточек`);
      }
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Не удалось собрать APKG");
    }
    setBusy(null);
  };
  const uploadDeck = async (file?: File) => {
    if (!file) return;
    setBusy("upload");
    const form = new FormData();
    form.set("kind", "apkg");
    form.set("file", file);
    const response = await fetch("/api/uploads", { method: "POST", body: form });
    const payload = await response.json() as { file?: { id: string }; error?: string };
    if (response.ok && payload.file) {
      setDeckId(payload.file.id);
      await onRefresh();
      onToast("Колода сохранена");
    } else onToast(payload.error ?? "Не удалось загрузить колоду");
    setBusy(null);
  };
  return (
    <div className="content-wrap">
      <PageHeader eyebrow="Без повторов" title={<>Заберите новое.<br /><em>Старое останется.</em></>} />
      <p className="exports-intro">После успешной выгрузки карточки помечаются как скачанные. Следующий файл содержит только новые слова.</p>
      <section className="export-grid">
        <article className="export-card csv-card">
          <span className="export-icon"><FileText size={25} /></span>
          <div><span className="eyebrow">Простой импорт</span><h3>Новые карточки в CSV</h3></div>
          <strong className="export-count">{data.newCsvCount}<small>карточек ждут</small></strong>
          <ul><li><Check size={15} /> UTF-8 и готовый HTML</li><li><Check size={15} /> Front, Back, Tags</li><li><Check size={15} /> Только ещё не скачанные</li></ul>
          <button className="primary-button full" disabled={busy !== null || !data.newCsvCount} onClick={() => void exportCsv()}>
            {busy === "csv" ? <LoaderCircle className="spin" size={18} /> : <Download size={18} />} Скачать CSV
          </button>
        </article>
        <article className="export-card apkg-card">
          <span className="export-icon"><FileArchive size={25} /></span>
          <div><span className="eyebrow">Колода целиком</span><h3>Обновлённый APKG</h3></div>
          <strong className="export-count">{data.newApkgCount}<small>новых карточек</small></strong>
          {data.decks.length ? (
            <label className="deck-select">Исходная колода<select value={deckId} onChange={(event) => setDeckId(event.target.value)}>{data.decks.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
          ) : (
            <button className="deck-drop" onClick={() => input.current?.click()}><Upload size={20} /><span><strong>Добавьте текущую колоду</strong><small>APKG до 80 МБ</small></span></button>
          )}
          <p className="schedule-note"><Clock3 size={17} /><span><strong>Расписание сохранится.</strong> Меняются только новые записи; существующие карточки не трогаются.</span></p>
          <div className="split-actions">
            {data.decks.length ? <button className="secondary-button" onClick={() => input.current?.click()}><Upload size={17} /> Другая</button> : null}
            <button className="primary-button" disabled={busy !== null || !deck || !data.newApkgCount} onClick={() => void exportApkg()}>{busy === "apkg" ? <LoaderCircle className="spin" size={18} /> : <Download size={18} />} Скачать APKG</button>
          </div>
          <input ref={input} className="sr-only" type="file" accept=".apkg,application/zip" onChange={(event) => void uploadDeck(event.target.files?.[0])} />
        </article>
      </section>
      <div className="privacy-note"><span className="avatar"><Check size={17} /></span><p><strong>APKG собирается прямо в вашем браузере.</strong><br />Расписание не разбирается сервером и остаётся внутри файла.</p></div>
    </div>
  );
}

function CardComposer({ draft, onClose, onSaved }: { draft: WordDraft; onClose: () => void; onSaved: () => Promise<void> }) {
  const [translations, setTranslations] = useState("");
  const [replacement, setReplacement] = useState("");
  const [alternatives, setAlternatives] = useState("");
  const [busy, setBusy] = useState<"ai" | "save" | null>(null);
  const [error, setError] = useState("");

  const enrich = async () => {
    setBusy("ai"); setError("");
    const response = await fetch("/api/cards/enrich", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: draft.target, sentence: draft.sentence }),
    });
    const payload = await response.json() as { translationsRu?: string[]; replacementRu?: string; alternativesEn?: string[]; error?: string };
    if (response.ok) {
      setTranslations(payload.translationsRu?.join(", ") ?? "");
      setReplacement(payload.replacementRu ?? "");
      setAlternatives(payload.alternativesEn?.join(", ") ?? "");
    } else setError(payload.error ?? "Автоперевод недоступен.");
    setBusy(null);
  };
  const save = async (event: FormEvent) => {
    event.preventDefault(); setBusy("save"); setError("");
    const response = await fetch("/api/cards", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...draft,
        translationsRu: splitList(translations),
        replacementRu: replacement,
        alternativesEn: splitList(alternatives),
      }),
    });
    const payload = await response.json() as { error?: string };
    if (response.ok) await onSaved();
    else setError(payload.error ?? "Не удалось сохранить.");
    setBusy(null);
  };
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <form className="composer" onSubmit={save}>
        <button type="button" className="icon-button modal-close" onClick={onClose} aria-label="Закрыть"><X size={19} /></button>
        <span className="eyebrow">Страница {draft.page} · Новое слово</span>
        <h2>{draft.target}</h2>
        <blockquote>{highlightWord(draft.sentence, draft.target)}</blockquote>
        <button type="button" className="ai-button" disabled={busy !== null} onClick={() => void enrich()}>{busy === "ai" ? <LoaderCircle className="spin" size={17} /> : <Sparkles size={17} />} Заполнить перевод автоматически</button>
        <label>Переводы на русском<input value={translations} onChange={(event) => setTranslations(event.target.value)} required placeholder="например: препятствовать, мешать" /></label>
        <label>Русская замена в предложении<input value={replacement} onChange={(event) => setReplacement(event.target.value)} placeholder="например: препятствует" /></label>
        <label>Допустимые английские синонимы<input value={alternatives} onChange={(event) => setAlternatives(event.target.value)} placeholder="hinder, block, slow down" /></label>
        {error ? <div className="form-error">{error}</div> : null}
        <div className="composer-actions"><button type="button" className="secondary-button" onClick={onClose}>Отмена</button><button className="primary-button" disabled={busy !== null}>{busy === "save" ? <LoaderCircle className="spin" size={18} /> : <Plus size={18} />} Добавить 2 карточки</button></div>
      </form>
    </div>
  );
}

function Brand() { return <div className="brand"><span className="brand-mark"><BookOpen size={21} /></span><strong>paperdeck</strong></div>; }
function LoadingScreen() { return <main className="loading-screen"><Brand /><LoaderCircle className="spin" size={26} /><span>Готовим библиотеку…</span></main>; }
function Toast({ text }: { text: string }) { return <div className="toast"><Check size={17} />{text}</div>; }
function NavButton({ active, icon, label, badge, onClick }: { active: boolean; icon: React.ReactNode; label: string; badge?: number; onClick: () => void }) { return <button className={active ? "active" : ""} onClick={onClick}>{icon}<span>{label}</span>{badge ? <b>{badge}</b> : null}</button>; }
function MobileNav({ view, counts, onView }: { view: View; counts: DashboardData; onView: (view: View) => void }) { return <nav className="mobile-nav"><NavButton active={view === "library"} icon={<Library size={20} />} label="Статьи" onClick={() => onView("library")} /><NavButton active={view === "cards"} icon={<Layers3 size={20} />} label="Карточки" badge={counts.cards.length} onClick={() => onView("cards")} /><NavButton active={view === "exports"} icon={<Download size={20} />} label="Выгрузить" badge={counts.newCsvCount || undefined} onClick={() => onView("exports")} /></nav>; }
function PageHeader({ eyebrow, title, action }: { eyebrow: string; title: React.ReactNode; action?: React.ReactNode }) { return <header className="page-header"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1></div>{action}</header>; }
function Stat({ value, label, accent }: { value: number; label: string; accent?: boolean }) { return <div className={`stat ${accent ? "accent" : ""}`}><strong>{value}</strong><span>{label}</span></div>; }
function formatBytes(value: number) { return value < 1024 * 1024 ? `${Math.ceil(value / 1024)} КБ` : `${(value / 1024 / 1024).toFixed(1)} МБ`; }
function formatDate(value: string) { return new Intl.DateTimeFormat("ru", { day: "numeric", month: "short" }).format(new Date(value)); }
function normalize(value: string) { return value.normalize("NFKC").toLocaleLowerCase("en-US"); }
function splitList(value: string) { return value.split(/[,;\n]/).map((item) => item.trim()).filter(Boolean); }
function highlightWord(sentence: string, target: string) { const index = sentence.toLocaleLowerCase("en-US").indexOf(target.toLocaleLowerCase("en-US")); return index < 0 ? sentence : <>{sentence.slice(0, index)}<mark>{sentence.slice(index, index + target.length)}</mark>{sentence.slice(index + target.length)}</>; }
