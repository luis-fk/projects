from collections.abc import Sequence

from news_podcast.api.core.schemas import Host, Turn, Usage

VOICE_MODEL = "google_genai:gemini-3.8-flash-lite-tts"


class FakeVoice:
    """Answers each request in turn with the next outcome: None answers, an error is
    raised. Without outcomes left, it answers, each answer costing `output_tokens` of
    `model`."""

    def __init__(
        self,
        outcomes: Sequence[Exception | None] = (),
        usages: list[Usage] | None = None,
        model: str = VOICE_MODEL,
        output_tokens: int = 1_000,
    ) -> None:
        self.outcomes = list(outcomes)
        self.usages = usages
        self.model = model
        self.output_tokens = output_tokens
        self.chunks: list[list[Turn]] = []
        self.hosts: list[Sequence[Host]] = []
        self.styles: list[tuple[str, str, str]] = []
        self.usages_seen: list[int] = []

    async def synthesize(
        self,
        turns: list[Turn],
        hosts: Sequence[Host],
        *,
        style: str,
        headline_style: str,
        step: str,
    ) -> tuple[bytes, Usage]:
        self.chunks.append(turns)
        self.hosts.append(hosts)
        self.styles.append((style, headline_style, step))
        if self.usages is not None:
            self.usages_seen.append(len(self.usages))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        return (
            f"audio {len(self.chunks)}".encode(),
            Usage(step=step, model=self.model, output_tokens=self.output_tokens),
        )
