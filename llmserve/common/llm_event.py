from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel


class Flag(Enum):
    HATE = "hate"
    OBSCENE = "obscene"
    WRONG_LANGUAGE = "wrong-language"
    NONFACTUAL = "non-factual"


class Vote(BaseModel):
    llm: str
    score: float


class LlmResponse(BaseModel):
    model_id: str
    text: str
    model_conf: Optional[Dict] = None
    gen_stats: Optional[Dict] = None


class LlmEvent(BaseModel):
    created_at: datetime
    # Name of the project
    project_name: str

    # Identifier for a session
    session_id: Optional[str] = None

    # unique string representing this event
    instance_id: str

    # Prompt given by the user
    user_prompt: str
    responses: List[LlmResponse]

    # Vote is a dictionary by llm and the votes
    # that model got. Typically, this is 1.
    votes: Optional[List[Vote]] = None
    vote_comments: Optional[Dict[str, str]] = None

    # Key: llm
    # Value: list of flags
    flag: Optional[Dict[str, List[Flag]]] = None

    # Key: llm
    # Value: Comment for each llm
    flag_comments: Optional[Dict[str, str]] = None
