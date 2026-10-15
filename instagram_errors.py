"""Safe, actionable classifications; never return raw upstream payloads."""
import re

import instaloader
from requests.exceptions import RequestException


def explain(error):
    text = str(error).lower()
    if any(word in text for word in ('challenge_required', 'checkpoint_required', 'checkpoint', 'feedback_required')):
        return 'confirmation_required', 'Confirm the security prompt in Instagram, then retry.'
    if isinstance(error, instaloader.TooManyRequestsException) or re.search(r'\b429\b', text):
        return 'rate_limited', 'Instagram rate limit (HTTP 429). Wait before retrying.'
    if isinstance(error, instaloader.LoginRequiredException) or any(word in text for word in ('logged out', 'redirected to login', 'login_required')):
        return 'session_expired', 'Session expired. Use browser login, then retry.'
    if re.search(r'\b401\b', text):
        return 'unauthorized', 'Instagram denied access (HTTP 401). Try browser login or wait before retrying.'
    if re.search(r'\b403\b', text):
        return 'forbidden', 'Instagram denied access (HTTP 403). Check security prompts in Instagram.'
    if isinstance(error, instaloader.ProfileNotExistsException):
        return 'profile_unavailable', 'Profile unavailable. Check the username and access in Instagram.'
    if isinstance(error, (KeyError, TypeError)) or any(word in text for word in ('execution error', 'doc_id', 'graphql')):
        return 'instagram_api_changed', 'Unexpected Instagram response. Its API may have changed.'
    if isinstance(error, (instaloader.ConnectionException, RequestException)):
        return 'connection_failed', 'Cannot connect to Instagram. Check your connection and retry later.'
    return 'download_failed', 'Download failed. Check your connection and profile access.'
