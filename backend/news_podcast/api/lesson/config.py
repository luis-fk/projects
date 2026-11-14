# A lesson stays under 10 minutes: the admin refuses a maximum of 10 or more.
LESSON_MINUTES_LIMIT = 10
# The worst case: the text waits (TEXT_RETRY_MINUTES, 10 + 20), the voice waits
# (VOICE_RETRY_MINUTES, 10 + 20) and a margin for the requests themselves.
MAX_LESSON_MINUTES = 90
# The longest path runs 16 nodes: choose_topic; two topics with two searches and two
# picks each, and next_topic between them; then keep_pages, the script, the review,
# the voice, the audio and the send. LangGraph needs a limit at least one above it
# (17). The limit stops a routing bug from looping on paid requests.
MAX_LESSON_STEPS = 20

# The lessons' trusted sites. Tavily matches a domain with its subdomains, and the code
# checks every host again, since the list is the project's rule.
LESSON_SOURCE_DOMAINS = (
    "leg.br",
    "jus.br",
    "gov.br",
    "politize.com.br",
    "agenciabrasil.ebc.com.br",
)
SEARCH_RESULTS = 10
# Per topic, and topics per lesson: 2 credits a search, so 8 credits at most.
MAX_SEARCHES = 2
MAX_TOPICS_PER_LESSON = 2
MIN_LESSON_PAGES = 2
MAX_LESSON_PAGES = 3
# Shorter is a stub or an index page; longer is cut, so three pages stay near 11,000
# input tokens per request.
MIN_PAGE_CHARS = 1_000
MAX_PAGE_CHARS = 15_000
