from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from app.bot.constants.actions import (
    ACTION_TICKET_CLOSED,
    TG_CALLBACK_CANCEL_TICKET,
    TG_CALLBACK_CLOSE_PREFIX,
    TG_CALLBACK_CLOSE_REASON_PREFIX,
    TG_CALLBACK_CONTINUE_MEDIA,
    TG_CALLBACK_FORM_PREFIX,
    TG_CALLBACK_MINECRAFT_LOOKUP_CONTINUE,
    TG_CALLBACK_MINECRAFT_LOOKUP_OTHER,
    TG_CALLBACK_OPEN_TICKET,
    TG_CALLBACK_PROFILE_NICKNAME_CHANGE_CANCEL,
    TG_CALLBACK_PROFILE_NICKNAME_CHANGE_CONFIRM,
    TG_CALLBACK_PROFILE_NICKNAME_OTHER,
    TG_CALLBACK_PROFILE_NICKNAME_YES,
    TG_CALLBACK_RESTART_TICKET,
    TG_CALLBACK_SKIP_QUESTION,
    TG_CALLBACK_SUBMIT_TICKET,
)
from app.bot.services.ticket_form_service import TicketCloseReason, TicketForm
from app.bot.services.ticket_service import CLOSE_REASONS, MANUAL_CLOSE_REASON_CODES, CloseReason


CALLBACK_OPEN_TICKET = TG_CALLBACK_OPEN_TICKET
CALLBACK_CANCEL_TICKET = TG_CALLBACK_CANCEL_TICKET
CALLBACK_SUBMIT_TICKET = TG_CALLBACK_SUBMIT_TICKET
CALLBACK_RESTART_TICKET = TG_CALLBACK_RESTART_TICKET
CALLBACK_SKIP_QUESTION = TG_CALLBACK_SKIP_QUESTION
CALLBACK_CONTINUE_MEDIA = TG_CALLBACK_CONTINUE_MEDIA
CALLBACK_PROFILE_NICKNAME_YES = TG_CALLBACK_PROFILE_NICKNAME_YES
CALLBACK_PROFILE_NICKNAME_OTHER = TG_CALLBACK_PROFILE_NICKNAME_OTHER
CALLBACK_PROFILE_NICKNAME_CHANGE_CONFIRM = TG_CALLBACK_PROFILE_NICKNAME_CHANGE_CONFIRM
CALLBACK_PROFILE_NICKNAME_CHANGE_CANCEL = TG_CALLBACK_PROFILE_NICKNAME_CHANGE_CANCEL
CALLBACK_MINECRAFT_LOOKUP_CONTINUE = TG_CALLBACK_MINECRAFT_LOOKUP_CONTINUE
CALLBACK_MINECRAFT_LOOKUP_OTHER = TG_CALLBACK_MINECRAFT_LOOKUP_OTHER
CALLBACK_FORM_PREFIX = TG_CALLBACK_FORM_PREFIX
CALLBACK_CLOSE_PREFIX = TG_CALLBACK_CLOSE_PREFIX
CALLBACK_CLOSE_REASON_PREFIX = TG_CALLBACK_CLOSE_REASON_PREFIX
SUPPORT_CLOSE_TICKET_TEXT = "✅ Закрыть тикет"


def open_ticket_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть тикет", callback_data=CALLBACK_OPEN_TICKET)],
        ]
    )


def ticket_forms_keyboard(forms: list[TicketForm]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=form.button_text, callback_data=f"{CALLBACK_FORM_PREFIX}{form.id}")]
        for form in forms
    ]
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=CALLBACK_CANCEL_TICKET)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ticket_forms_reply_keyboard(forms: list[TicketForm]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=form.button_text)] for form in forms],
        resize_keyboard=True,
        input_field_placeholder="Выберите тип обращения",
    )


def question_keyboard(required: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if not required:
        rows.append([InlineKeyboardButton(text="Пропустить", callback_data=CALLBACK_SKIP_QUESTION)])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=CALLBACK_CANCEL_TICKET)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def multiple_media_keyboard(question_index: int | None = None) -> InlineKeyboardMarkup:
    continue_callback = CALLBACK_CONTINUE_MEDIA
    if question_index is not None:
        continue_callback = f"{CALLBACK_CONTINUE_MEDIA}:{question_index}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Продолжить", callback_data=continue_callback)],
            [InlineKeyboardButton(text="Отмена", callback_data=CALLBACK_CANCEL_TICKET)],
        ]
    )


def ticket_summary_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отправить", callback_data=CALLBACK_SUBMIT_TICKET)],
            [InlineKeyboardButton(text="Заполнить заново", callback_data=CALLBACK_RESTART_TICKET)],
            [InlineKeyboardButton(text="Отмена", callback_data=CALLBACK_CANCEL_TICKET)],
        ]
    )


def ticket_preview_error_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Заполнить заново", callback_data=CALLBACK_RESTART_TICKET)],
            [InlineKeyboardButton(text="Отмена", callback_data=CALLBACK_CANCEL_TICKET)],
        ]
    )


def minecraft_nickname_keyboard(*, change_label: str = "Ввести другой") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да", callback_data=CALLBACK_PROFILE_NICKNAME_YES)],
            [InlineKeyboardButton(text=change_label, callback_data=CALLBACK_PROFILE_NICKNAME_OTHER)],
        ]
    )


def minecraft_nickname_change_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Да, изменить", callback_data=CALLBACK_PROFILE_NICKNAME_CHANGE_CONFIRM)],
            [InlineKeyboardButton(text="Отмена", callback_data=CALLBACK_PROFILE_NICKNAME_CHANGE_CANCEL)],
        ]
    )


def minecraft_lookup_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Продолжить", callback_data=CALLBACK_MINECRAFT_LOOKUP_CONTINUE)],
            [InlineKeyboardButton(text="Ввести другой", callback_data=CALLBACK_MINECRAFT_LOOKUP_OTHER)],
        ]
    )


def close_ticket_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Закрыть тикет", callback_data=f"{CALLBACK_CLOSE_PREFIX}{ticket_id}")],
        ]
    )


def support_close_ticket_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=SUPPORT_CLOSE_TICKET_TEXT)]],
        resize_keyboard=True,
        input_field_placeholder="Управление тикетом",
    )


def support_close_reason_keyboard(reasons: list[TicketCloseReason | CloseReason] | None = None) -> ReplyKeyboardMarkup:
    close_reasons = reasons or [CLOSE_REASONS[reason_code] for reason_code in MANUAL_CLOSE_REASON_CODES]
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=reason.button_text)]
            for reason in close_reasons
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder="Выберите причину закрытия",
    )


def close_reason_keyboard(ticket_id: int, reasons: list[TicketCloseReason | CloseReason] | None = None) -> InlineKeyboardMarkup:
    close_reasons = reasons or [CLOSE_REASONS[reason_code] for reason_code in MANUAL_CLOSE_REASON_CODES]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=reason.button_text,
                    callback_data=f"{CALLBACK_CLOSE_REASON_PREFIX}{ticket_id}:{reason.id}",
                )
            ]
            for reason in close_reasons
        ]
    )


def closed_ticket_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Закрыто ✅", callback_data=ACTION_TICKET_CLOSED)],
        ]
    )
