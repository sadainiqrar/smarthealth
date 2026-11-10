from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ChatRequest(_message.Message):
    __slots__ = ("conversation_id", "turn_id", "message", "resume_from")
    CONVERSATION_ID_FIELD_NUMBER: _ClassVar[int]
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RESUME_FROM_FIELD_NUMBER: _ClassVar[int]
    conversation_id: str
    turn_id: str
    message: str
    resume_from: int
    def __init__(self, conversation_id: _Optional[str] = ..., turn_id: _Optional[str] = ..., message: _Optional[str] = ..., resume_from: _Optional[int] = ...) -> None: ...

class ChatEvent(_message.Message):
    __slots__ = ("turn_id", "seq", "token", "citation", "done", "error")
    TURN_ID_FIELD_NUMBER: _ClassVar[int]
    SEQ_FIELD_NUMBER: _ClassVar[int]
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    CITATION_FIELD_NUMBER: _ClassVar[int]
    DONE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    turn_id: str
    seq: int
    token: str
    citation: Citation
    done: Done
    error: Error
    def __init__(self, turn_id: _Optional[str] = ..., seq: _Optional[int] = ..., token: _Optional[str] = ..., citation: _Optional[_Union[Citation, _Mapping]] = ..., done: _Optional[_Union[Done, _Mapping]] = ..., error: _Optional[_Union[Error, _Mapping]] = ...) -> None: ...

class Citation(_message.Message):
    __slots__ = ("source_id", "title")
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    source_id: str
    title: str
    def __init__(self, source_id: _Optional[str] = ..., title: _Optional[str] = ...) -> None: ...

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
