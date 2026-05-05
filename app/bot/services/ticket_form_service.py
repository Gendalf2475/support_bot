from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


logger = logging.getLogger(__name__)

ANSWER_TYPE_TEXT = "text"
ANSWER_TYPE_MEDIA = "media"
ANSWER_TYPE_ANY = "any"
ALLOWED_ANSWER_TYPES = {ANSWER_TYPE_TEXT, ANSWER_TYPE_MEDIA, ANSWER_TYPE_ANY}
PROFILE_FIELD_MINECRAFT_NICKNAME = "minecraft_nickname"
MINECRAFT_NICKNAME_QUESTION_IDS = {"nickname", "your_nickname", "minecraft_nickname", "player_nickname"}
DEFAULT_MINECRAFT_NICKNAME_REGEX = r"^[A-Za-z0-9_]{3,16}$"
DEFAULT_MINECRAFT_NICKNAME_VALIDATION_ERROR = "Введите корректный Minecraft-ник: 3–16 символов, латиница, цифры или _."


@dataclass(frozen=True)
class TicketQuestion:
    id: str
    text: str
    required: bool
    answer_type: str = ANSWER_TYPE_ANY
    help_text: str | None = None
    allow_multiple: bool = False
    max_files: int | None = 1
    profile_field: str | None = None
    validation_regex: str | None = None
    validation_error: str | None = None


@dataclass(frozen=True)
class TicketCloseReason:
    id: str
    title: str
    button_text: str
    user_message: str | None = None
    show_to_user: bool = True


@dataclass(frozen=True)
class TicketForm:
    id: str
    title: str
    description: str
    button_text: str
    admin_title: str
    success_text: str | None
    close_reasons: tuple[TicketCloseReason, ...]
    questions: tuple[TicketQuestion, ...]


class TicketFormService:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.enabled = True
        self.forms: list[TicketForm] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            logger.warning("Ticket forms file not found: %s. Fallback form will be used.", self.path)
            self.enabled = True
            self.forms = [self.fallback_form()]
            return

        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = yaml.safe_load(file) or {}
        except yaml.YAMLError as error:
            logger.error("Failed to parse ticket forms file %s: %s. Fallback form will be used.", self.path, error)
            self.enabled = True
            self.forms = [self.fallback_form()]
            return
        except OSError as error:
            logger.error("Failed to read ticket forms file %s: %s. Fallback form will be used.", self.path, error)
            self.enabled = True
            self.forms = [self.fallback_form()]
            return

        if not isinstance(data, dict):
            logger.error("Ticket forms file %s must contain a YAML object. Fallback form will be used.", self.path)
            self.enabled = True
            self.forms = [self.fallback_form()]
            return

        self.enabled = bool(data.get("enabled", True))
        parsed_forms = self._parse_forms(data.get("forms", []))
        if not parsed_forms:
            logger.error("Ticket forms file %s does not contain valid forms. Fallback form will be used.", self.path)
            parsed_forms = [self.fallback_form()]

        self.forms = parsed_forms
        logger.info("Loaded ticket forms: enabled=%s forms=%s", self.enabled, len(self.forms))

    def get_forms(self) -> list[TicketForm]:
        return list(self.forms)

    def get_form(self, form_id: str) -> TicketForm | None:
        return next((form for form in self.forms if form.id == form_id), None)

    def _parse_forms(self, raw_forms: Any) -> list[TicketForm]:
        if not isinstance(raw_forms, list):
            logger.error("Ticket forms 'forms' must be a list. All forms are skipped.")
            return []

        forms: list[TicketForm] = []
        used_ids: set[str] = set()
        for index, raw_form in enumerate(raw_forms, start=1):
            form = self._parse_form(raw_form, index, used_ids)
            if form is not None:
                forms.append(form)
                used_ids.add(form.id)
        return forms

    def _parse_form(self, raw_form: Any, index: int, used_ids: set[str]) -> TicketForm | None:
        if not isinstance(raw_form, dict):
            logger.error("Ticket form #%s must be an object. Form is skipped.", index)
            return None
        if raw_form.get("enabled", True) is False:
            return None

        form_id = str(raw_form.get("id") or "").strip()
        title = str(raw_form.get("title") or "").strip()
        description = str(raw_form.get("description") or "").strip()
        button_text = str(raw_form.get("button_text") or title).strip()
        admin_title = str(raw_form.get("admin_title") or title).strip()
        success_text = str(raw_form.get("success_text") or "").strip() or None
        close_reasons = self._parse_close_reasons(raw_form.get("close_reasons", []), form_id or f"#{index}")
        questions = self._parse_questions(raw_form.get("questions", []), form_id or f"#{index}")

        if not form_id:
            logger.error("Ticket form #%s has empty id. Form is skipped.", index)
            return None
        if form_id in used_ids:
            logger.error("Ticket form '%s' has duplicate id. Form is skipped.", form_id)
            return None
        if len(f"ticket_form:{form_id}") > 64:
            logger.error("Ticket form '%s' id is too long for Telegram callback data. Form is skipped.", form_id)
            return None
        if not title:
            logger.error("Ticket form '%s' has empty title. Form is skipped.", form_id)
            return None
        if not button_text:
            logger.error("Ticket form '%s' has empty button_text. Form is skipped.", form_id)
            return None
        if not admin_title:
            logger.error("Ticket form '%s' has empty admin_title. Form is skipped.", form_id)
            return None
        if not questions:
            logger.error("Ticket form '%s' has no valid questions. Form is skipped.", form_id)
            return None

        return TicketForm(
            id=form_id,
            title=title,
            description=description,
            button_text=button_text,
            admin_title=admin_title,
            success_text=success_text,
            close_reasons=tuple(close_reasons),
            questions=tuple(questions),
        )

    def _parse_close_reasons(self, raw_reasons: Any, form_label: str) -> list[TicketCloseReason]:
        if raw_reasons in (None, ""):
            return []
        if not isinstance(raw_reasons, list):
            logger.error("Ticket form '%s' close_reasons must be a list. Reasons are skipped.", form_label)
            return []

        reasons: list[TicketCloseReason] = []
        used_ids: set[str] = set()
        for index, raw_reason in enumerate(raw_reasons, start=1):
            if not isinstance(raw_reason, dict):
                logger.error("Close reason #%s in form '%s' must be an object. Reason is skipped.", index, form_label)
                continue

            reason_id = str(raw_reason.get("id") or "").strip()
            title = str(raw_reason.get("title") or "").strip()
            button_text = str(raw_reason.get("button_text") or title).strip()
            user_message = str(raw_reason.get("user_message") or "").strip() or None
            show_to_user = bool(raw_reason.get("show_to_user", True))

            if not reason_id:
                logger.error("Close reason #%s in form '%s' has empty id. Reason is skipped.", index, form_label)
                continue
            if reason_id in used_ids:
                logger.error("Close reason '%s' in form '%s' has duplicate id. Reason is skipped.", reason_id, form_label)
                continue
            if len(f"ticket_close_reason:0:{reason_id}") > 64:
                logger.error("Close reason '%s' in form '%s' id is too long for Telegram callback data. Reason is skipped.", reason_id, form_label)
                continue
            if not title:
                logger.error("Close reason '%s' in form '%s' has empty title. Reason is skipped.", reason_id, form_label)
                continue
            if not button_text:
                logger.error("Close reason '%s' in form '%s' has empty button_text. Reason is skipped.", reason_id, form_label)
                continue

            reasons.append(
                TicketCloseReason(
                    id=reason_id,
                    title=title,
                    button_text=button_text,
                    user_message=user_message,
                    show_to_user=show_to_user,
                )
            )
            used_ids.add(reason_id)
        return reasons

    def _parse_questions(self, raw_questions: Any, form_label: str) -> list[TicketQuestion]:
        if not isinstance(raw_questions, list):
            logger.error("Ticket form '%s' questions must be a list. Questions are skipped.", form_label)
            return []

        questions: list[TicketQuestion] = []
        used_ids: set[str] = set()
        for index, raw_question in enumerate(raw_questions, start=1):
            if not isinstance(raw_question, dict):
                logger.error("Question #%s in form '%s' must be an object. Question is skipped.", index, form_label)
                continue

            question_id = str(raw_question.get("id") or "").strip()
            text = str(raw_question.get("text") or "").strip()
            help_text = str(raw_question.get("help_text") or "").strip() or None
            required = bool(raw_question.get("required", False))
            answer_type = str(raw_question.get("answer_type") or ANSWER_TYPE_ANY).strip().lower()
            allow_multiple = bool(raw_question.get("allow_multiple", False))
            max_files = self._parse_max_files(raw_question.get("max_files"), allow_multiple)
            profile_field = str(raw_question.get("profile_field") or "").strip() or None
            validation_regex = str(raw_question.get("validation_regex") or "").strip() or None
            validation_error = str(raw_question.get("validation_error") or "").strip() or None

            if not question_id:
                logger.error("Question #%s in form '%s' has empty id. Question is skipped.", index, form_label)
                continue
            if question_id in used_ids:
                logger.error("Question '%s' in form '%s' has duplicate id. Question is skipped.", question_id, form_label)
                continue
            if not text:
                logger.error("Question '%s' in form '%s' has empty text. Question is skipped.", question_id, form_label)
                continue
            if answer_type not in ALLOWED_ANSWER_TYPES:
                logger.error(
                    "Question '%s' in form '%s' has invalid answer_type '%s'. Question is skipped.",
                    question_id,
                    form_label,
                    answer_type,
                )
                continue

            questions.append(
                TicketQuestion(
                    id=question_id,
                    text=text,
                    required=required,
                    answer_type=answer_type,
                    help_text=help_text,
                    allow_multiple=allow_multiple,
                    max_files=max_files,
                    profile_field=profile_field,
                    validation_regex=validation_regex,
                    validation_error=validation_error,
                )
            )
            used_ids.add(question_id)

        return questions

    @staticmethod
    def _parse_max_files(raw_value: Any, allow_multiple: bool) -> int | None:
        if not allow_multiple:
            return 1
        if raw_value is None or str(raw_value).strip() == "":
            return None
        try:
            max_files = int(raw_value)
        except (TypeError, ValueError):
            max_files = 5
        return max(1, min(max_files, 20))

    @staticmethod
    def fallback_form() -> TicketForm:
        return TicketForm(
            id="other",
            title="Другое",
            description="Свободное обращение в поддержку",
            button_text="💬 Другое",
            admin_title="Другое обращение",
            success_text=None,
            close_reasons=(),
            questions=(
                TicketQuestion(
                    id="message",
                    text="Опишите вашу проблему:",
                    required=True,
                    answer_type=ANSWER_TYPE_ANY,
                ),
            ),
        )


def is_minecraft_nickname_question(question: TicketQuestion) -> bool:
    profile_field = str(question.profile_field or "").strip().lower()
    if profile_field == PROFILE_FIELD_MINECRAFT_NICKNAME:
        return True
    return str(question.id or "").strip().lower() in MINECRAFT_NICKNAME_QUESTION_IDS


def validate_profile_text_answer(question: TicketQuestion, text: str) -> str | None:
    if not is_minecraft_nickname_question(question):
        return None

    regex = question.validation_regex or DEFAULT_MINECRAFT_NICKNAME_REGEX
    try:
        is_valid = re.fullmatch(regex, text) is not None
    except re.error as error:
        logger.error(
            "Invalid validation_regex for question_id=%s regex=%s: %s",
            question.id,
            regex,
            error,
        )
        is_valid = re.fullmatch(DEFAULT_MINECRAFT_NICKNAME_REGEX, text) is not None
    if is_valid:
        return None
    return question.validation_error or DEFAULT_MINECRAFT_NICKNAME_VALIDATION_ERROR
