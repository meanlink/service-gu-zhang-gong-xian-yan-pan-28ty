from pydantic import BaseModel, Field
class LimitInput(BaseModel):
    capacity_limit_bytes: int = Field(gt=0, le=10**15)
