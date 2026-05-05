from __future__ import annotations

import json
import logging
from typing import Any

from app.bot.services.ticket_form_service import ANSWER_TYPE_MEDIA, TicketForm, TicketQuestion
from app.bot.services.ticket_service import TicketService


logger = logging.getLogger(__name__)

VK_FORM_BUTTON_LIMIT = 10


def build_form_keyboard(forms: list[TicketForm]) -> str:
    visible_forms = forms[:VK_FORM_BUTTON_LIMIT]
    if len(forms) > VK_FORM_BUTTON_LIMIT:
        logger.warning("VK keyboard MVP shows only first %s forms", VK_FORM_BUTTON_LIMIT)
    return _keyboard(
        [
            [_button(form.button_text, "primary", {"action": "select_form", "form_id": form.id})]
            for form in visible_forms
        ]
    )


def build_question_keyboard(question: TicketQuestion) -> str:
    row = []
    if not question.required:
        row.append(_button("Пропустить", "secondary", {"action": "skip"}))
    row.append(_button("Отмена", "negative", {"action": "cancel"}))
    return _keyboard([row])


def build_media_continue_keyboard() -> str:
    return _keyboard(
        [
            [
                _button("Продолжить", "positive", {"action": "continue"}),
                _button("Отмена", "negative", {"action": "cancel"}),
            ]
        ]
    )


def build_minecraft_nickname_keyboard() -> str:
    return _keyboard(
        [
            [
                _button("Да", "positive", {"action": "profile_yes"}),
                _button("Ввести другой", "secondary", {"action": "profile_other"}),
            ]
        ]
    )


def build_preview_keyboard() -> str:
    return _keyboard(
        [
            [_button("Отправить", "positive", {"action": "submit"})],
            [_button("Заполнить заново", "secondary", {"action": "restart"})],
            [_button("Отмена", "negative", {"action": "cancel"})],
        ]
    )


def build_closed_ticket_keyboard(forms: list[TicketForm]) -> str:
    return build_form_keyboard(forms)


def build_form_menu_text(prefix_text: str | None = None) -> str:
    text = (
        "Здравствуйте! Здесь вы можете обратиться в поддержку.\n\n"
        "Выберите тип обращения ниже."
    )
    if prefix_text:
        return f"{prefix_text.strip()}\n\n{text}"
    return text


def build_question_text(
    form: TicketForm,
    question_index: int,
    *,
    prefix_text: str | None = None,
) -> str:
    question = form.questions[question_index]
    lines = [
        form.title,
        "",
        f"Вопрос {question_index + 1} из {len(form.questions)}",
        "",
        question.text,
    ]
    if question.help_text:
        lines.extend(["", "Подсказка:", question.help_text])
    if question.answer_type == ANSWER_TYPE_MEDIA:
        lines.extend(["", "Прикрепите файл, фото или видео следующим сообщением."])
    if prefix_text:
        lines = [prefix_text.strip(), ""] + lines
    return "\n".join(lines)


def build_media_continue_text(
    media_count: int,
    max_files: int | None,
    *,
    limit_reached: bool = False,
) -> str:
    if max_files is None:
        return (
            f"Файлы добавлены: {media_count}.\n"
            "Можно отправить ещё файл или нажать «Продолжить»."
        )
    if limit_reached:
        return (
            f"Файлы добавлены: {media_count} из {max_files}.\n"
            "Достигнут лимит файлов.\n\n"
            "Нажмите «Продолжить», чтобы перейти дальше."
        )
    return (
        f"Файлы добавлены: {media_count} из {max_files}.\n"
        "Можно отправить ещё файл или нажать «Продолжить»."
    )


def build_minecraft_nickname_text(nickname: str) -> str:
    return f"Использовать прошлый ник {nickname}?"


def build_preview_text(form: TicketForm, answers: list[dict[str, Any]]) -> str:
    lines = [
        "Проверьте данные тикета:",
        "",
        f"Тип обращения: {form.title}",
        "",
    ]
    answers_by_question = {answer.get("question_id"): answer for answer in answers}
    for question_number, question in enumerate(form.questions, start=1):
        answer = answers_by_question.get(question.id)
        lines.append(f"{question_number}. {question.text.strip().rstrip(':')}")
        lines.append(_format_preview_answer(answer))
        lines.append("")
    lines.append("Отправить тикет?")
    return "\n".join(lines)


def build_ticket_sent_text(ticket_id: int | None = None, success_text: str | None = None) -> str:
    text = success_text or "✅ Тикет отправлен в поддержку.\nОтвет придёт сюда."
    if ticket_id is not None and success_text is None:
        text = f"{text}\n\nТикет: #{ticket_id}"
    return text


def _format_preview_answer(answer: dict[str, Any] | None) -> str:
    if answer is None or answer.get("skipped"):
        return "— Пропущено"
    if answer.get("answer_type") == "media":
        media_count = len(TicketService.extract_media_files(answer))
        return f"📎 Медиафайлов: {media_count}" if media_count else "— Пропущено"
    return str(answer.get("answer_text") or "не указано")


def _keyboard(rows: list[list[dict[str, Any]]]) -> str:
    return json.dumps(
        {
            "one_time": False,
            "inline": False,
            "buttons": rows,
        },
        ensure_ascii=False,
    )


def _button(label: str, color: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": {
            "type": "text",
            "label": label,
            "payload": json.dumps(payload, ensure_ascii=False),
        },
        "color": color,
    }
