"""
Concrete SessionProvider for the mock CoreBank app. Credentials come from
environment variables and are never written into an artifact, a log line, or
an evidence file -- only the fact that a re-login was *attempted* is logged.
"""
import os


class MockBankSessionProvider:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def is_login_url(self, url: str) -> bool:
        return "/login" in url

    def login(self, page) -> None:
        username = os.environ.get("MOCK_APP_USERNAME", "operator")
        password = os.environ.get("MOCK_APP_PASSWORD", "demo-pass")
        page.goto(f"{self.base_url}/login")
        page.locator('input[name="username"]').fill(username)
        page.locator('input[name="password"]').fill(password)
        page.locator('input[type="submit"]').click()
