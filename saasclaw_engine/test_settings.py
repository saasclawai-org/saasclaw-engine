"""Minimal Django settings for pytest."""

SECRET_KEY = 'test-secret-key-only-for-testing'
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}
INSTALLED_APPS = [
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'saasclaw_engine.projects',
    'saasclaw_engine.studio_models',
    'saasclaw_engine.agents',
    'saasclaw_engine.integrations',
    'saasclaw_engine.deployments',
]
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
USE_TZ = True

LLM_GATEWAY_BLOCKED_PROVIDERS = ['zai', 'openai', 'anthropic', 'google', 'mistral', 'groq', 'deepseek', 'together', 'fireworks']
LLM_GATEWAY_URL = 'http://127.0.0.1:8081/v1'
LLM_GATEWAY_MODEL = ''
PROJECT_APPROVAL_REQUIRED = False

# GitHub App settings (for integration tests)
GITHUB_APP_ID = 12345
GITHUB_APP_PRIVATE_KEY = 'dummy-key'
GITHUB_WEBHOOK_SECRET = 'test-webhook-secret'
