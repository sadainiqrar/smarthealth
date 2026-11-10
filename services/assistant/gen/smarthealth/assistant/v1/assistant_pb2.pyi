from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ChatRequest(_message.Message):
    __slots__ = ("conversation_id", "turn_id", "message", "resume_from", "choice", "starter_id")
    CONVERSATION_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RESUME_FROM_FIELD_NUMBER: _ClassVar[int]
    CHOICE_FIELD_NUMBER: _ClassVar[int]
    STARTER_ID_FIELD_NUMBER: _ClassVar[int]
    conversation_id: str
    turn_id: str
    message: str
    resume_from: int
    choice: OptionChoice
    starter_id: str
    def __init__(self, conversation_id: _Optional[str] = ..., turn_id: _Optional[str] = ..., message: _Optional[str] = ..., resume_from: _Optional[int] = ..., choice: _Optional[_Union[OptionChoice, _Mapping]] = ..., starter_id: _Optional[str] = ...) -> None: ...

class OptionChoice(_message.Message):
    __slots__ = ("options_id", "option_id")
    OPTIONS_ID_FIELD_NUMBER: _ClassVar[int]
    OPTION_ID_FIELD_NUMBER: _ClassVar[int]
    options_id: str
    option_id: str
    def __init__(self, options_id: _Optional[str] = ..., option_id: _Optional[str] = ...) -> None: ...

class ChatEvent(_message.Message):
    __slots__ = ("turn_id", "seq", "token", "citation", "done", "error", "tool", "options", "confirmation", "card", "suggestions")
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    SEQ_FIELD_NUMBER: _ClassVar[int]
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    CITATION_FIELD_NUMBER: _ClassVar[int]
    DONE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    TOOL_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    CONFIRMATION_FIELD_NUMBER: _ClassVar[int]
    CARD_FIELD_NUMBER: _ClassVar[int]
    SUGGESTIONS_FIELD_NUMBER: _ClassVar[int]
    turn_id: str
    seq: int
    token: str
    citation: Citation
    done: Done
    error: Error
    tool: ToolActivity
    options: Options
    confirmation: ConfirmationRequired
    card: Card
    suggestions: Suggestions
    def __init__(self, turn_id: _Optional[str] = ..., seq: _Optional[int] = ..., token: _Optional[str] = ..., citation: _Optional[_Union[Citation, _Mapping]] = ..., done: _Optional[_Union[Done, _Mapping]] = ..., error: _Optional[_Union[Error, _Mapping]] = ..., tool: _Optional[_Union[ToolActivity, _Mapping]] = ..., options: _Optional[_Union[Options, _Mapping]] = ..., confirmation: _Optional[_Union[ConfirmationRequired, _Mapping]] = ..., card: _Optional[_Union[Card, _Mapping]] = ..., suggestions: _Optional[_Union[Suggestions, _Mapping]] = ...) -> None: ...

class Citation(_message.Message):
    __slots__ = ("source_id", "title", "snippet")
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    SNIPPET_FIELD_NUMBER: _ClassVar[int]
    source_id: str
    title: str
    snippet: str
    def __init__(self, source_id: _Optional[str] = ..., title: _Optional[str] = ..., snippet: _Optional[str] = ...) -> None: ...

class Done(_message.Message):
    __slots__ = ("reason",)
    REASON_FIELD_NUMBER: _ClassVar[int]
    reason: str
    def __init__(self, reason: _Optional[str] = ...) -> None: ...

class Error(_message.Message):
    __slots__ = ("code", "message")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    code: str
    message: str
    def __init__(self, code: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class ToolActivity(_message.Message):
    __slots__ = ("call_id", "name", "label", "status", "summary")
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    name: str
    label: str
    status: str
    summary: str
    def __init__(self, call_id: _Optional[str] = ..., name: _Optional[str] = ..., label: _Optional[str] = ..., status: _Optional[str] = ..., summary: _Optional[str] = ...) -> None: ...

class Options(_message.Message):
    __slots__ = ("options_id", "prompt", "options", "style")
    OPTIONS_ID_FIELD_NUMBER: _ClassVar[int]
    PROMPT_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    STYLE_FIELD_NUMBER: _ClassVar[int]
    options_id: str
    prompt: str
    options: _containers.RepeatedCompositeFieldContainer[Option]
    style: str
    def __init__(self, options_id: _Optional[str] = ..., prompt: _Optional[str] = ..., options: _Optional[_Iterable[_Union[Option, _Mapping]]] = ..., style: _Optional[str] = ...) -> None: ...

class Option(_message.Message):
    __slots__ = ("id", "label", "description", "badge")
    ID_FIELD_NUMBER: _ClassVar[int]
    LABEL_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    BADGE_FIELD_NUMBER: _ClassVar[int]
    id: str
    label: str
    description: str
    badge: str
    def __init__(self, id: _Optional[str] = ..., label: _Optional[str] = ..., description: _Optional[str] = ..., badge: _Optional[str] = ...) -> None: ...

class ConfirmationRequired(_message.Message):
    __slots__ = ("confirmation_id", "action", "title", "details", "confirm_label", "deny_label", "tone")
    CONFIRMATION_ID_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    DETAILS_FIELD_NUMBER: _ClassVar[int]
    CONFIRM_LABEL_FIELD_NUMBER: _ClassVar[int]
    DENY_LABEL_FIELD_NUMBER: _ClassVar[int]
    TONE_FIELD_NUMBER: _ClassVar[int]
    confirmation_id: str
    action: str
    title: str
    details: _containers.RepeatedCompositeFieldContainer[Field]
    confirm_label: str
    deny_label: str
    tone: str
    def __init__(self, confirmation_id: _Optional[str] = ..., action: _Optional[str] = ..., title: _Optional[str] = ..., details: _Optional[_Iterable[_Union[Field, _Mapping]]] = ..., confirm_label: _Optional[str] = ..., deny_label: _Optional[str] = ..., tone: _Optional[str] = ...) -> None: ...

class Card(_message.Message):
    __slots__ = ("kind", "title", "subtitle", "fields", "badge", "link")
    KIND_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    SUBTITLE_FIELD_NUMBER: _ClassVar[int]
    FIELDS_FIELD_NUMBER: _ClassVar[int]
    BADGE_FIELD_NUMBER: _ClassVar[int]
    LINK_FIELD_NUMBER: _ClassVar[int]
    kind: str
    title: str
    subtitle: str
    fields: _containers.RepeatedCompositeFieldContainer[Field]
    badge: str
    link: str
    def __init__(self, kind: _Optional[str] = ..., title: _Optional[str] = ..., subtitle: _Optional[str] = ..., fields: _Optional[_Iterable[_Union[Field, _Mapping]]] = ..., badge: _Optional[str] = ..., link: _Optional[str] = ...) -> None: ...

class Field(_message.Message):
    __slots__ = ("label", "value")
    LABEL_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    label: str
    value: str
    def __init__(self, label: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

class Suggestions(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, items: _Optional[_Iterable[str]] = ...) -> None: ...

class ConfirmRequest(_message.Message):
    __slots__ = ("conversation_id", "turn_id", "confirmation_id", "approved", "resume_from")
    CONVERSATION_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    CONFIRMATION_ID_FIELD_NUMBER: _ClassVar[int]
    APPROVED_FIELD_NUMBER: _ClassVar[int]
    RESUME_FROM_FIELD_NUMBER: _ClassVar[int]
    conversation_id: str
    turn_id: str
    confirmation_id: str
    approved: bool
    resume_from: int
    def __init__(self, conversation_id: _Optional[str] = ..., turn_id: _Optional[str] = ..., confirmation_id: _Optional[str] = ..., approved: _Optional[bool] = ..., resume_from: _Optional[int] = ...) -> None: ...

class CancelRequest(_message.Message):
    __slots__ = ("turn_id",)
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    turn_id: str
    def __init__(self, turn_id: _Optional[str] = ...) -> None: ...

class CancelReply(_message.Message):
    __slots__ = ("cancelled",)
    CANCELLED_FIELD_NUMBER: _ClassVar[int]
    cancelled: bool
    def __init__(self, cancelled: _Optional[bool] = ...) -> None: ...

class StartersRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class StartersReply(_message.Message):
    __slots__ = ("groups",)
    GROUPS_FIELD_NUMBER: _ClassVar[int]
    groups: _containers.RepeatedCompositeFieldContainer[StarterGroup]
    def __init__(self, groups: _Optional[_Iterable[_Union[StarterGroup, _Mapping]]] = ...) -> None: ...

class StarterGroup(_message.Message):
    __slots__ = ("title", "starters")
    TITLE_FIELD_NUMBER: _ClassVar[int]
    STARTERS_FIELD_NUMBER: _ClassVar[int]
    title: str
    starters: _containers.RepeatedCompositeFieldContainer[Starter]
    def __init__(self, title: _Optional[str] = ..., starters: _Optional[_Iterable[_Union[Starter, _Mapping]]] = ...) -> None: ...

class Starter(_message.Message):
    __slots__ = ("id", "title", "description", "icon", "mode", "prompt")
    ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    ICON_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    PROMPT_FIELD_NUMBER: _ClassVar[int]
    id: str
    title: str
    description: str
    icon: str
    mode: str
    prompt: str
    def __init__(self, id: _Optional[str] = ..., title: _Optional[str] = ..., description: _Optional[str] = ..., icon: _Optional[str] = ..., mode: _Optional[str] = ..., prompt: _Optional[str] = ...) -> None: ...

class ListConversationsRequest(_message.Message):
    __slots__ = ("limit",)
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    limit: int
    def __init__(self, limit: _Optional[int] = ...) -> None: ...

class ConversationSummary(_message.Message):
    __slots__ = ("conversation_id", "title", "updated_at", "turns", "awaiting_input")
    CONVERSATION_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    TURNS_FIELD_NUMBER: _ClassVar[int]
    AWAITING_INPUT_FIELD_NUMBER: _ClassVar[int]
    conversation_id: str
    title: str
    updated_at: str
    turns: int
    awaiting_input: bool
    def __init__(self, conversation_id: _Optional[str] = ..., title: _Optional[str] = ..., updated_at: _Optional[str] = ..., turns: _Optional[int] = ..., awaiting_input: _Optional[bool] = ...) -> None: ...

class ListConversationsReply(_message.Message):
    __slots__ = ("conversations",)
    CONVERSATIONS_FIELD_NUMBER: _ClassVar[int]
    conversations: _containers.RepeatedCompositeFieldContainer[ConversationSummary]
    def __init__(self, conversations: _Optional[_Iterable[_Union[ConversationSummary, _Mapping]]] = ...) -> None: ...

class GetConversationRequest(_message.Message):
    __slots__ = ("conversation_id",)
    CONVERSATION_ID_FIELD_NUMBER: _ClassVar[int]
    conversation_id: str
    def __init__(self, conversation_id: _Optional[str] = ...) -> None: ...

class TranscriptTurn(_message.Message):
    __slots__ = ("turn_id", "user_text", "events")
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    USER_TEXT_FIELD_NUMBER: _ClassVar[int]
    EVENTS_FIELD_NUMBER: _ClassVar[int]
    turn_id: str
    user_text: str
    events: _containers.RepeatedCompositeFieldContainer[ChatEvent]
    def __init__(self, turn_id: _Optional[str] = ..., user_text: _Optional[str] = ..., events: _Optional[_Iterable[_Union[ChatEvent, _Mapping]]] = ...) -> None: ...

class ConversationReply(_message.Message):
    __slots__ = ("conversation_id", "title", "turns")
    CONVERSATION_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    TURNS_FIELD_NUMBER: _ClassVar[int]
    conversation_id: str
    title: str
    turns: _containers.RepeatedCompositeFieldContainer[TranscriptTurn]
    def __init__(self, conversation_id: _Optional[str] = ..., title: _Optional[str] = ..., turns: _Optional[_Iterable[_Union[TranscriptTurn, _Mapping]]] = ...) -> None: ...

class DeleteConversationRequest(_message.Message):
    __slots__ = ("conversation_id",)
    CONVERSATION_ID_FIELD_NUMBER: _ClassVar[int]
    conversation_id: str
    def __init__(self, conversation_id: _Optional[str] = ...) -> None: ...

class DeleteConversationReply(_message.Message):
    __slots__ = ("deleted",)
    DELETED_FIELD_NUMBER: _ClassVar[int]
    deleted: bool
    def __init__(self, deleted: _Optional[bool] = ...) -> None: ...

class UsageRequest(_message.Message):
    __slots__ = ("from_date", "to_date")
    FROM_DATE_FIELD_NUMBER: _ClassVar[int]
    TO_DATE_FIELD_NUMBER: _ClassVar[int]
    from_date: str
    to_date: str
    def __init__(self, from_date: _Optional[str] = ..., to_date: _Optional[str] = ...) -> None: ...

class UserUsage(_message.Message):
    __slots__ = ("user_id", "role", "conversations", "turns", "model_turns", "input_tokens", "output_tokens", "cost_usd", "avg_first_token_ms", "avg_total_ms", "errors", "last_active")
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    CONVERSATIONS_FIELD_NUMBER: _ClassVar[int]
    TURNS_FIELD_NUMBER: _ClassVar[int]
    MODEL_TURNS_FIELD_NUMBER: _ClassVar[int]
    INPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    OUTPUT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    COST_USD_FIELD_NUMBER: _ClassVar[int]
    AVG_FIRST_TOKEN_MS_FIELD_NUMBER: _ClassVar[int]
    AVG_TOTAL_MS_FIELD_NUMBER: _ClassVar[int]
    ERRORS_FIELD_NUMBER: _ClassVar[int]
    LAST_ACTIVE_FIELD_NUMBER: _ClassVar[int]
    user_id: str
    role: str
    conversations: int
    turns: int
    model_turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    avg_first_token_ms: float
    avg_total_ms: float
    errors: int
    last_active: str
    def __init__(self, user_id: _Optional[str] = ..., role: _Optional[str] = ..., conversations: _Optional[int] = ..., turns: _Optional[int] = ..., model_turns: _Optional[int] = ..., input_tokens: _Optional[int] = ..., output_tokens: _Optional[int] = ..., cost_usd: _Optional[float] = ..., avg_first_token_ms: _Optional[float] = ..., avg_total_ms: _Optional[float] = ..., errors: _Optional[int] = ..., last_active: _Optional[str] = ...) -> None: ...

class UsageReply(_message.Message):
    __slots__ = ("from_date", "to_date", "provider", "model", "price_input_per_mtok", "price_output_per_mtok", "users", "totals", "drafts_generated", "drafts_sent", "drafts_discarded", "draft_recipients", "bookings_total", "bookings_via_assistant", "conversations_with_booking", "conversations")
    FROM_DATE_FIELD_NUMBER: _ClassVar[int]
    TO_DATE_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    PRICE_INPUT_PER_MTOK_FIELD_NUMBER: _ClassVar[int]
    PRICE_OUTPUT_PER_MTOK_FIELD_NUMBER: _ClassVar[int]
    USERS_FIELD_NUMBER: _ClassVar[int]
    TOTALS_FIELD_NUMBER: _ClassVar[int]
    DRAFTS_GENERATED_FIELD_NUMBER: _ClassVar[int]
    DRAFTS_SENT_FIELD_NUMBER: _ClassVar[int]
    DRAFTS_DISCARDED_FIELD_NUMBER: _ClassVar[int]
    DRAFT_RECIPIENTS_FIELD_NUMBER: _ClassVar[int]
    BOOKINGS_TOTAL_FIELD_NUMBER: _ClassVar[int]
    BOOKINGS_VIA_ASSISTANT_FIELD_NUMBER: _ClassVar[int]
    CONVERSATIONS_WITH_BOOKING_FIELD_NUMBER: _ClassVar[int]
    CONVERSATIONS_FIELD_NUMBER: _ClassVar[int]
    from_date: str
    to_date: str
    provider: str
    model: str
    price_input_per_mtok: float
    price_output_per_mtok: float
    users: _containers.RepeatedCompositeFieldContainer[UserUsage]
    totals: UserUsage
    drafts_generated: int
    drafts_sent: int
    drafts_discarded: int
    draft_recipients: int
    bookings_total: int
    bookings_via_assistant: int
    conversations_with_booking: int
    conversations: int
    def __init__(self, from_date: _Optional[str] = ..., to_date: _Optional[str] = ..., provider: _Optional[str] = ..., model: _Optional[str] = ..., price_input_per_mtok: _Optional[float] = ..., price_output_per_mtok: _Optional[float] = ..., users: _Optional[_Iterable[_Union[UserUsage, _Mapping]]] = ..., totals: _Optional[_Union[UserUsage, _Mapping]] = ..., drafts_generated: _Optional[int] = ..., drafts_sent: _Optional[int] = ..., drafts_discarded: _Optional[int] = ..., draft_recipients: _Optional[int] = ..., bookings_total: _Optional[int] = ..., bookings_via_assistant: _Optional[int] = ..., conversations_with_booking: _Optional[int] = ..., conversations: _Optional[int] = ...) -> None: ...

class StartReportRequest(_message.Message):
    __slots__ = ("kind", "from_date", "to_date")
    KIND_FIELD_NUMBER: _ClassVar[int]
    FROM_DATE_FIELD_NUMBER: _ClassVar[int]
    TO_DATE_FIELD_NUMBER: _ClassVar[int]
    kind: str
    from_date: str
    to_date: str
    def __init__(self, kind: _Optional[str] = ..., from_date: _Optional[str] = ..., to_date: _Optional[str] = ...) -> None: ...

class ReportJob(_message.Message):
    __slots__ = ("report_id", "title", "status", "from_date", "to_date", "created_at")
    REPORT_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    FROM_DATE_FIELD_NUMBER: _ClassVar[int]
    TO_DATE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    report_id: str
    title: str
    status: str
    from_date: str
    to_date: str
    created_at: str
    def __init__(self, report_id: _Optional[str] = ..., title: _Optional[str] = ..., status: _Optional[str] = ..., from_date: _Optional[str] = ..., to_date: _Optional[str] = ..., created_at: _Optional[str] = ...) -> None: ...

class WatchReportRequest(_message.Message):
    __slots__ = ("report_id", "resume_from")
    REPORT_ID_FIELD_NUMBER: _ClassVar[int]
    RESUME_FROM_FIELD_NUMBER: _ClassVar[int]
    report_id: str
    resume_from: int
    def __init__(self, report_id: _Optional[str] = ..., resume_from: _Optional[int] = ...) -> None: ...

class ListReportsRequest(_message.Message):
    __slots__ = ("limit",)
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    limit: int
    def __init__(self, limit: _Optional[int] = ...) -> None: ...

class ListReportsReply(_message.Message):
    __slots__ = ("reports",)
    REPORTS_FIELD_NUMBER: _ClassVar[int]
    reports: _containers.RepeatedCompositeFieldContainer[ReportJob]
    def __init__(self, reports: _Optional[_Iterable[_Union[ReportJob, _Mapping]]] = ...) -> None: ...
