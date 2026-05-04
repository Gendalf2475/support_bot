from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from html import escape
from typing import Any

from app.bot.database.models import Ticket, TicketAnswer, TicketAnswerMedia, TicketStatus, User
from app.bot.services.ticket_form_service import TicketForm, TicketQuestion


TELEGRAM_MESSAGE_LIMIT = 4096
CARD_MESSAGE_LIMIT = 3700
EXTRA_MESSAGE_LIMIT = 3900


class TicketFormatter:
    @classmethod
    def build_new_ticket_parts(
        cls,
        ticket: Ticket,
        user: User,
        form: TicketForm,
        answers: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        answer_blocks = cls._format_form_answer_blocks(form.questions, answers)
        return cls._build_card_parts(
            ticket_id=ticket.id,
            form_title=form.admin_title,
            user=user,
            status=ticket.status,
            created_at=ticket.created_at,
            answer_blocks=answer_blocks,
        )

    @classmethod
    def build_ticket_parts(
        cls,
        ticket: Ticket,
        user: User,
        close_reason_label: str | None = None,
    ) -> list[str]:
        answer_blocks = cls._format_stored_answer_blocks(ticket.answers)
        return cls._build_card_parts(
            ticket_id=ticket.id,
            form_title=ticket.form_title,
            user=user,
            status=ticket.status,
            created_at=ticket.created_at,
            answer_blocks=answer_blocks,
            close_reason_label=close_reason_label,
            closed_at=ticket.closed_at,
        )

    @staticmethod
    def build_control_text(ticket_id: int) -> str:
        return f"Управление тикетом #{ticket_id}:"

    @staticmethod
    def build_closed_control_text(ticket_id: int) -> str:
        return f"Управление тикетом #{ticket_id}:\nСтатус: закрыт ✅"

    @staticmethod
    def build_close_reason_prompt_text(ticket_id: int) -> str:
        return f"Управление тикетом #{ticket_id}:\nВыберите причину закрытия:"

    @classmethod
    def build_media_caption(
        cls,
        question_number: int,
        question_text: str,
        media_index: int,
        media_total: int,
        user_caption: str | None,
        media_type: str | None,
    ) -> str | None:
        if media_type == "sticker":
            return None

        title = (
            f"📎 Вопрос {question_number}: {cls._clean_label(question_text)}\n"
            f"Файл {media_index} из {media_total}"
        )
        caption = (user_caption or "").strip()
        if not caption:
            return cls._limit_caption(title)

        combined = f"{title}\n\n{caption}"
        if len(combined) <= 1024:
            return combined
        return cls._limit_caption(caption)

    @classmethod
    def build_media_group_caption(
        cls,
        question_number: int,
        question_text: str,
        media_total: int,
        user_caption: str | None,
    ) -> str:
        title = (
            f"📎 Вопрос {question_number}: {cls._clean_label(question_text)}\n"
            f"Файлов: {media_total}"
        )
        caption = (user_caption or "").strip()
        if not caption:
            return cls._limit_caption(title)

        combined = f"{title}\n\nПодпись пользователя:\n{caption}"
        if len(combined) <= 1024:
            return combined
        return cls._limit_caption(caption)

    @classmethod
    def build_media_captions_text(cls, media_items: Sequence[Mapping[str, Any]]) -> str | None:
        captions = [
            (index, str(media.get("caption") or "").strip())
            for index, media in enumerate(media_items, start=1)
            if str(media.get("caption") or "").strip()
        ]
        if not captions:
            return None
        if len(captions) == 1 and captions[0][0] == 1:
            return None

        lines = ["Подписи к файлам:"]
        lines.extend(f"{index}. {caption}" for index, caption in captions)
        text = "\n".join(lines)
        if len(text) <= TELEGRAM_MESSAGE_LIMIT:
            return text
        return text[: TELEGRAM_MESSAGE_LIMIT - 3] + "..."

    @classmethod
    def iter_media_answers(
        cls,
        form: TicketForm,
        answers: Sequence[Mapping[str, Any]],
    ) -> Iterable[tuple[int, TicketQuestion, int, int, Mapping[str, Any]]]:
        answers_by_question = cls._group_mapping_answers(answers)
        for question_number, question in enumerate(form.questions, start=1):
            media_items: list[Mapping[str, Any]] = []
            for answer in answers_by_question.get(question.id, []):
                media_items.extend(cls._get_media_items(answer))
            media_total = len(media_items)
            for media_index, media in enumerate(media_items, start=1):
                yield question_number, question, media_index, media_total, media

    @classmethod
    def iter_question_media_groups(
        cls,
        form: TicketForm,
        answers: Sequence[Mapping[str, Any]],
    ) -> Iterable[tuple[int, TicketQuestion, list[Mapping[str, Any]]]]:
        answers_by_question = cls._group_mapping_answers(answers)
        for question_number, question in enumerate(form.questions, start=1):
            media_items: list[Mapping[str, Any]] = []
            for answer in answers_by_question.get(question.id, []):
                media_items.extend(cls._get_media_items(answer))
            if media_items:
                yield question_number, question, media_items

    @classmethod
    def _build_card_parts(
        cls,
        ticket_id: int,
        form_title: str,
        user: User,
        status: TicketStatus,
        created_at: datetime,
        answer_blocks: Sequence[str],
        close_reason_label: str | None = None,
        closed_at: datetime | None = None,
    ) -> list[str]:
        header = cls._build_header(ticket_id, form_title, user)
        footer = cls._build_footer(status, created_at, close_reason_label, closed_at)
        formatted_answer_blocks = list(answer_blocks) or ["— Нет данных"]
        full_text = cls._join_sections([header, "Данные формы:", *formatted_answer_blocks, footer])
        if len(full_text) <= CARD_MESSAGE_LIMIT:
            return [full_text]

        summary_header = "\n".join(
            [
                header,
                "Данные формы:",
                "Часть данных не поместилась в карточку и отправлена ниже.",
            ]
        )
        first_part = cls._join_sections([summary_header, footer])
        if len(first_part) > TELEGRAM_MESSAGE_LIMIT:
            first_part = cls._safe_html_limit(first_part, TELEGRAM_MESSAGE_LIMIT)

        return [first_part, *cls._build_extra_parts(formatted_answer_blocks)]

    @classmethod
    def _build_header(cls, ticket_id: int, form_title: str, user: User) -> str:
        username = f"@{user.username}" if user.username else "нет username"
        full_name = user.full_name or "не указано"
        return "\n".join(
            [
                f"🟣 Новый тикет #{ticket_id}",
                "",
                "Тип обращения:",
                cls._e(form_title),
                "",
                "Пользователь:",
                f"• Telegram: {cls._e(username)}",
                f"• ID: {user.telegram_id}",
                f"• Имя: {cls._e(full_name)}",
            ]
        )

    @classmethod
    def _build_footer(
        cls,
        status: TicketStatus,
        created_at: datetime,
        close_reason_label: str | None,
        closed_at: datetime | None,
    ) -> str:
        status_text = "✅ Закрыт" if status == TicketStatus.CLOSED else "🟢 Открыт"
        lines = [
            "Статус:",
            status_text,
        ]
        if close_reason_label:
            lines.extend(["", "Причина закрытия:", cls._e(close_reason_label)])
        if closed_at is not None:
            lines.extend(["", "Закрыт:", cls._format_dt(closed_at)])
        lines.extend(["", "Создан:", cls._format_dt(created_at)])
        return "\n".join(lines)

    @classmethod
    def _format_form_answer_blocks(
        cls,
        questions: Sequence[TicketQuestion],
        answers: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        answers_by_question = cls._group_mapping_answers(answers)
        blocks: list[str] = []
        for number, question in enumerate(questions, start=1):
            question_answers = answers_by_question.get(question.id, [])
            blocks.append(cls._format_answer_block(number, question.text, question_answers))
        return blocks

    @classmethod
    def _format_stored_answer_blocks(cls, answers: Sequence[TicketAnswer]) -> list[str]:
        grouped: dict[str, list[TicketAnswer]] = {}
        ordered_questions: list[tuple[str, str]] = []
        for answer in sorted(answers, key=lambda item: item.id):
            if answer.question_id not in grouped:
                grouped[answer.question_id] = []
                ordered_questions.append((answer.question_id, answer.question_text))
            grouped[answer.question_id].append(answer)

        blocks: list[str] = []
        for number, (question_id, question_text) in enumerate(ordered_questions, start=1):
            blocks.append(cls._format_answer_block(number, question_text, grouped[question_id]))
        return blocks

    @classmethod
    def _format_answer_block(
        cls,
        number: int,
        question_text: str,
        answers: Sequence[TicketAnswer | Mapping[str, Any]],
    ) -> str:
        lines = [
            f"{number}. {cls._e(cls._clean_label(question_text))}",
        ]
        if not answers:
            lines.append("— Пропущено")
            return "\n".join(lines)

        value_lines: list[str] = []
        for answer in answers:
            value_lines.extend(cls._format_answer_value_lines(answer))
        if not value_lines:
            value_lines.append("— Пропущено")
        lines.extend(value_lines)
        return "\n".join(lines)

    @classmethod
    def _format_answer_value_lines(cls, answer: TicketAnswer | Mapping[str, Any]) -> list[str]:
        if cls._get_answer_value(answer, "skipped"):
            return ["— Пропущено"]

        media_count = cls._get_media_count(answer)
        if media_count:
            return [f"📎 Медиафайлов: {media_count}"]

        answer_text = str(cls._get_answer_value(answer, "answer_text") or "").strip()
        if not answer_text:
            return ["— Пропущено"]
        return [cls._e(answer_text)]

    @classmethod
    def _build_extra_parts(cls, answer_blocks: Sequence[str]) -> list[str]:
        parts: list[str] = []
        current = "Данные формы, продолжение:"
        for block in answer_blocks:
            candidate = cls._join_sections([current, block])
            if len(candidate) <= EXTRA_MESSAGE_LIMIT:
                current = candidate
                continue

            if current != "Данные формы, продолжение:":
                parts.append(current)
                current = "Данные формы, продолжение:"

            if len(cls._join_sections([current, block])) <= EXTRA_MESSAGE_LIMIT:
                current = cls._join_sections([current, block])
                continue

            chunks = cls._split_large_block(block, EXTRA_MESSAGE_LIMIT - len(current) - 2)
            for chunk in chunks:
                parts.append(cls._join_sections([current, chunk]))

        if current != "Данные формы, продолжение:":
            parts.append(current)
        return parts

    @classmethod
    def _split_large_block(cls, block: str, max_chunk_size: int) -> list[str]:
        if max_chunk_size <= 0:
            return [cls._safe_html_limit(block, EXTRA_MESSAGE_LIMIT)]
        chunks: list[str] = []
        rest = block
        while len(rest) > max_chunk_size:
            chunk = cls._safe_html_slice(rest, max_chunk_size)
            chunks.append(chunk)
            rest = rest[len(chunk) :]
        if rest:
            chunks.append(rest)
        return chunks

    @staticmethod
    def _group_mapping_answers(answers: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for answer in answers:
            question_id = answer.get("question_id")
            if question_id:
                grouped[str(question_id)].append(answer)
        return grouped

    @classmethod
    def _is_media_answer(cls, answer: TicketAnswer | Mapping[str, Any]) -> bool:
        return cls._get_media_count(answer) > 0

    @classmethod
    def _get_media_count(cls, answer: TicketAnswer | Mapping[str, Any]) -> int:
        if isinstance(answer, Mapping):
            media_files = answer.get("media_files")
            if isinstance(media_files, Sequence) and not isinstance(media_files, (str, bytes)):
                return len(media_files)
            return 1 if answer.get("file_id") else 0

        if answer.media_files:
            return len(answer.media_files)
        return 1 if answer.file_id else 0

    @staticmethod
    def _get_media_items(answer: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        media_files = answer.get("media_files")
        if isinstance(media_files, Sequence) and not isinstance(media_files, (str, bytes)):
            media_items = [media for media in media_files if isinstance(media, Mapping)]
            return [
                media
                for _, media in sorted(
                    enumerate(media_items),
                    key=lambda item: (TicketFormatter._media_sort_order(item[1]), item[0]),
                )
            ]
        if answer.get("file_id"):
            return [answer]
        return []

    @staticmethod
    def _media_sort_order(media: Mapping[str, Any]) -> int:
        for key in ("sort_order", "source_message_id"):
            try:
                value = media.get(key)
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                continue
        return 0

    @staticmethod
    def _get_answer_value(answer: TicketAnswer | TicketAnswerMedia | Mapping[str, Any], key: str) -> Any:
        if isinstance(answer, Mapping):
            return answer.get(key)
        return getattr(answer, key)

    @staticmethod
    def _clean_label(text: str) -> str:
        return str(text).strip().rstrip(":")

    @staticmethod
    def _format_dt(value: datetime) -> str:
        return value.astimezone().strftime("%d.%m.%Y %H:%M")

    @staticmethod
    def _join_sections(sections: Sequence[str]) -> str:
        return "\n\n".join(section for section in sections if section)

    @staticmethod
    def _e(value: str) -> str:
        return escape(str(value), quote=False)

    @staticmethod
    def _limit_caption(caption: str) -> str:
        if len(caption) <= 1024:
            return caption
        return caption[:1021] + "..."

    @classmethod
    def _safe_html_limit(cls, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        suffix = "\n\n...текст обрезан, потому что Telegram ограничивает длину сообщения."
        return cls._safe_html_slice(text, limit - len(suffix)) + suffix

    @staticmethod
    def _safe_html_slice(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        cut = max(1, limit)
        entity_start = text.rfind("&", max(0, cut - 8), cut)
        entity_end = text.rfind(";", max(0, cut - 8), cut)
        if entity_start > entity_end:
            cut = max(1, entity_start)
        return text[:cut]
