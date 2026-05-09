import json
import logging
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Tuple

import requests
from dotenv import load_dotenv
from flask import Flask, g, jsonify, request, send_from_directory
from litellm import completion

load_dotenv()

app = Flask(__name__, static_folder='.', static_url_path='')

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
)
logger = logging.getLogger('gitguild')

DEFAULT_MODEL = os.getenv('LLM_MODEL', 'google/gemma-4-E2B-it')
GITHUB_API = 'https://api.github.com'
PIPESHIFT_API_BASE = os.getenv('PIPESHIFT_API_BASE', 'https://api.pipeshift.com/api/v0').strip().strip('"').strip("'")
PIPESHIFT_COMPLETIONS_URL = (
    os.getenv('PIPESHIFT_COMPLETIONS_URL', '') or os.getenv('PIPESHIFT_CHAT_COMPLETIONS_URL', '')
).strip().strip('"').strip("'")
PIPESHIFT_API_KEY = os.getenv('PIPESHIFT_API_KEY', '').strip().strip('"').strip("'")
# Timeout controls:
# - LLM_TIMEOUT_SECONDS is the preferred global knob.
# - LLM_FAIL_FAST_TIMEOUT_SECONDS is retained for backward compatibility.
LLM_TIMEOUT_SECONDS = float(os.getenv('LLM_TIMEOUT_SECONDS', os.getenv('LLM_FAIL_FAST_TIMEOUT_SECONDS', '60')))
LLM_FAIL_FAST_TIMEOUT_SECONDS = LLM_TIMEOUT_SECONDS
LLM_CONNECT_TIMEOUT_SECONDS = float(os.getenv('LLM_CONNECT_TIMEOUT_SECONDS', '5'))
LLM_READ_TIMEOUT_SECONDS = float(os.getenv('LLM_READ_TIMEOUT_SECONDS', str(LLM_TIMEOUT_SECONDS)))
PIPESHIFT_MAX_FALLBACK_URLS = int(os.getenv('PIPESHIFT_MAX_FALLBACK_URLS', '2'))
DEFAULT_GITHUB_TOKEN = (
    os.getenv('GITHUB_TOKEN', '')
    or os.getenv('GH_PAT', '')
    or os.getenv('GITHUB_PAT', '')
).strip().strip('"').strip("'")


@app.before_request
def _log_request_start() -> None:
    g.request_id = str(uuid.uuid4())[:8]
    g.request_start = time.time()
    logger.info(
        '[%s] %s %s from=%s',
        g.request_id,
        request.method,
        request.path,
        request.remote_addr,
    )


@app.after_request
def _log_request_end(response):
    start = getattr(g, 'request_start', time.time())
    request_id = getattr(g, 'request_id', 'no-id')
    elapsed_ms = int((time.time() - start) * 1000)
    logger.info('[%s] completed status=%s duration_ms=%s', request_id, response.status_code, elapsed_ms)
    return response


def github_headers(token: str = '') -> Dict[str, str]:
    headers = {
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'git-guild-dashboard',
    }
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


def parse_repo(repo_input: str) -> str:
    repo = repo_input.strip()
    if repo.startswith('http://') or repo.startswith('https://'):
        match = re.search(r'github\.com/([^/]+/[^/]+)', repo, re.IGNORECASE)
        if not match:
            raise ValueError('Invalid GitHub repository URL.')
        repo = match.group(1)
    return repo.replace('.git', '').strip('/')


def github_get(url: str, token: str = '', params: Dict[str, Any] | None = None) -> requests.Response:
    resp = requests.get(url, headers=github_headers(token), params=params or {}, timeout=45)
    logger.debug('github_get url=%s params=%s status=%s', url, params, resp.status_code)
    if resp.status_code >= 400:
        if resp.status_code in (401, 403) and 'rate limit' in resp.text.lower():
            raise RuntimeError(
                'GitHub API rate limit reached for unauthenticated requests. '
                'Add a PAT for higher limits.'
            )
        raise RuntimeError(f'GitHub API error {resp.status_code}: {resp.text}')
    return resp


def fetch_commits(repo: str, token: str = '') -> List[Dict[str, Any]]:
    logger.info('Fetching commits repo=%s auth=%s', repo, bool(token))
    commits: List[Dict[str, Any]] = []
    page = 1

    while True:
        url = f'{GITHUB_API}/repos/{repo}/commits'
        resp = github_get(url, token, {'per_page': 100, 'page': page})
        chunk = resp.json()
        if not chunk:
            break

        logger.debug('fetch_commits repo=%s page=%s count=%s', repo, page, len(chunk))

        for c in chunk:
            commit_data = c.get('commit', {})
            author_meta = commit_data.get('author') or {}
            commits.append(
                {
                    'sha': c.get('sha'),
                    'author_login': (c.get('author') or {}).get('login'),
                    'author_name': author_meta.get('name'),
                    'author_email': author_meta.get('email'),
                    'date': author_meta.get('date'),
                    'message': commit_data.get('message', ''),
                }
            )
        page += 1

    logger.info('Fetched commits repo=%s total=%s', repo, len(commits))
    return commits


def fetch_pull_requests(repo: str, token: str = '') -> List[Dict[str, Any]]:
    logger.info('Fetching pull requests repo=%s', repo)
    prs: List[Dict[str, Any]] = []
    page = 1

    while True:
        url = f'{GITHUB_API}/repos/{repo}/pulls'
        resp = github_get(url, token, {'state': 'all', 'per_page': 100, 'page': page})
        chunk = resp.json()
        if not chunk:
            break

        logger.debug('fetch_pull_requests repo=%s page=%s count=%s', repo, page, len(chunk))

        for pr in chunk:
            prs.append(
                {
                    'number': pr.get('number'),
                    'title': pr.get('title'),
                    'state': pr.get('state'),
                    'merged_at': pr.get('merged_at'),
                    'created_at': pr.get('created_at'),
                    'closed_at': pr.get('closed_at'),
                    'author': (pr.get('user') or {}).get('login'),
                    'comments': pr.get('comments', 0),
                    'review_comments': pr.get('review_comments', 0),
                }
            )
        page += 1

    logger.info('Fetched pull requests repo=%s total=%s', repo, len(prs))
    return prs


def fetch_contributor_stats(repo: str, token: str = '') -> Dict[str, Dict[str, Any]]:
    logger.info('Fetching contributor stats repo=%s', repo)
    url = f'{GITHUB_API}/repos/{repo}/stats/contributors'

    # GitHub may return 202 while preparing contributor stats.
    for attempt in range(1, 5):
        resp = requests.get(url, headers=github_headers(token), timeout=45)
        logger.debug('fetch_contributor_stats repo=%s attempt=%s status=%s', repo, attempt, resp.status_code)
        if resp.status_code == 202:
            time.sleep(1.2)
            continue
        if resp.status_code >= 400:
            logger.warning('Contributor stats unavailable repo=%s status=%s', repo, resp.status_code)
            return {}

        data = resp.json() or []
        result: Dict[str, Dict[str, Any]] = {}
        for row in data:
            author = (row.get('author') or {}).get('login')
            if not author:
                continue

            weeks = row.get('weeks') or []
            active_weeks = [w for w in weeks if (w.get('c') or 0) > 0]
            additions = sum((w.get('a') or 0) for w in weeks)
            deletions = sum((w.get('d') or 0) for w in weeks)
            longest_streak = longest_active_week_streak(weeks)

            result[author] = {
                'total_commits': row.get('total', 0),
                'additions': additions,
                'deletions': deletions,
                'longest_streak_weeks': longest_streak,
                'active_weeks': len(active_weeks),
            }
        logger.info('Fetched contributor stats repo=%s contributors=%s', repo, len(result))
        return result

    logger.warning('Contributor stats timed out repo=%s after retries', repo)
    return {}


def longest_active_week_streak(weeks: List[Dict[str, Any]]) -> int:
    longest = 0
    current = 0
    for w in weeks:
        if (w.get('c') or 0) > 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def build_contributor_rollup(
    commits: List[Dict[str, Any]], prs: List[Dict[str, Any]], contributor_stats: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    by_person: Dict[str, Dict[str, Any]] = {}

    def get_key(author_login: str | None, author_name: str | None) -> str:
        return author_login or author_name or 'unknown'

    for c in commits:
        key = get_key(c.get('author_login'), c.get('author_name'))
        item = by_person.setdefault(
            key,
            {
                'name': key,
                'commits': 0,
                'first_commit': c.get('date'),
                'last_commit': c.get('date'),
                'prs': 0,
                'review_signal': 0,
            },
        )
        item['commits'] += 1
        item['first_commit'] = min(item['first_commit'] or c.get('date'), c.get('date'))
        item['last_commit'] = max(item['last_commit'] or c.get('date'), c.get('date'))

    for pr in prs:
        author = pr.get('author') or 'unknown'
        item = by_person.setdefault(
            author,
            {
                'name': author,
                'commits': 0,
                'first_commit': None,
                'last_commit': None,
                'prs': 0,
                'review_signal': 0,
            },
        )
        item['prs'] += 1
        item['review_signal'] += int(pr.get('comments') or 0) + int(pr.get('review_comments') or 0)

    for name, item in by_person.items():
        stats = contributor_stats.get(name, {})
        item['additions'] = stats.get('additions', 0)
        item['deletions'] = stats.get('deletions', 0)
        item['longest_streak_weeks'] = stats.get('longest_streak_weeks', 0)
        item['active_weeks'] = stats.get('active_weeks', 0)

    # Determine active/retired/newcomer status heuristics by date windows.
    dates = [c.get('date') for c in commits if c.get('date')]
    if not dates:
        return list(by_person.values())

    newest = max(dates)
    oldest = min(dates)

    newest_dt = datetime.fromisoformat(newest.replace('Z', '+00:00'))
    oldest_dt = datetime.fromisoformat(oldest.replace('Z', '+00:00'))
    span_days = max((newest_dt - oldest_dt).days, 1)

    for item in by_person.values():
        last = item.get('last_commit')
        first = item.get('first_commit')
        if not last or not first:
            item['status'] = 'supporting'
            continue

        last_dt = datetime.fromisoformat(last.replace('Z', '+00:00'))
        first_dt = datetime.fromisoformat(first.replace('Z', '+00:00'))

        days_since_last = (newest_dt - last_dt).days
        days_since_start = (first_dt - oldest_dt).days

        if days_since_last > max(60, span_days * 0.4):
            item['status'] = 'retired'
        elif days_since_start > max(45, span_days * 0.6):
            item['status'] = 'newcomer'
        else:
            item['status'] = 'active'

    return sorted(by_person.values(), key=lambda x: x.get('commits', 0), reverse=True)


def sanitize_for_prompt(text: Any) -> str:
    value = str(text or '')
    # Remove prompt-injection-like tags often used by reasoning models.
    value = value.replace('<think>', '[think]').replace('</think>', '[/think]')
    value = value.replace('<analysis>', '[analysis]').replace('</analysis>', '[/analysis]')
    value = value.replace('<assistant>', '[assistant]').replace('</assistant>', '[/assistant]')
    value = value.replace('```', "'''")
    return value.strip()


def build_history_blob(
    repo: str,
    commits: List[Dict[str, Any]],
    prs: List[Dict[str, Any]],
    contributors: List[Dict[str, Any]],
) -> str:
    lines = [
        f'Repository: {repo}',
        f'Total commits: {len(commits)}',
        f'Total pull requests: {len(prs)}',
        f'Total contributors (rolled up): {len(contributors)}',
        '--- CONTRIBUTOR SIGNALS ---',
    ]

    for c in contributors:
        lines.append(
            (
                f"{c.get('name')} status={c.get('status')} commits={c.get('commits')} prs={c.get('prs')} "
                f"additions={c.get('additions')} deletions={c.get('deletions')} "
                f"active_weeks={c.get('active_weeks')} streak_weeks={c.get('longest_streak_weeks')} "
                f"first={c.get('first_commit')} last={c.get('last_commit')}"
            )
        )

    lines.append('--- COMMITS ---')
    for idx, c in enumerate(commits, start=1):
        lines.append(
            (
                f"[{idx}] sha={c['sha']} author={c.get('author_login') or c.get('author_name') or 'unknown'} "
                f"date={c.get('date')}\n{sanitize_for_prompt(c.get('message', ''))}"
            )
        )

    lines.append('--- PULL REQUESTS ---')
    for idx, pr in enumerate(prs, start=1):
        lines.append(
            (
                f"[{idx}] #{pr.get('number')} author={pr.get('author')} state={pr.get('state')} merged_at={pr.get('merged_at')} "
                f"created_at={pr.get('created_at')} comments={pr.get('comments')} review_comments={pr.get('review_comments')}\n"
                f"{sanitize_for_prompt(pr.get('title', ''))}"
            )
        )

    return '\n'.join(lines)


def extract_json(content: str) -> Dict[str, Any]:
    content = (content or '').strip()
    if not content:
        raise ValueError('Model returned empty output.')

    def extract_balanced_json_object(text: str) -> str:
        in_string = False
        escaped = False
        depth = 0
        start_idx = -1
        for i, ch in enumerate(text):
            if escaped:
                escaped = False
                continue
            if ch == '\\':
                escaped = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                if depth == 0:
                    start_idx = i
                depth += 1
                continue
            if ch == '}':
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start_idx != -1:
                        return text[start_idx : i + 1]
        return ''

    def sanitize_candidate(text: str) -> str:
        cleaned = (text or '').strip().lstrip('\ufeff')
        cleaned = cleaned.replace('“', '"').replace('”', '"').replace('’', "'")
        # Common malformed JSON fix: trailing commas before } or ]
        cleaned = re.sub(r',(\s*[}\]])', r'\1', cleaned)
        return cleaned

    def salvage_truncated_json(text: str) -> str:
        raw = (text or '').strip()
        start = raw.find('{')
        if start == -1:
            return ''
        raw = raw[start:]

        out: List[str] = []
        stack: List[str] = []
        in_string = False
        escaped = False

        for ch in raw:
            out.append(ch)
            if escaped:
                escaped = False
                continue
            if ch == '\\':
                escaped = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch in '{[':
                stack.append(ch)
            elif ch == '}' and stack and stack[-1] == '{':
                stack.pop()
            elif ch == ']' and stack and stack[-1] == '[':
                stack.pop()

        repaired = ''.join(out).rstrip()
        if in_string:
            repaired += '"'

        # Drop incomplete dangling field fragments before closing containers.
        repaired = re.sub(r',\s*"[^"]*"?\s*:\s*$', '', repaired)
        repaired = re.sub(r',\s*$', '', repaired)

        while stack:
            opener = stack.pop()
            repaired += '}' if opener == '{' else ']'

        repaired = sanitize_candidate(repaired)
        return repaired

    candidates: List[str] = [content]
    fenced = re.search(r'```json\s*(.*?)\s*```', content, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidates.append(fenced.group(1))
    balanced = extract_balanced_json_object(content)
    if balanced:
        candidates.append(balanced)
    salvaged = salvage_truncated_json(content)
    if salvaged:
        candidates.append(salvaged)

    last_error: json.JSONDecodeError | None = None
    for raw in candidates:
        candidate = sanitize_candidate(raw)
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as err:
            last_error = err
            continue

    snippet = content[:500].replace('\n', '\\n')
    if last_error:
        raise ValueError(
            f'Model returned invalid JSON after cleanup: {last_error.msg} at line {last_error.lineno} col {last_error.colno}. '
            f'Snippet={snippet}'
        )
    raise ValueError(f'Model did not return valid JSON. Snippet={snippet}')


def compact_history_blob(history_blob: str, max_chars: int = 120000) -> str:
    if len(history_blob) <= max_chars:
        return history_blob

    head = history_blob[: max_chars // 2]
    tail = history_blob[-max_chars // 2 :]
    return f"{head}\n\n--- HISTORY TRUNCATED FOR TOKEN BUDGET ---\n\n{tail}"


def apply_defaults(data: Dict[str, Any]) -> Dict[str, Any]:
    data.setdefault('party', [])
    data.setdefault('milestones', [])
    data.setdefault('bosses', [])
    data.setdefault('story', {'prologue': '', 'chapters': [], 'epilogue': ''})
    data.setdefault(
        'team_summary',
        {
            'archetype': '',
            'history': '',
            'technical_debt_risk': '',
            'future_plans': '',
        },
    )
    data.setdefault('oracle', {'next_quests': [], 'risk_forecast': [], 'recommended_actions': []})
    return data


def pipeshift_completion_candidate_urls() -> List[str]:
    candidates: List[str] = []

    if PIPESHIFT_COMPLETIONS_URL:
        candidates.append(PIPESHIFT_COMPLETIONS_URL.rstrip('/'))

    base = PIPESHIFT_API_BASE.rstrip('/') if PIPESHIFT_API_BASE else ''
    if base:
        candidates.append(f'{base}/completions')
        candidates.append(f'{base}/chat/completions')

    candidates.extend(
        [
            'https://api.pipeshift.com/api/v0/completions',
            'https://api.pipeshift.com/api/v0/chat/completions',
            'https://api.pipeshift.com/v0/completions',
            'https://api.pipeshift.com/v0/chat/completions',
            'https://api.pipeshift.com/api/v1/completions',
            'https://api.pipeshift.com/api/v1/chat/completions',
        ]
    )

    seen = set()
    unique: List[str] = []
    for c in candidates:
        if c and c not in seen:
            unique.append(c)
            seen.add(c)
    limit = max(PIPESHIFT_MAX_FALLBACK_URLS, 1)
    if len(unique) > limit:
        logger.info('Limiting PipeShift fallback URLs to %s of %s candidates', limit, len(unique))
    return unique[:limit]


def call_pipeshift_direct(system_prompt: str, user_prompt: str) -> str:
    logger.info('Calling PipeShift direct fallback model=%s', DEFAULT_MODEL)
    headers = {
        'Authorization': f'Bearer {PIPESHIFT_API_KEY}',
        'Content-Type': 'application/json',
    }
    prompt = f"System:\n{system_prompt}\n\nUser:\n{user_prompt}\n\nAssistant:\n"
    errors: List[str] = []
    for url in pipeshift_completion_candidate_urls():
        is_chat_endpoint = '/chat/completions' in url
        body = (
            {
                'model': DEFAULT_MODEL,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt},
                ],
                'temperature': 0.2,
                'stream': False,
                'max_tokens': 1800,
                'response_format': {'type': 'json_object'},
            }
            if is_chat_endpoint
            else {
                'model': DEFAULT_MODEL,
                'prompt': prompt,
                'temperature': 0.2,
                'stream': False,
                'max_tokens': 1800,
            }
        )
        started_at = time.time()
        logger.info(
            'PipeShift direct request url=%s endpoint_type=%s timeout_connect_s=%s timeout_read_s=%s',
            url,
            'chat' if is_chat_endpoint else 'completion',
            LLM_CONNECT_TIMEOUT_SECONDS,
            LLM_READ_TIMEOUT_SECONDS,
        )
        resp = requests.post(
            url,
            headers=headers,
            json=body,
            timeout=(LLM_CONNECT_TIMEOUT_SECONDS, LLM_READ_TIMEOUT_SECONDS),
        )
        elapsed_ms = int((time.time() - started_at) * 1000)
        if resp.status_code == 404:
            errors.append(f'{url} -> 404 ({elapsed_ms}ms)')
            logger.warning('PipeShift direct got 404 url=%s elapsed_ms=%s', url, elapsed_ms)
            continue
        if resp.status_code >= 400:
            logger.error(
                'PipeShift direct failed url=%s status=%s elapsed_ms=%s body=%s',
                url,
                resp.status_code,
                elapsed_ms,
                resp.text[:500],
            )
            raise RuntimeError(f'PipeShift error {resp.status_code} at {url}: {resp.text}')

        data = resp.json()
        logger.info('PipeShift direct success url=%s status=%s elapsed_ms=%s', url, resp.status_code, elapsed_ms)
        if isinstance(data, dict) and isinstance(data.get('choices'), list) and data['choices']:
            choice = data['choices'][0]
            if isinstance(choice, dict):
                if choice.get('text'):
                    return str(choice.get('text'))
                msg = choice.get('message')
                if isinstance(msg, dict):
                    if msg.get('content'):
                        return str(msg.get('content'))
                    if msg.get('reasoning_content'):
                        return str(msg.get('reasoning_content'))

        raise RuntimeError(f'Unexpected PipeShift response shape from {url}: {data}')

    raise RuntimeError(f'PipeShift returned 404 for all completions URLs: {"; ".join(errors)}')


def llm_analyze(repo: str, history_blob: str) -> Dict[str, Any]:
    logger.info('Starting llm_analyze repo=%s model=%s history_chars=%s', repo, DEFAULT_MODEL, len(history_blob))
    if not PIPESHIFT_API_KEY:
        raise ValueError('Missing PIPESHIFT_API_KEY in environment/.env')

    output_contract = {
        'party': [
            {
                'name': 'string',
                'archetype': 'Barb|Rogue|Cleric|Wizard|Necromancer|Bard|Hybrid',
                'status': 'active|retired|fallen|newcomer|supporting',
                'sprite_hint': 'short visual cue for later asset swap',
                'stats': {
                    'str': '0-100',
                    'dex': '0-100',
                    'int': '0-100',
                    'con': '0-100',
                    'wis': '0-100',
                    'cha': '0-100',
                    'commits': 'number',
                    'prs': 'number',
                    'commit_frequency': 'string',
                    'loc_delta': 'string',
                    'longevity_signal': 'string',
                    'review_influence': 'string',
                },
                'lore_blurb': '1-2 lines',
            }
        ],
        'milestones': [{'title': 'string', 'summary': 'string', 'impact': 'string'}],
        'bosses': [{'name': 'string', 'difficulty': 'string', 'context': 'string', 'outcome': 'string'}],
        'story': {
            'prologue': 'string',
            'chapters': [{'title': 'string', 'text': 'string', 'image_prompt': 'string'}],
            'epilogue': 'string',
        },
        'team_summary': {
            'archetype': 'string',
            'history': 'string',
            'technical_debt_risk': 'string',
            'future_plans': 'string',
        },
        'oracle': {
            'next_quests': ['string'],
            'risk_forecast': ['string'],
            'recommended_actions': ['string'],
        },
    }

    stat_logic = (
        'Map stats using project data signals: '
        'STR=high code volume/additions, DEX=incident response/precision fixes, '
        'INT=complex design/architectural depth, CON=streak and sustained delivery, '
        'WIS=stable long-lived code and maintenance, CHA=PR collaboration and review/social influence.'
    )

    archetype_refs = (
        'Archetypes: Barb(STR+CON), Rogue(DEX), Cleric(CON+WIS), Wizard(INT+WIS), '
        'Necromancer(WIS+CON legacy rescue), Bard(CHA). '
        'Use Hybrid only when no single archetype dominates.'
    )

    system_prompt = (
        'You are the Game Master + Oracle for Git Guild. '\
        'Turn raw git and PR history into a coherent fantasy campaign report. '\
        'You must output ONLY valid JSON. No markdown wrappers. '\
        f'Output contract: {json.dumps(output_contract)} '\
        f'{stat_logic} {archetype_refs}'
    )

    safe_history = compact_history_blob(sanitize_for_prompt(history_blob), max_chars=120000)

    user_prompt = (
        f'Analyze this repository history and produce Lore + Oracle sections.\n\n'
        f'Repository: {repo}\n'
        f'Generated At: {datetime.utcnow().isoformat()}Z\n\n'
        'Important requirements:\n'
        '1) Include retired/fallen/newcomer states when justified by timeline.\n'
        '2) Include technical debt and future campaign risks.\n'
        '3) Keep story grounded in the data while being high-fantasy in tone.\n'
        '4) Output compact JSON only (target <= 1200 tokens).\n\n'
        f'{safe_history}'
    )

    # 1) Preferred: LiteLLM chat completion against PipeShift base.
    def run_litellm(curr_user_prompt: str) -> Dict[str, Any]:
        logger.info(
            'Calling LiteLLM completion(chat) prompt_chars=%s timeout_s=%s',
            len(curr_user_prompt),
            LLM_FAIL_FAST_TIMEOUT_SECONDS,
        )
        started_at = time.time()
        response = completion(
            model=DEFAULT_MODEL,
            custom_llm_provider='openai',
            api_base=PIPESHIFT_API_BASE,
            api_key=PIPESHIFT_API_KEY,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': curr_user_prompt},
            ],
            temperature=0.2,
            max_tokens=1800,
            stream=False,
            timeout=LLM_FAIL_FAST_TIMEOUT_SECONDS,
            response_format={'type': 'json_object'},
        )
        elapsed_ms = int((time.time() - started_at) * 1000)
        choices = response.get('choices') if isinstance(response, dict) else getattr(response, 'choices', None)
        choice_obj = (choices or [{}])[0]
        choice = choice_obj if isinstance(choice_obj, dict) else (choice_obj.model_dump() if hasattr(choice_obj, 'model_dump') else {})
        message = choice.get('message') if isinstance(choice.get('message'), dict) else {}
        content = message.get('content') or message.get('reasoning_content') or choice.get('text') or ''
        logger.info('LiteLLM response received elapsed_ms=%s content_chars=%s', elapsed_ms, len(content or ''))
        return apply_defaults(extract_json(content))

    try:
        result = run_litellm(user_prompt)
        logger.info('llm_analyze completed via LiteLLM first attempt')
        return result
    except Exception as e_litellm:
        logger.warning('LiteLLM attempt failed (primary path): %s', str(e_litellm))
        # Skip second LiteLLM retry and jump directly to one short fallback path.
        retry_prompt = (
            f'Repository: {repo}\n'
            'Return ONLY JSON object matching the required contract. '
            'No chain-of-thought. No markdown. Keep it concise.\n\n'
            f'{safe_history[:24000]}'
        )
        try:
            content = call_pipeshift_direct(system_prompt, retry_prompt)
            logger.info('llm_analyze completed via direct PipeShift fallback content_chars=%s', len(content or ''))
            return apply_defaults(extract_json(content))
        except Exception as e_direct:
            raise RuntimeError(
                f'PipeShift inference failed. LiteLLM chat failed ({e_litellm}). '
                f'Direct PipeShift completion failed ({e_direct}). '
                f'Using model={DEFAULT_MODEL}, base={PIPESHIFT_API_BASE}, '
                f'completions_url={PIPESHIFT_COMPLETIONS_URL or "<empty>"}, '
                f'timeout_s={LLM_FAIL_FAST_TIMEOUT_SECONDS}, '
                f'connect_timeout_s={LLM_CONNECT_TIMEOUT_SECONDS}, '
                f'read_timeout_s={LLM_READ_TIMEOUT_SECONDS}, '
                f'max_fallback_urls={PIPESHIFT_MAX_FALLBACK_URLS}.'
            )


@app.route('/')
def home():
    return send_from_directory('.', 'index.html')


@app.route('/<path:path>')
def static_proxy(path: str):
    return send_from_directory('.', path)


@app.route('/api/analyze', methods=['POST'])
def analyze() -> Tuple[Any, int] | Any:
    payload = request.get_json(silent=True) or {}
    repo_input = payload.get('repo', '')
    request_token = (payload.get('token') or '').strip()
    token = request_token or DEFAULT_GITHUB_TOKEN

    if not repo_input:
        logger.warning('analyze rejected: missing repo')
        return jsonify({'error': 'repo is required'}), 400

    try:
        repo = parse_repo(repo_input)
        token_source = 'request' if request_token else ('env' if DEFAULT_GITHUB_TOKEN else 'none')
        logger.info('Analyze started repo=%s auth=%s token_source=%s', repo, bool(token), token_source)

        t0 = time.time()
        commits = fetch_commits(repo, token)
        if not commits:
            logger.warning('Analyze aborted repo=%s reason=no commits', repo)
            return jsonify({'error': 'No commits found for this repository'}), 400

        prs = fetch_pull_requests(repo, token)
        contributor_stats = fetch_contributor_stats(repo, token)
        contributors = build_contributor_rollup(commits, prs, contributor_stats)

        history_blob = build_history_blob(repo, commits, prs, contributors)
        logger.info(
            'Data assembled repo=%s commits=%s prs=%s contributors=%s history_chars=%s elapsed_ms=%s',
            repo,
            len(commits),
            len(prs),
            len(contributors),
            len(history_blob),
            int((time.time() - t0) * 1000),
        )

        t1 = time.time()
        data = llm_analyze(repo, history_blob)
        logger.info('LLM completed repo=%s elapsed_ms=%s', repo, int((time.time() - t1) * 1000))

        return jsonify(
            {
                'ok': True,
                'repo': repo,
                'commit_count': len(commits),
                'pr_count': len(prs),
                'contributor_count': len(contributors),
                'data': data,
            }
        )
    except Exception as exc:
        logger.exception('Analyze failed repo_input=%s error=%s', repo_input, str(exc))
        return jsonify({'error': str(exc)}), 500


if __name__ == '__main__':
    port = int(os.getenv('PORT', '8000'))
    logger.info('Starting Git Guild backend port=%s log_level=%s model=%s', port, LOG_LEVEL, DEFAULT_MODEL)
    app.run(host='0.0.0.0', port=port, debug=True)
