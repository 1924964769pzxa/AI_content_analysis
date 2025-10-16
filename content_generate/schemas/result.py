from typing import List
from pydantic import BaseModel

class GenerateResult(BaseModel):
    task_id: str
    title: str
    content: str
    img_list: List[str]
