import os

# Existing unit tests exercise the model branch without an external knowledge
# service. Exact QA tests inject a matcher or explicitly enable its HTTP client.
os.environ.setdefault("NEW_DH_EXACT_QA__ENABLED", "false")
