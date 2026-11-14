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
