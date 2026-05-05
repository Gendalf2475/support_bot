from __future__ import annotations

import logging
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from app.bot.services.ticket_form_service import ANSWER_TYPE_MEDIA, TicketForm, TicketQuestion
from app.bot.services.ticket_service import TicketService


logger = logging.getLogger(__name__)

DISCORD_SELECT_LIMIT = 25
STALE_ACTION_TEXT = "Это действие уже неактуально."

FormSelectCallback = Callable[[Any, str], Awaitable[None]]
ActionCallback = Callable[[Any, str, int | None, str | None], Awaitable[None]]


class FormSelectView(discord.ui.View):
    def __init__(
        self,
        forms: list[TicketForm],
        owner_id: int,
        on_select: FormSelectCallback,
        *,
        placeholder: str = "Выберите тип обращения",
    ) -> None:
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.on_select = on_select
        select = discord.ui.Select(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=build_form_select_options(forms),
            custom_id=f"majure_form_select:{owner_id}",
        )
        select.callback = self._select_callback
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await send_interaction_notice(interaction, "Это меню открыто для другого пользователя.")
        return False

    async def _select_callback(self, interaction: discord.Interaction) -> None:
        select = next((item for item in self.children if isinstance(item, discord.ui.Select)), None)
        if select is None or not select.values:
            await send_interaction_notice(interaction, STALE_ACTION_TEXT)
            return
        await self.on_select(interaction, str(select.values[0]))


class QuestionActionView(discord.ui.View):
    def __init__(
        self,
        question: TicketQuestion,
        question_index: int,
        owner_id: int,
        on_action: ActionCallback,
    ) -> None:
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.on_action = on_action
        if not question.required:
            skip_button = discord.ui.Button(
                label="Пропустить",
                style=discord.ButtonStyle.secondary,
                custom_id=f"majure_question_skip:{question_index}",
            )
            skip_button.callback = self._skip_callback
            self.add_item(skip_button)

        cancel_button = discord.ui.Button(
            label="Отмена",
            style=discord.ButtonStyle.danger,
            custom_id=f"majure_question_cancel:{question_index}",
        )
        cancel_button.callback = self._cancel_callback
        self.add_item(cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await send_interaction_notice(interaction, "Эта кнопка доступна только автору обращения.")
        return False

    async def _skip_callback(self, interaction: discord.Interaction) -> None:
        await self.on_action(interaction, "skip", _parse_custom_index(interaction), None)

    async def _cancel_callback(self, interaction: discord.Interaction) -> None:
        await self.on_action(interaction, "cancel", _parse_custom_index(interaction), None)


class MediaContinueView(discord.ui.View):
    def __init__(self, question_index: int, owner_id: int, on_action: ActionCallback) -> None:
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.on_action = on_action

        continue_button = discord.ui.Button(
            label="Продолжить",
            style=discord.ButtonStyle.success,
            custom_id=f"majure_media_continue:{question_index}",
        )
        continue_button.callback = self._continue_callback
        self.add_item(continue_button)

        cancel_button = discord.ui.Button(
            label="Отмена",
            style=discord.ButtonStyle.danger,
            custom_id=f"majure_media_cancel:{question_index}",
        )
        cancel_button.callback = self._cancel_callback
        self.add_item(cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await send_interaction_notice(interaction, "Эта кнопка доступна только автору обращения.")
        return False

    async def _continue_callback(self, interaction: discord.Interaction) -> None:
        await self.on_action(interaction, "continue", _parse_custom_index(interaction), None)

    async def _cancel_callback(self, interaction: discord.Interaction) -> None:
        await self.on_action(interaction, "cancel", _parse_custom_index(interaction), None)


class MinecraftNicknameView(discord.ui.View):
    def __init__(self, question_index: int, owner_id: int, on_action: ActionCallback) -> None:
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.on_action = on_action

        yes_button = discord.ui.Button(
            label="Да",
            style=discord.ButtonStyle.success,
            custom_id=f"majure_profile_nickname_yes:{question_index}",
        )
        yes_button.callback = self._yes_callback
        self.add_item(yes_button)

        other_button = discord.ui.Button(
            label="Ввести другой",
            style=discord.ButtonStyle.secondary,
            custom_id=f"majure_profile_nickname_other:{question_index}",
        )
        other_button.callback = self._other_callback
        self.add_item(other_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await send_interaction_notice(interaction, "Эта кнопка доступна только автору обращения.")
        return False

    async def _yes_callback(self, interaction: discord.Interaction) -> None:
        await self.on_action(interaction, "profile_yes", _parse_custom_index(interaction), None)

    async def _other_callback(self, interaction: discord.Interaction) -> None:
        await self.on_action(interaction, "profile_other", _parse_custom_index(interaction), None)


class MinecraftLookupConfirmView(discord.ui.View):
    def __init__(self, question_index: int, owner_id: int, on_action: ActionCallback) -> None:
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.on_action = on_action

        continue_button = discord.ui.Button(
            label="Продолжить",
            style=discord.ButtonStyle.success,
            custom_id=f"majure_minecraft_lookup_continue:{question_index}",
        )
        continue_button.callback = self._continue_callback
        self.add_item(continue_button)

        other_button = discord.ui.Button(
            label="Ввести другой",
            style=discord.ButtonStyle.secondary,
            custom_id=f"majure_minecraft_lookup_other:{question_index}",
        )
        other_button.callback = self._other_callback
        self.add_item(other_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await send_interaction_notice(interaction, "Эта кнопка доступна только автору обращения.")
        return False

    async def _continue_callback(self, interaction: discord.Interaction) -> None:
        await self.on_action(interaction, "minecraft_lookup_continue", _parse_custom_index(interaction), None)

    async def _other_callback(self, interaction: discord.Interaction) -> None:
        await self.on_action(interaction, "minecraft_lookup_other", _parse_custom_index(interaction), None)


class TicketPreviewView(discord.ui.View):
    def __init__(self, form_id: str, owner_id: int, on_action: ActionCallback) -> None:
        super().__init__(timeout=None)
        self.owner_id = owner_id
        self.form_id = form_id
        self.on_action = on_action

        submit_button = discord.ui.Button(
            label="Отправить",
            style=discord.ButtonStyle.success,
            custom_id=f"majure_preview_submit:{form_id}",
        )
        submit_button.callback = self._submit_callback
        self.add_item(submit_button)

        restart_button = discord.ui.Button(
            label="Заполнить заново",
            style=discord.ButtonStyle.secondary,
            custom_id=f"majure_preview_restart:{form_id}",
        )
        restart_button.callback = self._restart_callback
        self.add_item(restart_button)

        cancel_button = discord.ui.Button(
            label="Отмена",
            style=discord.ButtonStyle.danger,
            custom_id=f"majure_preview_cancel:{form_id}",
        )
        cancel_button.callback = self._cancel_callback
        self.add_item(cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await send_interaction_notice(interaction, "Эта кнопка доступна только автору обращения.")
        return False

    async def _submit_callback(self, interaction: discord.Interaction) -> None:
        await self.on_action(interaction, "submit", None, self.form_id)

    async def _restart_callback(self, interaction: discord.Interaction) -> None:
        await self.on_action(interaction, "restart", None, self.form_id)

    async def _cancel_callback(self, interaction: discord.Interaction) -> None:
        await self.on_action(interaction, "cancel", None, self.form_id)


class ClosedTicketView(FormSelectView):
    def __init__(self, forms: list[TicketForm], owner_id: int, on_select: FormSelectCallback) -> None:
        super().__init__(
            forms,
            owner_id,
            on_select,
            placeholder="Выберите тип нового обращения",
        )


def build_main_menu_embed() -> discord.Embed:
    return discord.Embed(
        title="MAJURE SUPPORT",
        description=(
            "Здравствуйте! Здесь вы можете обратиться в поддержку.\n\n"
            "Выберите тип обращения ниже."
        ),
        color=discord.Color.blurple(),
    )


def build_question_embed(
    form: TicketForm,
    question_index: int,
    *,
    prefix_text: str | None = None,
) -> discord.Embed:
    question = form.questions[question_index]
    description_parts = [
        f"Вопрос {question_index + 1} из {len(form.questions)}",
        "",
        question.text,
    ]
    if question.help_text:
        description_parts.extend(["", "Подсказка:", question.help_text])
    if prefix_text:
        description_parts = [prefix_text, ""] + description_parts

    embed = discord.Embed(
        title=form.title,
        description="\n".join(description_parts),
        color=discord.Color.blurple(),
    )
    if question.answer_type == ANSWER_TYPE_MEDIA:
        embed.set_footer(text="Прикрепите файл, фото или видео следующим сообщением.")
    else:
        embed.set_footer(text="Ответьте следующим сообщением.")
    return embed


def build_media_continue_embed(
    media_count: int,
    max_files: int | None,
    *,
    limit_reached: bool = False,
) -> discord.Embed:
    if max_files is None:
        description = (
            f"Файлы добавлены: {media_count}.\n"
            "Можно отправить ещё файл или нажать «Продолжить»."
        )
    elif limit_reached:
        description = (
            f"Файлы добавлены: {media_count} из {max_files}.\n"
            "Достигнут лимит файлов.\n\n"
            "Нажмите «Продолжить», чтобы перейти дальше."
        )
    else:
        description = (
            f"Файлы добавлены: {media_count} из {max_files}.\n"
            "Можно отправить ещё файл или нажать «Продолжить»."
        )
    return discord.Embed(
        title="Файлы добавлены",
        description=description,
        color=discord.Color.green(),
    )


def build_minecraft_nickname_embed(nickname: str) -> discord.Embed:
    return discord.Embed(
        title="Игровой ник",
        description=f"Использовать прошлый ник {nickname}?",
        color=discord.Color.blurple(),
    )


def build_minecraft_lookup_not_found_embed(nickname: str) -> discord.Embed:
    return discord.Embed(
        title="Игрок не найден",
        description=(
            f"⚠️ Игрок с ником {nickname} не найден на сервере.\n"
            "Вы можете продолжить, если уверены, что ник указан правильно."
        ),
        color=discord.Color.gold(),
    )


def build_ticket_preview_embed(form: TicketForm, answers: list[dict[str, Any]]) -> discord.Embed:
    embed = discord.Embed(
        title="Проверьте данные тикета",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Тип обращения", value=_limit_field_value(form.title), inline=False)

    answers_by_question = {answer.get("question_id"): answer for answer in answers}
    max_question_fields = 24 if len(form.questions) <= 24 else 23
    for question_number, question in enumerate(form.questions[:max_question_fields], start=1):
        answer = answers_by_question.get(question.id)
        embed.add_field(
            name=_limit_field_name(f"{question_number}. {question.text.strip().rstrip(':')}"),
            value=_format_preview_answer(answer),
            inline=False,
        )
    if len(form.questions) > max_question_fields:
        embed.add_field(
            name="Остальные вопросы",
            value=f"Ещё вопросов: {len(form.questions) - max_question_fields}",
            inline=False,
        )
    return embed


def build_ticket_sent_embed(ticket_id: int | None = None, success_text: str | None = None) -> discord.Embed:
    description = success_text or "Ваш тикет отправлен в поддержку.\nОтвет придёт сюда."
    if ticket_id is not None and success_text is None:
        description = f"{description}\n\nТикет: #{ticket_id}"
    return discord.Embed(
        title="✅ Тикет отправлен",
        description=description,
        color=discord.Color.green(),
    )


def build_closed_ticket_embed(text: str) -> discord.Embed:
    description = text.strip()
    if description.startswith("✅ "):
        description = description[2:].strip()
    return discord.Embed(
        title="✅ Тикет закрыт",
        description=description,
        color=discord.Color.green(),
    )


def build_form_select_options(forms: list[TicketForm]) -> list[discord.SelectOption]:
    visible_forms = forms[:DISCORD_SELECT_LIMIT]
    if len(forms) > DISCORD_SELECT_LIMIT:
        logger.warning("Discord select menu supports only %s forms; showing first %s", DISCORD_SELECT_LIMIT, DISCORD_SELECT_LIMIT)

    options = []
    for form in visible_forms:
        option_kwargs: dict[str, Any] = {
            "label": _limit_option_text(form.title or form.button_text),
            "value": form.id,
        }
        if form.description:
            option_kwargs["description"] = _limit_option_text(form.description)
        emoji = extract_leading_emoji(form.button_text)
        if emoji:
            option_kwargs["emoji"] = emoji
        options.append(discord.SelectOption(**option_kwargs))
    return options


async def send_interaction_notice(interaction: discord.Interaction, text: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
        return
    except Exception as error:
        logger.warning("Failed to send ephemeral Discord interaction response: %s", error)

    try:
        if interaction.response.is_done():
            await interaction.followup.send(text)
        else:
            await interaction.response.send_message(text)
    except Exception as error:
        logger.exception("Failed to send Discord interaction response: %s", error)


def extract_leading_emoji(text: str) -> str | None:
    first_token = (text or "").strip().split(maxsplit=1)[0] if text else ""
    if not first_token:
        return None
    if any(unicodedata.category(character) == "So" for character in first_token):
        return first_token
    return None


def _format_preview_answer(answer: dict[str, Any] | None) -> str:
    if answer is None or answer.get("skipped"):
        return "— Пропущено"
    if answer.get("answer_type") == "media":
        media_count = len(TicketService.extract_media_files(answer))
        return f"📎 Медиафайлов: {media_count}" if media_count else "— Пропущено"
    return _limit_field_value(str(answer.get("answer_text") or "не указано"))


def _parse_custom_index(interaction: discord.Interaction) -> int | None:
    custom_id = getattr(getattr(interaction, "data", None), "custom_id", None)
    if custom_id is None and isinstance(getattr(interaction, "data", None), dict):
        custom_id = interaction.data.get("custom_id")
    if not custom_id or ":" not in str(custom_id):
        return None
    raw_index = str(custom_id).rsplit(":", maxsplit=1)[-1]
    try:
        return int(raw_index)
    except ValueError:
        return None


def _limit_option_text(text: str) -> str:
    return _limit_text(text, 100)


def _limit_field_name(text: str) -> str:
    return _limit_text(text, 256)


def _limit_field_value(text: str) -> str:
    return _limit_text(text, 1024) or "—"


def _limit_text(text: str, limit: int) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"
