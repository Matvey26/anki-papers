"""Render worker card payloads for Anki notes."""
from __future__ import annotations

import html
import json
import re
from typing import Any

from .errors import PermanentSyncError


def _card_sides(card: dict[str, Any], direction: str) -> tuple[str, str]:
    if card.get("semantic"):
        return _semantic_sides(card, direction)
    translations = card["translations"]
    alternatives = card["alternatives"]
    if direction == "meaning":
        front = _replace_target(card["sentence"], card["target"], html.escape(card["target"]))
        back = "<br>".join(f"• {html.escape(value)}" for value in translations)
        return front, back
    replacement = f"<b>{html.escape(card['replacement'])}</b>"
    front = _replace_target(card["sentence"], card["target"], replacement, raw=True)
    if alternatives:
        front += "<br><small>Нельзя использовать: " + ", ".join(
            html.escape(value) for value in alternatives
        ) + "</small>"
    return front, f"<b>{html.escape(card['target'])}</b>"


def _semantic_sides(card: dict[str, Any], direction: str) -> tuple[str, str]:
    contexts = [
        context
        for context in (card.get("contexts") or [])
        if _semantic_context_enabled(context, direction)
    ]
    if not contexts:
        raise PermanentSyncError("semantic_card_without_contexts")
    values = []
    fallback_replacement = str(card["translations"][0])
    for context in contexts:
        target = str(context["target"])
        sentence = str(context["sentence"])
        replacement = _compact_semantic_replacement(
            str(context["replacement"]),
            fallback=fallback_replacement,
            english_surface=target,
        )
        values.append({
            "front": _replace_target(sentence, target, html.escape(target)),
            "recall": _replace_target(
                sentence,
                target,
                f"<b>{html.escape(replacement)}</b>",
                raw=True,
            ) + _semantic_recall_distractors_html(context),
            "answer": html.escape(target),
            "source": html.escape(str(context["source"])),
        })
    payload = html.escape(json.dumps(values, ensure_ascii=False), quote=True)
    side = "front" if direction == "meaning" else "recall"
    key = html.escape(str(card["id"]), quote=True)
    front = _semantic_front_html(payload, key, side, values[0])
    if direction == "meaning":
        translations = "<br>".join(f"• {html.escape(str(item))}" for item in card["translations"])
        details = (
            f"<br>{translations}<br><small>"
            f"{html.escape(str(card['sense_definition_en']))}</small>"
        )
        return front, _semantic_back_html(payload, key, side, values[0], details)
    return front, _semantic_back_html(
        payload,
        key,
        side,
        values[0],
        "",
    )


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
    fallback_html = fallback[side]
    return (
        f'<div class="anki-papers-semantic" data-key="{key}" data-side="{side}" '
        f'data-contexts="{payload}">{fallback_html}</div>'
        '<script>(function(){var root=document.currentScript.previousElementSibling,items=JSON.parse(root.dataset.contexts),'
        'key="anki-papers-context:"+root.dataset.key+":"+root.dataset.side,stored=null;'
        'try{stored=JSON.parse(sessionStorage.getItem(key)||"null")}catch(e){}var now=Date.now(),'
        'valid=stored&&stored.until>now&&Number.isInteger(stored.index)&&stored.index>=0&&'
        'stored.index<items.length,index=valid?stored.index:Math.floor(Math.random()*items.length);'
        'try{sessionStorage.setItem(key,JSON.stringify({index:index,until:now+120000}))}catch(e){}'
        'var item=items[index];root.innerHTML=item[root.dataset.side];})();</script>'
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


def _replace_target(sentence: str, target: str, replacement: str, raw: bool = False) -> str:
    match = re.search(re.escape(target), sentence, flags=re.IGNORECASE)
    if not match:
        return html.escape(sentence)
    selected = replacement if raw else f"<b>{replacement}</b>"
    return html.escape(sentence[: match.start()]) + selected + html.escape(sentence[match.end() :])


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


def _plain(value: str) -> str:
    return _normalized(html.unescape(re.sub(r"<[^>]+>", " ", value)))


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())
