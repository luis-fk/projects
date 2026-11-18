from datetime import date

import pytest

from news_podcast.api.core.costs import (
    format_brl,
    total_cost_usd,
    usage_cost_usd,
)
from news_podcast.api.core.messages import (
    failure_message,
    long_date,
)
from news_podcast.api.core.schemas import (
    ModelPrice,
    Usage,
)
from tests.news_podcast.factories import build_settings

NOVEMBER = date(2026, 11, 6)
PRICES = {
    "gemini-3.6-flash": ModelPrice(0.75, 3.75),
    "gemini-3.8-flash-lite-tts": ModelPrice(0.50, 6.00),
}


def test_voice_cost_follows_audio_tokens() -> None:
    eighteen_minutes = Usage(
        step="voice",
        model="gemini-3.8-flash-lite-tts",
        input_tokens=0,
        output_tokens=18 * 60 * 25,
    )

    assert usage_cost_usd(eighteen_minutes, PRICES) == pytest.approx(0.162)


def test_unknown_models_cost_nothing() -> None:
    assert usage_cost_usd(Usage(step="voice", model="x", output_tokens=10), PRICES) == 0


def test_brazilian_number_formats() -> None:
    assert format_brl(1234.5) == "R$ 1.234,50"
    assert format_brl(0.004) == "menos de R$ 0,01"
    assert long_date(NOVEMBER) == "sexta-feira, 6 de novembro"


def test_a_failure_notice_names_what_failed() -> None:
    message = failure_message(
        "o episódio de hoje", "voz", RuntimeError("a cota <acabou>")
    )

    assert message.split("\n") == [
        "⚠️ Não consegui gerar o episódio de hoje.",
        "<b>Etapa:</b> voz",
        "<b>Erro:</b> a cota &lt;acabou&gt;",
        "O log completo fica no Railway.",
    ]


def test_a_text_model_finds_its_price_under_the_name_as_saved() -> None:
    name = "google_genai:gemini-3.8-flash"
    prices = build_settings(
        text_model=name, text_input_price=0.75, text_output_price=3.75
    ).prices()
    usage = Usage(step="script", model=name, input_tokens=1_000_000)

    assert name in prices
    assert total_cost_usd([usage], prices) == pytest.approx(0.75)
