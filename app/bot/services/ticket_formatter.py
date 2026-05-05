from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from html import escape
from typing import Any

from app.bot.database.models import Platform, Ticket, TicketAnswer, TicketAnswerMedia, TicketStatus, User
from app.bot.services.ticket_form_service import (
    PROFILE_FIELD_MINECRAFT_NICKNAME,
    PROFILE_FIELD_MINECRAFT_TARGET_NICKNAME,
    TicketForm,
    TicketQuestion,
    get_minecraft_profile_field,
)


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
        minecraft_lookup_block = cls._format_mapping_minecraft_lookup_block(form.questions, answers)
        return cls._build_card_parts(
            ticket_id=ticket.id,
            form_title=form.admin_title,
            user=user,
            status=ticket.status,
            created_at=ticket.created_at,
            extra_blocks=[minecraft_lookup_block] if minecraft_lookup_block else (),
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
        minecraft_lookup_block = cls._format_stored_minecraft_lookup_block(ticket.answers)
        return cls._build_card_parts(
            ticket_id=ticket.id,
            form_title=ticket.form_title,
            user=user,
            status=ticket.status,
            created_at=ticket.created_at,
            extra_blocks=[minecraft_lookup_block] if minecraft_lookup_block else (),
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
        extra_blocks: Sequence[str] = (),
        close_reason_label: str | None = None,
        closed_at: datetime | None = None,
    ) -> list[str]:
        header = cls._build_header(ticket_id, form_title, user)
        footer = cls._build_footer(status, created_at, close_reason_label, closed_at)
        formatted_answer_blocks = list(answer_blocks) or ["— Нет данных"]
        full_text = cls._join_sections([header, *extra_blocks, "Данные формы:", *formatted_answer_blocks, footer])
        if len(full_text) <= CARD_MESSAGE_LIMIT:
            return [full_text]

        summary_header = "\n".join(
            [
                header,
                *extra_blocks,
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
        platform_name = cls._platform_name(user.platform)
        username = cls._format_username(user)
        platform_user_id = user.platform_user_id or (str(user.telegram_id) if user.telegram_id else "не указан")
        full_name = user.full_name or "не указано"
        return "\n".join(
            [
                f"🟣 Новый тикет #{ticket_id}",
                "",
                "Платформа:",
                platform_name,
                "",
                "Тип обращения:",
                cls._e(form_title),
                "",
                "Пользователь:",
                f"• Username: {cls._e(username)}",
                f"• Platform ID: {cls._e(platform_user_id)}",
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
        status_texts = {
            TicketStatus.OPEN: "🟢 Открыт",
            TicketStatus.IN_PROGRESS: "🟣 В работе",
            TicketStatus.WAITING_USER: "⏳ Ожидает пользователя",
            TicketStatus.CLOSED: "✅ Закрыт",
            TicketStatus.CANCELLED: "🚫 Отменён",
        }
        status_text = status_texts.get(status, str(status.value if hasattr(status, "value") else status))
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
    def _format_mapping_minecraft_lookup_block(
        cls,
        questions: Sequence[TicketQuestion],
        answers: Sequence[Mapping[str, Any]],
    ) -> str | None:
        answers_by_question = cls._group_mapping_answers(answers)
        items: list[dict[str, Any]] = []
        used_keys: set[tuple[str, str]] = set()
        for question in questions:
            profile_field = get_minecraft_profile_field(question)
            if profile_field is None:
                continue
            for answer in answers_by_question.get(question.id, []):
                if answer.get("skipped"):
                    continue
                nickname = str(answer.get("answer_text") or "").strip()
                if not nickname:
                    continue
                key = (profile_field, nickname.casefold())
                if key in used_keys:
                    continue
                used_keys.add(key)
                lookup = answer.get("minecraft_lookup")
                if not isinstance(lookup, Mapping):
                    lookup = {"nickname": nickname, "exists": None, "error": "disabled"}
                items.append(
                    {
                        "label": cls._minecraft_lookup_label(profile_field, question.text),
                        "nickname": str(lookup.get("nickname") or nickname),
                        "exists": lookup.get("exists"),
                        "uuid": lookup.get("uuid"),
                        "online": lookup.get("online"),
                        "source": lookup.get("source"),
                        "error": lookup.get("error"),
                    }
                )
        return cls._format_minecraft_lookup_items(items)

    @classmethod
    def _format_stored_minecraft_lookup_block(cls, answers: Sequence[TicketAnswer]) -> str | None:
        items: list[dict[str, Any]] = []
        used_keys: set[tuple[str, str]] = set()
        for answer in sorted(answers, key=lambda item: item.id):
            profile_field = answer.profile_field
            if profile_field not in {PROFILE_FIELD_MINECRAFT_NICKNAME, PROFILE_FIELD_MINECRAFT_TARGET_NICKNAME}:
                continue
            if answer.skipped:
                continue
            nickname = answer.minecraft_lookup_nickname or answer.answer_text
            nickname = str(nickname or "").strip()
            if not nickname:
                continue
            key = (profile_field, nickname.casefold())
            if key in used_keys:
                continue
            used_keys.add(key)
            items.append(
                {
                    "label": cls._minecraft_lookup_label(profile_field, answer.question_text),
                    "nickname": nickname,
                    "exists": answer.minecraft_lookup_exists,
                    "uuid": answer.minecraft_lookup_uuid,
                    "online": answer.minecraft_lookup_online,
                    "source": answer.minecraft_lookup_source,
                    "error": answer.minecraft_lookup_error or "disabled",
                }
            )
        return cls._format_minecraft_lookup_items(items)

    @classmethod
    def _format_minecraft_lookup_items(cls, items: Sequence[Mapping[str, Any]]) -> str | None:
        if not items:
            return None
        if len(items) == 1:
            item = items[0]
            return "\n".join(["🎮 Игрок", "", *cls._format_minecraft_lookup_lines(item, bullet=False)])

        lines = ["🎮 Проверка игроков"]
        for item in items:
            lines.extend(["", f"{cls._e(str(item.get('label') or 'Игрок'))}:"])
            lines.extend(cls._format_minecraft_lookup_lines(item, bullet=True))
        return "\n".join(lines)

    @classmethod
    def _format_minecraft_lookup_lines(cls, item: Mapping[str, Any], *, bullet: bool) -> list[str]:
        prefix = "• " if bullet else ""
        lines = [
            f"{prefix}Ник: {cls._e(str(item.get('nickname') or 'не указан'))}",
            f"{prefix}Проверка: {cls._minecraft_lookup_status(item)}",
        ]
        if item.get("uuid"):
            lines.append(f"{prefix}UUID: {cls._e(str(item.get('uuid')))}")
        if item.get("online") is not None:
            lines.append(f"{prefix}Онлайн: {'да' if item.get('online') else 'нет'}")
        if item.get("source"):
            lines.append(f"{prefix}Источник: {cls._e(str(item.get('source')))}")
        if item.get("exists") is None and item.get("error") and item.get("error") != "disabled":
            lines.append(f"{prefix}Ошибка: {cls._e(str(item.get('error')))}")
        return lines

    @staticmethod
    def _minecraft_lookup_status(item: Mapping[str, Any]) -> str:
        exists = item.get("exists")
        error = item.get("error")
        if exists is True:
            return "✅ найден"
        if exists is False:
            return "❌ не найден"
        if error == "disabled":
            return "отключена"
        return "⚠️ не удалось проверить"

    @staticmethod
    def _minecraft_lookup_label(profile_field: str | None, fallback_text: str) -> str:
        if profile_field == PROFILE_FIELD_MINECRAFT_NICKNAME:
            return "Ваш ник"
        if profile_field == PROFILE_FIELD_MINECRAFT_TARGET_NICKNAME:
            return "Ник нарушителя"
        return TicketFormatter._clean_label(fallback_text)

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
    def _platform_name(platform: str | None) -> str:
        names = {
            Platform.TELEGRAM.value: "Telegram",
            Platform.DISCORD.value: "Discord",
            Platform.VK.value: "VK",
        }
        return names.get(str(platform or Platform.TELEGRAM.value), str(platform or "Telegram"))

    @staticmethod
    def _format_username(user: User) -> str:
        if not user.username:
            return "нет username"
        username = str(user.username).strip()
        if user.platform == Platform.TELEGRAM.value:
            return f"@{username.strip('@')}"
        return username

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
