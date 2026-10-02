from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str

    class Config:
        env_file = ".env"
        extra = "ignore"  # 你.env里其他和agent相关的变量不会报错


settings = Settings()
