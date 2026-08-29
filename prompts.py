import config

SYSTEM_PROMPT = getattr(config, "SYSTEM_PROMPT", """You are a helpful AI Voice Receptionist.""")
INITIAL_GREETING = getattr(config, "INITIAL_GREETING", "Hello, how can I help you today?")
FALLBACK_GREETING = getattr(config, "fallback_greeting", "Hello!")
