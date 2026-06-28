"""Minimal Django settings for pytest."""

SECRET_KEY = 'test-secret-key-for-pytest-only'
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}
INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'saasclaw_engine.projects',
]
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
USE_TZ = True
LLM_GATEWAY_BLOCKED_PROVIDERS = ['zai', 'openai', 'anthropic', 'google', 'mistral', 'groq', 'deepseek', 'together', 'fireworks']
LLM_GATEWAY_URL = 'http://127.0.0.1:8081/v1'
LLM_GATEWAY_MODEL = ''
PROJECT_APPROVAL_REQUIRED = False
