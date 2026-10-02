"""
比特币 AI 合约交易机器人 Demo

⚠️ 重要风险提示：
    本程序仅用于学习和演示，不构成任何投资建议。
    加密货币合约交易风险极高，可能导致全部本金损失。
    默认启用模拟交易模式（PAPER_TRADING=True），不会真正下单。
    如需测试真实 API，请使用币安 Testnet，切勿直接使用实盘。

依赖：
    pip install python-binance pandas numpy ta openai python-dotenv

币安 Testnet 合约入口：
    https://testnet.binancefuture.com/
    在此创建 API Key/Secret 用于测试。

运行：
    python crypto_trading_bot.py
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import dotenv
import numpy as np
import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException
from openai import OpenAI

# 加载 .env 配置
dotenv.load_dotenv()

# ============ 配置区域 ============
# OpenAI / 阿里云兼容配置
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("api-key")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL") or os.getenv("base-url") or "https://api.openai.com/v1"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "qwen-plus")

# 币安 API（建议先用 Testnet）
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
# True=测试网，False=实盘（不推荐在 demo 中关闭）
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() in ("true", "1", "yes")

# 交易参数
SYMBOL = os.getenv("TRADING_SYMBOL", "BTCUSDT")
INTERVAL = Client.KLINE_INTERVAL_1HOUR  # 1小时K线
LIMIT = 100  # 拉取最近 100 根K线
LEVERAGE = int(os.getenv("LEVERAGE", "5"))  # 合约杠杆
QUANTITY = float(os.getenv("TRADE_QUANTITY", "0.001"))  # 下单数量（BTC）

# 安全开关：True=只模拟下单，False=真实下单
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() in ("true", "1", "yes")


@dataclass
class TradingSignal:
    action: Literal["LONG", "SHORT", "HOLD"]
    reason: str
    confidence: int  # 0-100
    stop_loss: float | None = None
    take_profit: float | None = None


def create_openai_client() -> OpenAI:
    return OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)


def create_binance_client() -> Client:
    """创建币安客户端，默认使用 Testnet。"""
    client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    if USE_TESTNET:
        # 币安 Testnet 合约 API 地址
        client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
        client.FUTURES_DATA_URL = "https://testnet.binancefuture.com/fapi"
    return client


def fetch_klines(client: Client, symbol: str, interval: str, limit: int = 100) -> pd.DataFrame:
    """获取币安 K 线数据并计算基础技术指标。"""
    klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)

    df = pd.DataFrame(
        klines,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore"
        ]
    )

    numeric_cols = ["open", "high", "low", "close", "volume"]
    df[numeric_cols] = df[numeric_cols].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")

    # 简单技术指标
    df["sma_20"] = df["close"].rolling(window=20).mean()
    df["sma_50"] = df["close"].rolling(window=50).mean()
    df["rsi_14"] = compute_rsi(df["close"], 14)
    df["volatility"] = df["close"].pct_change().rolling(window=20).std() * 100

    return df


def compute_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """计算 RSI 指标。"""
    delta = prices.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def build_market_summary(df: pd.DataFrame) -> dict:
    """构造给 AI 的市场摘要。"""
    latest = df.iloc[-1]
    prev = df.iloc[-2]

    return {
        "symbol": SYMBOL,
        "timestamp": datetime.now().isoformat(),
        "current_price": round(latest["close"], 2),
        "price_change_24h_pct": round((latest["close"] - df.iloc[-24]["close"]) / df.iloc[-24]["close"] * 100, 2) if len(df) >= 24 else None,
        "sma_20": round(latest["sma_20"], 2),
        "sma_50": round(latest["sma_50"], 2),
        "rsi_14": round(latest["rsi_14"], 2),
        "volatility_20": round(latest["volatility"], 4),
        "volume": round(latest["volume"], 4),
        "trend": "UP" if latest["close"] > latest["sma_20"] > latest["sma_50"] else "DOWN" if latest["close"] < latest["sma_20"] < latest["sma_50"] else "SIDEWAYS"
    }


def analyze_with_ai(client: OpenAI, market_summary: dict) -> TradingSignal:
    """调用大模型分析市场数据并给出交易信号。"""
    prompt = f"""你是一位专业的加密货币交易员，擅长技术分析和风险管理。

请根据以下 BTC/USDT 市场数据，给出接下来 1 小时的操作建议：

```json
{json.dumps(market_summary, ensure_ascii=False, indent=2)}
```

要求：
1. 只能从 "LONG"（开多）/ "SHORT"（开空）/ "HOLD"（观望）中选择一种。
2. 给出简短理由，说明技术面依据。
3. 给出信心指数 0-100。
4. 如果建议开单，给出建议的止损价（stop_loss）和止盈价（take_profit）。

请严格按以下 JSON 格式输出，不要包含任何其他内容：
{{
  "action": "LONG|SHORT|HOLD",
  "reason": "...",
  "confidence": 75,
  "stop_loss": 62000.0,
  "take_profit": 72000.0
}}
"""

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=500,
    )

    content = response.choices[0].message.content or ""

    # 尝试解析 JSON
    try:
        # 兼容模型可能包裹在 ```json ... ``` 中
        clean = content.strip()
        if clean.startswith("```"):
            clean = clean.strip("`")
            if clean.lower().startswith("json"):
                clean = clean[4:].strip()
        result = json.loads(clean)

        return TradingSignal(
            action=result.get("action", "HOLD").upper(),
            reason=result.get("reason", "AI 未给出理由"),
            confidence=int(result.get("confidence", 0)),
            stop_loss=result.get("stop_loss"),
            take_profit=result.get("take_profit"),
        )
    except Exception as e:
        print(f"AI 输出解析失败: {e}")
        print(f"原始输出: {content}")
        return TradingSignal(action="HOLD", reason="AI 输出解析失败，观望", confidence=0)


def set_leverage(client: Client, symbol: str, leverage: int):
    """设置合约杠杆。"""
    try:
        client.futures_change_leverage(symbol=symbol, leverage=leverage)
        print(f"杠杆已设置为 {leverage}x")
    except BinanceAPIException as e:
        print(f"设置杠杆失败: {e}")


def place_order(client: Client, signal: TradingSignal, current_price: float):
    """根据信号下单。PAPER_TRADING=True 时只模拟。"""
    side = "BUY" if signal.action == "LONG" else "SELL" if signal.action == "SHORT" else None
    if side is None:
        print("信号为 HOLD，不下单")
        return

    print("\n" + "=" * 50)
    print(f"交易信号: {signal.action}")
    print(f"理由: {signal.reason}")
    print(f"信心指数: {signal.confidence}/100")
    print(f"当前价格: {current_price}")
    print(f"建议止损: {signal.stop_loss}")
    print(f"建议止盈: {signal.take_profit}")
    print(f"下单方向: {side}")
    print(f"下单数量: {QUANTITY} BTC")
    print(f"杠杆: {LEVERAGE}x")
    print("=" * 50)

    if PAPER_TRADING:
        print("\n[模拟交易模式] 未调用真实 API，仅打印以上订单信息")
        return

    # 真实下单（仅建议在 Testnet 测试）
    try:
        order = client.futures_create_order(
            symbol=SYMBOL,
            side=side,
            type="MARKET",
            quantity=QUANTITY,
        )
        print(f"下单成功: {order['orderId']}")

        # 设置止损/止盈（简化版，实际应使用 OCO 或单独下止盈止损单）
        if signal.stop_loss:
            sl_side = "SELL" if side == "BUY" else "BUY"
            client.futures_create_order(
                symbol=SYMBOL,
                side=sl_side,
                type="STOP_MARKET",
                stopPrice=signal.stop_loss,
                closePosition=True,
            )
            print(f"止损单已挂: {signal.stop_loss}")

        if signal.take_profit:
            tp_side = "SELL" if side == "BUY" else "BUY"
            client.futures_create_order(
                symbol=SYMBOL,
                side=tp_side,
                type="TAKE_PROFIT_MARKET",
                stopPrice=signal.take_profit,
                closePosition=True,
            )
            print(f"止盈单已挂: {signal.take_profit}")

    except BinanceAPIException as e:
        print(f"下单失败: {e}")


def main():
    print("=" * 60)
    print("比特币 AI 合约交易机器人 Demo")
    print("=" * 60)
    print(f"交易对: {SYMBOL}")
    print(f"网络模式: {'Testnet' if USE_TESTNET else 'PROD（实盘）'}")
    print(f"交易模式: {'模拟/Paper' if PAPER_TRADING else '真实交易'}")
    print("=" * 60)

    if not PAPER_TRADING and not USE_TESTNET:
        print("\n⚠️ 警告：你正在配置为实盘真实交易！")
        print("请确认你完全了解风险，并建议先在 Testnet 测试。")
        confirm = input("输入 'I UNDERSTAND THE RISKS' 继续: ")
        if confirm != "I UNDERSTAND THE RISKS":
            print("未确认，程序退出")
            return

    # 检查 API Key
    if not OPENAI_API_KEY:
        print("\n❌ 缺少 OPENAI_API_KEY，请在 .env 中配置")
        return

    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        print("\n⚠️ 缺少币安 API Key，将只进行市场数据拉取和 AI 分析（无法下单）")
        if not PAPER_TRADING:
            return

    # 初始化客户端
    openai_client = create_openai_client()
    binance_client = create_binance_client() if BINANCE_API_KEY else None

    # 拉取市场数据
    print("\n正在拉取市场数据...")
    if binance_client:
        df = fetch_klines(binance_client, SYMBOL, INTERVAL, LIMIT)
    else:
        # 没有 API Key 时，使用币安公开接口读取 K 线
        public_client = Client()
        df = fetch_klines(public_client, SYMBOL, INTERVAL, LIMIT)

    market_summary = build_market_summary(df)
    print("\n市场摘要:")
    print(json.dumps(market_summary, ensure_ascii=False, indent=2))

    # AI 分析
    print("\n正在调用 AI 分析...")
    signal = analyze_with_ai(openai_client, market_summary)

    # 下单
    if binance_client and signal.action in ("LONG", "SHORT"):
        set_leverage(binance_client, SYMBOL, LEVERAGE)
        place_order(binance_client, signal, market_summary["current_price"])
    else:
        print(f"\n最终建议: {signal.action}")
        print(f"理由: {signal.reason}")
        print(f"信心: {signal.confidence}")
        if not binance_client:
            print("（未配置币安 API，仅展示分析结果）")


if __name__ == "__main__":
    main()
