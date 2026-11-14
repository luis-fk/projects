# A lesson stays under 10 minutes: the admin refuses a maximum of 10 or more.
LESSON_MINUTES_LIMIT = 10
# The worst case: the text waits (TEXT_RETRY_MINUTES, 10 + 20), the voice waits
# (VOICE_RETRY_MINUTES, 10 + 20) and a margin for the requests themselves.
MAX_LESSON_MINUTES = 90
# The longest path runs 16 nodes (two topics with two searches each, then the script,
# the review, the voice, the audio and the send), and LangGraph needs a limit at
# least one above it. The limit stops a routing bug from looping on paid requests.
MAX_LESSON_STEPS = 20
