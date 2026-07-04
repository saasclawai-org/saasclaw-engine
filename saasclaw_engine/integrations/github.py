import os
import shutil
import subprocess
import time
import base64
from pathlib import Path

import jwt
import requests
from django.conf import settings


def github_app_configured() -> bool:
    private_key = os.getenv('GITHUB_APP_PRIVATE_KEY', '').strip()
    private_key_path = os.getenv('GITHUB_APP_PRIVATE_KEY_PATH', '').strip()
    path_ok = bool(private_key_path and Path(private_key_path).exists())
    return all([
        os.getenv('GITHUB_APP_ID', '').strip(),
        (private_key or path_ok),
        os.getenv('GITHUB_WEBHOOK_SECRET', '').strip(),
    ])


def get_github_private_key() -> str:
    inline = settings.GITHUB_APP_PRIVATE_KEY.strip()
    if inline:
        return inline
    path = settings.GITHUB_APP_PRIVATE_KEY_PATH.strip()
    if path and Path(path).exists():
        return Path(path).read_text()
    raise RuntimeError('GitHub App private key is not configured.')


def build_github_app_jwt() -> str:
    app_id = settings.GITHUB_APP_ID.strip()
    if not app_id:
        raise RuntimeError('GitHub App ID is not configured.')
    now = int(time.time())
    payload = {
        'iat': now - 60,
        'exp': now + (9 * 60),
        'iss': app_id,
    }
    return jwt.encode(payload, get_github_private_key(), algorithm='RS256')


def create_installation_access_token(installation_id: int) -> str:
    jwt_token = build_github_app_jwt()
    response = requests.post(
        f'https://api.github.com/app/installations/{installation_id}/access_tokens',
        headers={
            'Authorization': f'Bearer {jwt_token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()['token']


def create_repo_for_installation(installation_id: int, repo_name: str, owner: str, private: bool = True, description: str = '') -> dict:
    """Create a repository using the GitHub App installation token."""
    token = create_installation_access_token(installation_id)
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }
    payload = {
        'name': repo_name,
        'private': private,
        'description': description or '',
    }

    # Try org endpoint first
    response = requests.post(
        f'https://api.github.com/orgs/{owner}/repos',
        headers=headers, json=payload, timeout=30,
    )
    if response.status_code == 201:
        return response.json()

    # For user installations, try /user/repos
    response = requests.post(
        'https://api.github.com/user/repos',
        headers=headers, json=payload, timeout=30,
    )
    if response.status_code == 201:
        return response.json()

    # Raise with details
    response.raise_for_status()
    return response.json()

def _git_error_message(action: str, owner: str, repo_name: str, exc: subprocess.CalledProcessError) -> str:
    detail = (exc.stderr or exc.stdout or '').strip()
    if detail:
        detail = ' '.join(detail.split())
    else:
        detail = f'exit status {exc.returncode}'
    return f'{action} failed for {owner}/{repo_name}: {detail}'


def _github_repo_url(owner: str, repo_name: str) -> str:
    return f'git@github.com:{owner}/{repo_name}.git'


def _git_auth_args(token: str) -> list[str]:
    basic = base64.b64encode(f'x-access-token:{token}'.encode('utf-8')).decode('ascii')
    return ['-c', f'http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}']


def clone_or_update_repo(installation_id: int, owner: str, repo_name: str, branch: str, destination: str) -> str:
    token = create_installation_access_token(installation_id)
    repo_url = f'https://github.com/{owner}/{repo_name}.git'
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        last_error: subprocess.CalledProcessError | None = None
        for attempt in range(3):
            try:
                subprocess.run(['git', *_git_auth_args(token), 'clone', '--branch', branch, repo_url, str(dest)], check=True, capture_output=True, text=True)
                return str(dest)
            except subprocess.CalledProcessError as branch_exc:
                last_error = branch_exc
                if dest.exists():
                    shutil.rmtree(dest, ignore_errors=True)
                try:
                    subprocess.run(['git', *_git_auth_args(token), 'clone', repo_url, str(dest)], check=True, capture_output=True, text=True)
                    return str(dest)
                except subprocess.CalledProcessError as clone_exc:
                    last_error = clone_exc
                    if dest.exists():
                        shutil.rmtree(dest, ignore_errors=True)
                    if attempt < 2:
                        time.sleep(attempt + 1)
        raise RuntimeError(_git_error_message('Git clone', owner, repo_name, last_error)) from None
    try:
        subprocess.run(['git', '-C', str(dest), 'remote', 'set-url', 'origin', repo_url], check=True, capture_output=True, text=True)
        subprocess.run(['git', *_git_auth_args(token), '-C', str(dest), 'fetch', 'origin'], check=True, capture_output=True, text=True)
        checkout = subprocess.run(['git', '-C', str(dest), 'checkout', branch], check=False, capture_output=True, text=True)
        if checkout.returncode != 0:
            subprocess.run(['git', '-C', str(dest), 'checkout', '-B', branch], check=True, capture_output=True, text=True)
        reset = subprocess.run(['git', '-C', str(dest), 'reset', '--hard', f'origin/{branch}'], check=False, capture_output=True, text=True)
        if reset.returncode != 0:
            head_check = subprocess.run(['git', '-C', str(dest), 'rev-parse', '--verify', 'HEAD'], check=False, capture_output=True, text=True)
            if head_check.returncode == 0:
                subprocess.run(['git', '-C', str(dest), 'reset', '--hard', 'HEAD'], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(_git_error_message('Git update', owner, repo_name, exc)) from None
    return str(dest)


def commit_and_push_repo(installation_id: int, owner: str, repo_name: str, branch: str, repo_path: str, message: str) -> None:
    token = create_installation_access_token(installation_id)
    repo_url = f'https://github.com/{owner}/{repo_name}.git'
    try:
        subprocess.run(['git', '-C', repo_path, 'config', 'user.name', 'SaaSClaw'], check=True, capture_output=True, text=True)
        subprocess.run(['git', '-C', repo_path, 'config', 'user.email', 'bot@saasclaw.ai'], check=True, capture_output=True, text=True)
        subprocess.run(['git', '-C', repo_path, 'add', '.'], check=True, capture_output=True, text=True)
        status = subprocess.run(['git', '-C', repo_path, 'status', '--porcelain'], check=True, capture_output=True, text=True)
        if not status.stdout.strip():
            return
        subprocess.run(['git', '-C', repo_path, 'commit', '-m', message], check=True, capture_output=True, text=True)
        subprocess.run(['git', '-C', repo_path, 'remote', 'set-url', 'origin', repo_url], check=True, capture_output=True, text=True)
        subprocess.run(['git', *_git_auth_args(token), '-C', repo_path, 'push', '-u', 'origin', f'HEAD:{branch}'], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(_git_error_message('Git push', owner, repo_name, exc)) from None
