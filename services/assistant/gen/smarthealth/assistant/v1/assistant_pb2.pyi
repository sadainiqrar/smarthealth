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
