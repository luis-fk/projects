import asyncio
import base64
import json

import httpx
import pytest
from pytest_django import Settings

from news_podcast.api.core.config import HOSTS
from news_podcast.api.core.schemas import Turn, Usage
from news_podcast.api.core.voice.base import VoiceError
from news_podcast.api.core.voice.gemini import GeminiVoice, pcm_to_wav, wav_duration
from news_podcast.api.core.voice.providers import VOICE_PROVIDERS, voice_provider

MODEL = "gemini-3.8-flash-lite-tts"
AUDIO_ANSWER = {
    "steps": [
        {
            "type": "model_output",
            "content": [
                {
                    "type": "audio",
                    "data": base64.b64encode(pcm_to_wav(b"\x00\x00" * 2400)).decode(),
                }
            ],
        }
    ]
}


@pytest.fixture(autouse=True)
def gemini_key(settings: Settings) -> None:
    settings.GEMINI_API_KEY = "test-key"


def no_request(request: httpx.Request) -> httpx.Response:
    raise AssertionError("no voice request should be made")


@pytest.mark.parametrize("saved", [f"google_genai:{MODEL}", MODEL])
def test_a_gemini_voice_model_speaks_through_gemini_voice(saved: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=AUDIO_ANSWER)

    async def scenario() -> tuple[object, bytes, Usage]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            voice = voice_provider(saved, client)
            audio, usage = await voice.synthesize(
                [Turn(speaker="Bia", text="Oi")],
                HOSTS,
                style="calma",
                headline_style="enfática",
                step="voice",
            )
            return voice, audio, usage

    voice, audio, usage = asyncio.run(scenario())

    assert isinstance(voice, GeminiVoice)
    (request,) = requests
    assert request.url.path.endswith("/interactions")
    assert json.loads(request.content)["model"] == MODEL
    assert request.headers["x-goog-api-key"] == "test-key"
    assert wav_duration(audio) == pytest.approx(0.1)
    assert usage.step == "voice"
    assert usage.model == saved


@pytest.mark.parametrize(
    "saved", ["google_genai:", "google_vertexai:gemini-3.8-flash-lite-tts"]
)
def test_a_voice_model_without_an_implementation_is_refused_before_any_request(
    saved: str,
) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(no_request)
        ) as client:
            voice_provider(saved, client)

    with pytest.raises(VoiceError) as caught:
        asyncio.run(scenario())

    assert saved in str(caught.value)
    assert "google_genai" in str(caught.value)


def test_every_host_has_a_voice_for_each_voice_provider() -> None:
    for host in HOSTS:
        assert set(VOICE_PROVIDERS) <= set(host.voices), host.name
