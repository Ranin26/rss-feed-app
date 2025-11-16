from pydantic import BaseModel  
from typing import Optional     

class Keyword(BaseModel):
    word: str
    type: str  # "whitelist" or "blacklist"

# Models
class Feed(BaseModel):
    url: str

class Entry(BaseModel):
    id: str
    feed_url: str
    title: str
    link: str
    published: Optional[str]
    published_parsed_tz: Optional[str]
    summary: Optional[str]

class Setting(BaseModel):
    name: str
    value: str
