from __future__ import annotations

ACTION_SELECT_FORM = "select_form"
ACTION_CANCEL = "cancel"
ACTION_SKIP = "skip"
ACTION_SUBMIT = "submit"
ACTION_RESTART = "restart"
ACTION_CONTINUE = "continue"
ACTION_MEDIA_CONTINUE = ACTION_CONTINUE
ACTION_PROFILE_NICKNAME_YES = "profile_yes"
ACTION_PROFILE_NICKNAME_OTHER = "profile_other"
ACTION_PROFILE_NICKNAME_CHANGE_CONFIRM = "profile_change_confirm"
ACTION_PROFILE_NICKNAME_CHANGE_CANCEL = "profile_change_cancel"
ACTION_MINECRAFT_LOOKUP_CONTINUE = "minecraft_lookup_continue"
ACTION_MINECRAFT_LOOKUP_OTHER = "minecraft_lookup_other"
ACTION_CLOSE_TICKET = "ticket_close"
ACTION_CLOSE_REASON = "ticket_close_reason"
ACTION_TICKET_CLOSED = "ticket_closed"

TG_CALLBACK_OPEN_TICKET = "ticket_open"
TG_CALLBACK_CANCEL_TICKET = "ticket_cancel"
TG_CALLBACK_SUBMIT_TICKET = "ticket_submit"
TG_CALLBACK_RESTART_TICKET = "ticket_restart"
TG_CALLBACK_SKIP_QUESTION = "ticket_skip"
TG_CALLBACK_CONTINUE_MEDIA = "ticket_media_continue"
TG_CALLBACK_PROFILE_NICKNAME_YES = "ticket_profile_nickname_yes"
TG_CALLBACK_PROFILE_NICKNAME_OTHER = "ticket_profile_nickname_other"
TG_CALLBACK_PROFILE_NICKNAME_CHANGE_CONFIRM = "ticket_profile_nickname_change_confirm"
TG_CALLBACK_PROFILE_NICKNAME_CHANGE_CANCEL = "ticket_profile_nickname_change_cancel"
TG_CALLBACK_MINECRAFT_LOOKUP_CONTINUE = "ticket_minecraft_lookup_continue"
TG_CALLBACK_MINECRAFT_LOOKUP_OTHER = "ticket_minecraft_lookup_other"
TG_CALLBACK_FORM_PREFIX = "ticket_form:"
TG_CALLBACK_CLOSE_PREFIX = f"{ACTION_CLOSE_TICKET}:"
TG_CALLBACK_CLOSE_REASON_PREFIX = f"{ACTION_CLOSE_REASON}:"

TEXT_YES = {"да", "yes", "y"}
TEXT_CANCEL = {"отмена", "cancel"}
TEXT_SKIP = {"пропустить", "skip"}
TEXT_SUBMIT = {"отправить", "send", "submit", "да"}
TEXT_RESTART = {"заново", "restart"}
TEXT_CONTINUE = {"продолжить", "continue", "далее"}
TEXT_PROFILE_OTHER = {"изменить ник", "ввести другой", "другой", "нет", "no"}
TEXT_PROFILE_CHANGE_CONFIRM = {"да", "да, изменить", "изменить", "yes", "y"}
TEXT_LOOKUP_OTHER = {"ввести другой", "другой", "нет", "no"}

EXTERNAL_CONTROL_TEXTS = (
    TEXT_SUBMIT
    | TEXT_RESTART
    | TEXT_CANCEL
    | TEXT_CONTINUE
    | TEXT_SKIP
    | TEXT_PROFILE_OTHER
    | {"да, изменить", "изменить"}
)
