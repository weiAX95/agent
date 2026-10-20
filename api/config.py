from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str

    # JWT 配置
    jwt_signing_key: str  # HS256 签名密钥（开发环境从 .env 读取）
    jwt_verifying_key: str  # HS256 验证密钥（与签名密钥相同）
    jwt_issuer: str  # 令牌签发者标识，如 "agent-api"
    jwt_audience: str  # 预期接收方标识，如 "agent-web"
    jwt_access_token_expire_minutes: int = 15

    class Config:
        env_file = ".env"
        extra = "ignore"  # 你.env里其他和agent相关的变量不会报错


settings = Settings()
