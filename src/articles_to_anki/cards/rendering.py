"""cards / rendering."""
from __future__ import annotations

import csv
import html
import io
import json
import re
import sqlite3
from pathlib import Path
from typing import Any


def cards_to_csv(cards: list[sqlite3.Row]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=["Front", "Back", "Tags"], quoting=csv.QUOTE_ALL)
    writer.writeheader()
    for card in cards:
        if card["semantic_version"] == 1:
            for row in semantic_card_rows(card):
                writer.writerow(row)
            continue
        translations = json.loads(card["translations_json"])
        alternatives = json.loads(card["alternatives_json"])
        front = emphasize_target(card["sentence"], card["target"], html.escape(card["target"]))
        back = "<br>".join(f"• {html.escape(value)}" for value in translations)
        tag = re.sub(r"[^A-Za-z0-9_:-]+", "_", Path(card["document_name"]).stem).strip("_") or "article"
        common = f"article::{tag} page::{card['page']} anki_papers::{card['id']}"
        writer.writerow({"Front": front, "Back": back, "Tags": f"{common} card::meaning"})
        replacement = f"<b>{html.escape(card['replacement'])}</b>"
        recall = emphasize_target(card["sentence"], card["target"], replacement, replacement_is_html=True)
        if alternatives:
            recall += f"<br><small>Нельзя использовать: {', '.join(html.escape(value) for value in alternatives)}</small>"
        writer.writerow(
            {"Front": recall, "Back": f"<b>{html.escape(card['target'])}</b>", "Tags": f"{common} card::recall"}
        )
    return ("\ufeff" + stream.getvalue()).encode("utf-8")


def semantic_card_rows(card: sqlite3.Row) -> list[dict[str, str]]:
    """Two fields only: works in CSV and APKG Basic-like note types."""
    contexts = json.loads(card["contexts_json"] or "[]")
    if not contexts:
        raise ValueError("Semantic card has no contexts")
    translations_values = json.loads(card["translations_json"])
    fallback_replacement = str(translations_values[0])
    def render(selected: list[dict[str, Any]]) -> list[dict[str, str]]:
        rendered = []
        for item in selected:
            target = str(item["target"])
            sentence = str(item["sentence"])
            replacement = _compact_semantic_replacement(
                str(item["replacement"]),
                fallback=fallback_replacement,
                english_surface=target,
            )
            rendered.append({
                "front": emphasize_target(sentence, target, html.escape(target)),
                "recall": emphasize_target(
                    sentence,
                    target,
                    f"<b>{html.escape(replacement)}</b>",
                    replacement_is_html=True,
                ) + _semantic_recall_distractors_html(item),
                "answer": html.escape(target),
                "source": html.escape(str(item["source"])),
            })
        return rendered

    meaning_rendered = render(
        [item for item in contexts if _semantic_context_enabled(item, "meaning")]
    )
    recall_rendered = render(
        [item for item in contexts if _semantic_context_enabled(item, "recall")]
    )
    if not meaning_rendered or not recall_rendered:
        raise ValueError("Semantic card has no contexts for one direction")
    meaning_payload = html.escape(
        json.dumps(meaning_rendered, ensure_ascii=False), quote=True
    )
    recall_payload = html.escape(
        json.dumps(recall_rendered, ensure_ascii=False), quote=True
    )
    key = html.escape(str(card["id"]), quote=True)
    front = _semantic_front_html(meaning_payload, key, "front", meaning_rendered[0])
    recall = _semantic_front_html(recall_payload, key, "recall", recall_rendered[0])
    translations = "<br>".join(
        f"• {html.escape(value)}" for value in translations_values
    )
    sense = html.escape(str(card["sense_definition_en"]))
    family_tag = re.sub(
        r"[^A-Za-z0-9_:-]+",
        "_",
        str(card["family_key"] or card["lemma"]),
    ).strip("_") or "word"
    common = f"anki_papers::{card['id']} semantic::v1 family::{family_tag}"
    meaning_back = _semantic_back_html(
        meaning_payload,
        key,
        "front",
        meaning_rendered[0],
        f"<br>{translations}<br><small>{sense}</small>",
    )
    recall_back = _semantic_back_html(
        recall_payload,
        key,
        "recall",
        recall_rendered[0],
        "",
    )
    return [
        {"Front": front, "Back": meaning_back, "Tags": f"{common} card::meaning"},
        {"Front": recall, "Back": recall_back, "Tags": f"{common} card::recall"},
    ]


def _semantic_context_enabled(context: dict[str, Any], direction: str) -> bool:
    directions = context.get("directions")
    return not directions or direction in directions


def _semantic_recall_distractors_html(context: dict[str, Any]) -> str:
    valid = context.get("valid_substitutes_en") or []
    related = (
        context.get("valid_related_en")
        or context.get("related_but_uninsertable_en")
        or context.get("meaning_related_non_substitutes_en")
        or []
    )
    valid_normalized = {str(value).casefold() for value in valid}
    related = [
        value for value in related if str(value).casefold() not in valid_normalized
    ]
    hints = []
    if valid:
        values = ", ".join(html.escape(str(value)) for value in valid)
        hints.append(f"Подходит, но не целевой ответ: {values}")
    if related:
        values = ", ".join(html.escape(str(value)) for value in related)
        hints.append(f"Близко по смыслу: {values}")
    if not hints:
        return ""
    return "<br><small>" + "<br>".join(hints) + "</small>"


def _semantic_front_html(
    payload: str,
    key: str,
    side: str,
    fallback: dict[str, str],
) -> str:
    # sessionStorage keeps question and answer on same context in clients that reload the page.
    fallback_html = fallback[side]
    return (
        f'<div class="anki-papers-semantic" data-key="{key}" data-side="{side}" '
        f'data-contexts="{payload}">{fallback_html}</div><script>(function(){{'
        'var root=document.currentScript.previousElementSibling,items=JSON.parse(root.dataset.contexts),'
        'key="anki-papers-context:"+root.dataset.key+":"+root.dataset.side,stored=null;'
        'try{stored=JSON.parse(sessionStorage.getItem(key)||"null")}catch(e){}'
        'var now=Date.now(),valid=stored&&stored.until>now&&Number.isInteger(stored.index)&&'
        'stored.index>=0&&stored.index<items.length,index=valid?stored.index:Math.floor(Math.random()*items.length);'
        'try{sessionStorage.setItem(key,JSON.stringify({index:index,until:now+120000}))}catch(e){}'
        'var item=items[index];root.innerHTML=item[root.dataset.side];'
        '})();</script>'
    )


def _semantic_back_html(
    payload: str,
    key: str,
    side: str,
    fallback: dict[str, str],
    details: str,
) -> str:
    return (
        f'<div class="anki-papers-semantic-answer" data-key="{key}" data-side="{side}" '
        f'data-contexts="{payload}"><b>{fallback["answer"]}</b></div>'
        '<script>(function(){var root=document.currentScript.previousElementSibling,'
        'items=JSON.parse(root.dataset.contexts),key="anki-papers-context:"+root.dataset.key+'
        '":"+root.dataset.side,stored=null;try{stored=JSON.parse(sessionStorage.getItem(key)||"null")}'
        'catch(e){}var valid=stored&&Number.isInteger(stored.index)&&stored.index>=0&&'
        'stored.index<items.length,index=valid?stored.index:0;root.innerHTML="<b>"+'
        'items[index].answer+"</b>";})();</script>'
        f"{details}"
    )


def emphasize_target(sentence: str, target: str, replacement: str, *, replacement_is_html: bool = False) -> str:
    match = re.search(re.escape(target), sentence, flags=re.IGNORECASE)
    if not match:
        return html.escape(sentence)
    selected = replacement if replacement_is_html else f"<b>{replacement}</b>"
    return f"{html.escape(sentence[:match.start()])}{selected}{html.escape(sentence[match.end():])}"


def _compact_semantic_replacement(
    value: str,
    *,
    fallback: str,
    english_surface: str,
) -> str:
    cleaned = " ".join(value.split())
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9-]+", cleaned)
    surface_words = re.findall(r"[A-Za-z0-9-]+", english_surface)
    maximum_words = min(8, max(5, len(surface_words) * 2 + 1))
    if len(words) > maximum_words or any(
        mark in cleaned for mark in ("\n", ".", ";", "!", "?")
    ):
        return fallback
    return cleaned or fallback
