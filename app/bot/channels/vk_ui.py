from __future__ import annotations

import json
import logging
from typing import Any

from app.bot.constants.actions import (
    ACTION_CANCEL,
    ACTION_MEDIA_CONTINUE,
    ACTION_MINECRAFT_LOOKUP_CONTINUE,
    ACTION_MINECRAFT_LOOKUP_OTHER,
    ACTION_PROFILE_NICKNAME_CHANGE_CANCEL,
    ACTION_PROFILE_NICKNAME_CHANGE_CONFIRM,
    ACTION_PROFILE_NICKNAME_OTHER,
    ACTION_PROFILE_NICKNAME_YES,
    ACTION_RESTART,
    ACTION_SELECT_FORM,
    ACTION_SKIP,
    ACTION_SUBMIT,
)
from app.bot.services.ticket_form_service import (
    ANSWER_TYPE_MEDIA,
    PROFILE_FIELD_MINECRAFT_NICKNAME,
    PROFILE_FIELD_MINECRAFT_TARGET_NICKNAME,
    TicketForm,
    TicketQuestion,
    get_minecraft_profile_field,
)
from app.bot.services.ticket_service import TicketService


logger = logging.getLogger(__name__)

VK_FORM_BUTTON_LIMIT = 10


def build_form_keyboard(forms: list[TicketForm]) -> str:
    visible_forms = forms[:VK_FORM_BUTTON_LIMIT]
    if len(forms) > VK_FORM_BUTTON_LIMIT:
        logger.warning("VK keyboard MVP shows only first %s forms", VK_FORM_BUTTON_LIMIT)
    return _keyboard(
        [
            [_button(form.button_text, "primary", {"action": ACTION_SELECT_FORM, "form_id": form.id})]
            for form in visible_forms
        ]
    )


def build_question_keyboard(question: TicketQuestion) -> str:
    row = []
    if not question.required:
        row.append(_button("Пропустить", "secondary", {"action": ACTION_SKIP}))
    row.append(_button("Отмена", "negative", {"action": ACTION_CANCEL}))
    return _keyboard([row])


def build_media_continue_keyboard() -> str:
    return _keyboard(
        [
            [
                _button("Продолжить", "positive", {"action": ACTION_MEDIA_CONTINUE}),
                _button("Отмена", "negative", {"action": ACTION_CANCEL}),
            ]
        ]
    )


def build_minecraft_nickname_keyboard(*, change_label: str = "Ввести другой") -> str:
    return _keyboard(
        [
            [
                _button("Да", "positive", {"action": ACTION_PROFILE_NICKNAME_YES}),
                _button(change_label, "secondary", {"action": ACTION_PROFILE_NICKNAME_OTHER}),
            ]
        ]
    )


def build_minecraft_nickname_change_confirm_keyboard() -> str:
    return _keyboard(
        [
            [
                _button("Да, изменить", "negative", {"action": ACTION_PROFILE_NICKNAME_CHANGE_CONFIRM}),
                _button("Отмена", "secondary", {"action": ACTION_PROFILE_NICKNAME_CHANGE_CANCEL}),
            ]
        ]
    )


def build_minecraft_lookup_confirmation_keyboard() -> str:
    return _keyboard(
        [
            [
                _button("Продолжить", "positive", {"action": ACTION_MINECRAFT_LOOKUP_CONTINUE}),
                _button("Ввести другой", "secondary", {"action": ACTION_MINECRAFT_LOOKUP_OTHER}),
            ]
        ]
    )


def build_preview_keyboard() -> str:
    return _keyboard(
        [
            [_button("Отправить", "positive", {"action": ACTION_SUBMIT})],
            [_button("Заполнить заново", "secondary", {"action": ACTION_RESTART})],
            [_button("Отмена", "negative", {"action": ACTION_CANCEL})],
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


def build_minecraft_nickname_change_confirmation_text(nickname: str) -> str:
    return (
        f"У вас уже закреплён игровой ник: {nickname}.\n"
        "Изменить ник можно только через подтверждение.\n\n"
        "Продолжить смену ника?"
    )


def build_minecraft_lookup_not_found_text(nickname: str) -> str:
    return (
        f"⚠️ Игрок с ником {nickname} не найден на сервере.\n"
        "Вы можете продолжить, если уверены, что ник указан правильно."
    )


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
        lines.append(f"{question_number}. {_preview_question_label(question)}")
        lines.append(_format_preview_answer(answer))
        lines.append("")
    lines.append("Отправить тикет?")
    return _limit_text("\n".join(lines), 4000)


def _preview_question_label(question: TicketQuestion) -> str:
    profile_field = get_minecraft_profile_field(question)
    if profile_field == PROFILE_FIELD_MINECRAFT_NICKNAME:
        return "Игровой ник"
    if profile_field == PROFILE_FIELD_MINECRAFT_TARGET_NICKNAME:
        return "Ник нарушителя"
    return question.text.strip().rstrip(":")


def build_ticket_sent_text(ticket_id: int | None = None, success_text: str | None = None) -> str:
    text = success_text or "✅ Тикет отправлен в поддержку.\nОтвет придёт сюда."
    if ticket_id is not None and success_text is None:
        text = f"{text}\n\nТикет: #{ticket_id}"
    return _limit_text(text, 4000)


def _format_preview_answer(answer: dict[str, Any] | None) -> str:
    if answer is None or answer.get("skipped"):
        return "— Пропущено"
    if answer.get("answer_type") == "media":
        media_count = len(TicketService.extract_media_files(answer))
        return f"📎 Медиафайлов: {media_count}" if media_count else "— Пропущено"
    return str(answer.get("answer_text") or "не указано")


def _limit_text(text: str | None, limit: int) -> str:
    normalized = str(text or "")
    if len(normalized) <= limit:
        return normalized
    suffix = "\n\n…"
    if limit <= len(suffix):
        return normalized[:limit]
    return normalized[: limit - len(suffix)].rstrip() + suffix


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
